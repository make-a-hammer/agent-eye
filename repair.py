#!/usr/bin/env python3
"""
repair.py — agent-eye 修复执行器

OpenGame repairer 的 Python 版：按协议条目的 fix.actions 序列执行行为修复。
修复动作通过 hand 的原子操作实现，返回 (repaired: bool, detail: str)。

合规边界：修复动作只包含「等待/重试/换源/放弃」等行为调整，
不包含验证码破解、封禁绕过等违规操作。
"""

import asyncio
import re
from typing import TYPE_CHECKING

from ethics import throttle, log

if TYPE_CHECKING:
    from hand import BrowserSession


class Repairer:
    """执行协议中记录的修复动作序列。"""

    def __init__(self, hand: "BrowserSession"):
        self.hand = hand

    async def execute(self, actions: list[dict], context: dict | None = None) -> tuple[bool, str]:
        """
        依次执行修复动作。任一动作返回 repaired=True 即停止（成功信号）。
        全部执行完返回 False。

        Args:
            actions: 协议 fix.actions 列表，如 [{"action": "wait_longer", "detail": "..."}]
            context: 修复上下文（可选）：原操作类型、URL、选择器等

        Returns:
            (是否已修复, 说明)
        """
        context = context or {}
        for act in actions:
            name = act.get("action", "")
            detail = act.get("detail", "")
            ok, msg = await self._run_one(name, detail, context)
            log.log(agent="repairer", action=name, target=context.get("url", ""),
                    result=msg, note=f"协议修复: {detail}")
            if ok:
                return True, f"{name}: {msg}"
        return False, "所有修复动作执行完毕，问题未解决"

    async def _run_one(self, name: str, detail: str, ctx: dict) -> tuple[bool, str]:
        """执行单个修复动作。返回 (是否产生修复效果, 说明)。"""
        throttle.wait()

        if name == "wait_longer":
            # 等待额外时间让页面/异步内容稳定（提取 detail 中的秒数，默认 3s）
            m = re.search(r"(\d+)\s*s", detail)
            seconds = int(m.group(1)) if m else 3
            await self.hand.wait(seconds * 1000)
            return True, f"等待完成 ({seconds}s: {detail})"

        if name == "reload":
            ok, msg = await self._reload()
            return ok, msg

        if name == "reobserve":
            # 页面已变化，交给主循环重新观察（返回 True 表示"继续观察"）
            return True, "页面状态已更新，重新观察"

        if name == "scroll_then_click":
            selector = ctx.get("selector", "")
            ok, msg = await self.hand.scroll(300)
            if not ok:
                return False, msg
            await self.hand.wait(500)
            ok, msg = await self.hand.click(selector) if selector else (False, "无选择器")
            return ok, msg

        if name == "check_proxy":
            from ethics import ProxyConfig
            proxy = ProxyConfig.detect()
            if proxy:
                return True, f"代理可用: {proxy}"
            return False, "代理不可用"

        if name == "retry_with_proxy":
            # 代理需要重建会话——这里只记录状态，主循环决定是否重建
            # 如果当前上下文标记了代理可用，直接重试原操作
            proxy_ok = ctx.get("proxy_available", False)
            if proxy_ok:
                return True, "代理已确认，重试原操作"
            return False, "代理未确认可用，无法重试"

        if name == "throttle_up":
            # 提高节流延迟（429/风控后降频）
            old = throttle.base
            throttle.base = max(old, 5.0)
            throttle.jitter = 2.0
            return True, f"节流提高: {old}s → {throttle.base}s"

        if name == "switch_source":
            # 换源由 thinker 决策，这里只标记"建议换源"
            # 返回 False 让主循环知道本条目修复不彻底，触发重决策
            return False, "建议换源（由 thinker 重新决策）"

        if name == "retry_llm":
            return True, "重试 LLM（由主循环执行）"

        if name == "fallback_decide":
            return True, "降级为 fallback 决策"

        if name == "give_up":
            return False, f"放弃: {detail}"

        return False, f"未知修复动作: {name}"

    async def _reload(self) -> tuple[bool, str]:
        try:
            await self.hand.page.reload(wait_until="domcontentloaded", timeout=20000)
            await self.hand.wait(2000)
            return True, "页面已刷新"
        except Exception as e:
            return False, f"刷新失败: {e}"
