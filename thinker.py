#!/usr/bin/env python3
"""
thinker.py — agent-eye v2 大脑组件

接收 Observation + 任务描述，输出决策。
LLM 通过 callable 注入，保持模型无关。

决策类型:
    extract  — 当前页面有目标内容，提取并返回
    search   — 没找到，输入关键词搜索
    navigate — 找到了链接，点击进入详情
    wait     — 被反爬/需要等待，延迟后重试
    done     — 任务完成或无法继续
"""

import json
import textwrap
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Decision:
    """AI 做出的单步决策。"""
    action: str  # extract | search | navigate | wait | done
    reason: str = ""           # 为什么做这个决策
    content: str = ""          # extract 时的提取内容
    search_query: str = ""     # search 时的搜索关键词
    target_url: str = ""       # navigate 时的目标链接
    selector: str = ""         # navigate 时的点击选择器
    delay_ms: int = 2000       # wait 时的延迟毫秒数
    raw: dict = field(default_factory=dict)  # LLM 原始响应


SYSTEM_PROMPT = textwrap.dedent("""\
你是一个网页内容提取 Agent。给你一个网页的截图和文字内容，以及用户想找什么的描述，
你需要决定下一步做什么。

返回纯 JSON（不要 markdown 代码块），格式：
{
    "action": "extract|search|navigate|wait|done",
    "reason": "为什么做这个决策（一句话）",
    "content": "提取到的内容文本（仅 extract 时）",
    "search_query": "搜索关键词（仅 search 时）",
    "target_url": "目标链接（仅 navigate 时）",
    "selector": "CSS 选择器（仅 navigate 时）",
    "delay_ms": 2000
}

决策规则：
- extract: 页面包含用户要找的东西 → 直接提取文本内容
- search: 页面不是目标，需要搜索 → 提供搜索关键词
- navigate: 找到了相关链接，需要点进去 → 提供链接和选择器
- wait: 遇到验证码/反爬/加载中 → 等待后重试
- done: 任务已完成或不可能完成 → 结束
""")


def make_user_message(obs: dict, query: str, history: list[dict] | None = None) -> str:
    """构建发给 LLM 的用户消息。"""
    parts = [
        f"## 任务\n{query}",
        f"## 当前页面\nURL: {obs.get('url', '?')}",
        f"标题: {obs.get('title', '?')}",
        f"描述: {obs.get('meta_desc', '?')}",
        f"正文预览:\n{obs.get('body_snippet', '')[:1000]}",
    ]
    if history:
        recent = history[-5:]  # 只保留最近 5 步
        parts.append(f"## 之前尝试过\n{json.dumps(recent, ensure_ascii=False, indent=2)}")
    parts.append("## 指令\n输出下一步决策的 JSON。")
    return "\n\n".join(parts)


def decide(
    obs: dict,
    query: str,
    llm: Callable[[str, str], str] | None = None,
    history: list[dict] | None = None,
) -> Decision:
    """
    核心决策函数。
    llm(system_prompt, user_message) -> str (JSON)
    """
    if llm is None:
        # 无 LLM 时：fallback — 尝试从页面提取相关内容
        body = obs.get("body_snippet", "")
        if query.lower() in body.lower():
            return Decision(action="extract", content=body[:500],
                           reason="fallback: 关键词匹配到正文")
        return Decision(action="done", reason="无 LLM 且关键词未匹配")

    user_msg = make_user_message(obs, query, history)
    try:
        raw = llm(SYSTEM_PROMPT, user_msg)
        data = json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        return Decision(action="done", reason=f"LLM 解析失败: {e}", raw={"error": str(e)})

    return Decision(
        action=data.get("action", "done"),
        reason=data.get("reason", ""),
        content=data.get("content", ""),
        search_query=data.get("search_query", ""),
        target_url=data.get("target_url", ""),
        selector=data.get("selector", ""),
        delay_ms=data.get("delay_ms", 2000),
        raw=data,
    )
