#!/usr/bin/env python3
"""
loop.py — agent-eye v2 主循环

串联 vision → thinker → hand，实现 AI 驱动的循环爬虫。

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
from ethics import throttle


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

    async with BrowserSession(headless=headless) as hand:
        # Step 0: 导航到起始页
        ok, detail = await hand.navigate(start_url)
        if not ok:
            return {"success": False, "result": f"导航失败: {detail}",
                    "steps_taken": 0, "history": history}

        for step in range(1, max_steps + 1):
            # 🔒 人类节奏——每步间隔 1-3 秒
            throttle.wait()

            # ① 观察
            obs = await observe(hand.page, start_url if step == 1 else hand.page.url)

            # ② 决策
            decision: Decision = decide(obs, query, llm=llm, history=history)

            # 记录历史
            history.append({
                "step": step,
                "url": obs["url"],
                "action": decision.action,
                "reason": decision.reason,
            })

            # ③ 执行
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
                        history[-1]["error"] = detail

            elif decision.action == "navigate":
                if decision.target_url:
                    ok, detail = await hand.navigate(decision.target_url)
                elif decision.selector:
                    ok, detail = await hand.click(decision.selector)
                else:
                    ok, detail = False, "navigate 缺少 target_url 或 selector"
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
