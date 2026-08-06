#!/usr/bin/env python3
"""
loop.py — agent-eye v2 主循环（协议增强版）

串联 vision → thinker → hand，实现 AI 驱动的循环爬虫。
v2.1: 失败时接入调试协议（protocol.py + repair.py）——
      签名匹配已知错误 → 应用验证过的修复 → 重试，形成记忆化调试循环。

用法:
    import asyncio
    from loop import run_agent

    result = asyncio.run(run_agent(
        start_url="https://scholar.google.com/scholar?q=transformer",
        query="找 Transformer 论文的 PDF 链接",
        llm=my_llm_fn,          # (system, user) -> str
        max_steps=10,
    ))
"""

import asyncio
import textwrap
from typing import Callable

from vision import observe
from hand import BrowserSession
from thinker import decide, Decision
from ethics import throttle, detector
from router import route_from_obs
from eval import StepEval, evaluate_step, evaluate_session, session_report
from protocol import load_protocol, DebugProtocol, DebugEntry
from repair import Repairer


async def diagnose_and_repair(
    protocol: DebugProtocol,
    repairer: Repairer,
    stage: str,
    error_text: str,
    context: dict | None = None,
) -> tuple[bool, str, DebugEntry | None]:
    """
    失败处理子循环：签名匹配 → 执行修复 → 返回是否已处理。

    Returns:
        (handled, detail, matched_entry)
        handled=True 表示修复动作已执行（主循环应重试原操作）
    """
    entry = protocol.match(stage, error_text)
    if entry is None:
        return False, f"未命中协议: {error_text[:120]}", None

    # 命中 → 执行该条目的修复动作序列
    actions = entry.fix.get("actions", [])
    if not actions:
        return False, f"{entry.id} 无修复动作", entry

    repaired, detail = await repairer.execute(actions, context)
    if repaired:
        protocol.record_match(entry)
        return True, f"{entry.id} → {detail}", entry
    return False, f"{entry.id} 修复未完成: {detail}", entry


async def run_agent(
    start_url: str,
    query: str,
    llm: Callable[[str, str], str] | None = None,
    max_steps: int = 10,
    headless: bool = False,
) -> dict:
    """
    运行 agent-eye 主循环。

    Args:
        start_url: 起始 URL
        query: 用户想找什么（自然语言）
        llm: LLM 决策函数 (system_prompt, user_message) -> str
        max_steps: 最大循环步数，防止无限循环
        headless: 是否无头模式

    Returns:
        {
            "success": bool,
            "result": str,           # 提取到的内容
            "steps_taken": int,
            "history": [dict, ...],  # 每一步的决策记录
        }
    """
    history: list[dict] = []
    protocol = load_protocol()

    async with BrowserSession(headless=headless) as hand:
        repairer = Repairer(hand)

        # Step 0: 导航到起始页（带协议兜底）
        ok, detail = await hand.navigate(start_url)
        if not ok:
            handled, rdetail, entry = await diagnose_and_repair(
                protocol, repairer, "navigate", detail,
                context={"url": start_url})
            if handled:
                ok, detail = await hand.navigate(start_url)
            if not ok:
                return {"success": False, "result": f"导航失败: {detail}",
                        "steps_taken": 0, "history": history}

        for step in range(1, max_steps + 1):
            # 🔒 人类节奏——每步间隔 1-3 秒
            throttle.wait()

            # ① 观察
            obs = await observe(hand.page, start_url if step == 1 else hand.page.url)

            # 🚦 场景路由（reverse-skill 模式）
            strategy = route_from_obs(obs)

            # ② 决策
            decision: Decision = decide(obs, query, llm=llm, history=history)

            # 记录历史
            history.append({
                "step": step,
                "url": obs["url"],
                "action": decision.action,
                "reason": decision.reason,
                "scene": strategy.scene,
            })

            # 🛡️ 异常检测（ADR 模式）
            is_fail = decision.action in ("wait", "done")
            if detector.record(decision.action, ok=not is_fail,
                               note=f"scene={strategy.scene}"):
                print(f"  ⚠️ ADR 检测到异常，自动暂停: {detector.reason}")
                decision = Decision(action="done", reason=detector.reason)

            # ③ 执行（失败时走协议诊断-修复-重试）
            if decision.action == "extract":
                return {
                    "success": True,
                    "result": decision.content,
                    "steps_taken": step,
                    "history": history,
                }

            elif decision.action == "search":
                if decision.search_query:
                    search_url = f"https://www.google.com/search?q={decision.search_query}"
                    ok, detail = await hand.navigate(search_url)
                    if not ok:
                        handled, rdetail, entry = await diagnose_and_repair(
                            protocol, repairer, "navigate", detail,
                            context={"url": search_url})
                        if handled:
                            ok, detail = await hand.navigate(search_url)
                        if not ok:
                            history[-1]["error"] = detail
                            history[-1]["repair"] = rdetail

            elif decision.action == "navigate":
                if decision.target_url:
                    ok, detail = await hand.navigate(decision.target_url)
                    stage = "navigate"
                    ctx = {"url": decision.target_url}
                elif decision.selector:
                    ok, detail = await hand.click(decision.selector)
                    stage = "click"
                    ctx = {"url": obs["url"], "selector": decision.selector}
                else:
                    ok, detail = False, "navigate 缺少 target_url 或 selector"
                    stage, ctx = "any", {}
                if not ok:
                    handled, rdetail, entry = await diagnose_and_repair(
                        protocol, repairer, stage, detail, context=ctx)
                    if handled:
                        # 修复后重试原操作
                        if stage == "navigate" and decision.target_url:
                            ok, detail = await hand.navigate(decision.target_url)
                        elif stage == "click" and decision.selector:
                            ok, detail = await hand.click(decision.selector)
                    history[-1]["repair"] = rdetail
                    if not ok:
                        history[-1]["error"] = detail

            elif decision.action == "wait":
                await hand.wait(decision.delay_ms)

            elif decision.action == "done":
                return {
                    "success": False,
                    "result": decision.reason or "任务结束",
                    "steps_taken": step,
                    "history": history,
                }

        # 达到最大步数
        return {
            "success": False,
            "result": f"达到最大步数 {max_steps}",
            "steps_taken": max_steps,
            "history": history,
        }
