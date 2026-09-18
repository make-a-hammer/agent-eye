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
import re
import textwrap
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Decision:
    """AI 做出的单步决策。"""
    action: str  # extract | search | navigate | type | wait | done
    reason: str = ""           # 为什么做这个决策
    content: str = ""          # extract 时的提取内容
    search_query: str = ""     # search 时的搜索关键词
    target_url: str = ""       # navigate 时的目标链接
    selector: str = ""         # navigate/type 时的元素引用或 CSS 选择器
    text: str = ""             # type 时要输入的文本
    submit: bool = False       # type 后是否按回车提交
    delay_ms: int = 2000       # wait 时的延迟毫秒数
    raw: dict = field(default_factory=dict)  # LLM 原始响应


SYSTEM_PROMPT = textwrap.dedent("""\
你是一个网页内容提取 Agent。给你一个网页的截图和文字内容，以及用户想找什么的描述，
你需要决定下一步做什么。

返回纯 JSON（不要 markdown 代码块），格式：
{
    "action": "extract|search|navigate|type|wait|done",
    "reason": "为什么做这个决策（一句话）",
    "content": "提取到的内容文本（仅 extract 时）",
    "search_query": "搜索关键词（仅 search 时）",
    "target_url": "目标链接（仅 navigate 时）",
    "selector": "元素引用（如 e3）或 CSS 选择器（仅 navigate/type 时）",
    "text": "要输入的文本（仅 type 时）",
    "submit": true,
    "delay_ms": 2000
}

决策规则：
- extract: 页面包含用户要找的东西 → 直接提取文本内容
- search: 页面不是目标，需要搜索 → 提供搜索关键词
- navigate: 找到了相关链接，需要点进去 → 提供链接和选择器
- type: 需要在搜索框/输入框里打字 → 填 selector、text；submit=true 表示打完回车提交
- wait: 遇到验证码/反爬/加载中 → 等待后重试
- done: 任务已完成或不可能完成 → 结束

定位元素的优先级（重要）：
1. 若消息里有「可交互元素」列表，**优先把它的 ref 填进 selector**（如 "e3"）——
   语义定位（按名字）比 CSS 按位置选，在导航栏/列表页里可靠得多。
2. 没有该列表时，才用 CSS 选择器（如 "a.read-more"）。

注意：search 动作会跳到 google.com（墙内不通）；在中国站点内搜索时，
**优先用 type 填它自己的搜索框 + submit**，而不是 search。
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
    # 可交互元素（camofox 后端提供）→ 让 LLM 用 ref 而不是猜 CSS
    inter = obs.get("interactive") or []
    if inter:
        lines = [f"  [{e.get('ref', '?')}] {e.get('role', '?')}: {e.get('name', '')}"
                 for e in inter[:25]]
        parts.append("## 可交互元素（点击时优先把 ref 填进 selector）\n" + "\n".join(lines))
    if history:
        recent = history[-5:]  # 只保留最近 5 步
        parts.append(f"## 之前尝试过\n{json.dumps(recent, ensure_ascii=False, indent=2)}")
    parts.append("## 指令\n输出下一步决策的 JSON。")
    return "\n\n".join(parts)


def _parse_json_lenient(raw: str) -> dict:
    """
    容错解析 LLM 返回的 JSON。

    真实 LLM（kimi-k3 实测）常返回 ```json 包裹、前后带解释文字、或被 max_tokens
    截断 —— 直接 json.loads 会 `Expecting value` 而**整个任务中断**（2026-09-18 实测）。
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("空响应")
    # ① 剥 markdown 代码块
    if "```" in s:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
        if m:
            s = m.group(1).strip()
    # ② 直接解析
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # ③ 取首尾大括号之间
    i, j = s.find("{"), s.rfind("}")
    inner = s[i:j + 1] if (i >= 0 and j > i) else ""
    if inner:
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass
    # ④ 截断补救：补常见后缀
    base = inner or s
    for suffix in ('"}', '"}}', '}'):
        try:
            return json.loads(base + suffix)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"无法解析为 JSON（前 120 字）: {s[:120]}")


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
        # 注意：title 与 body 都要查——标题命中同样是有效结果
        # （browse_bench.py t1_title_hit 曾因此失败）
        body = obs.get("body_snippet", "")
        title = obs.get("title", "")
        q = query.lower()
        if q in body.lower():
            return Decision(action="extract", content=body[:500],
                           reason="fallback: 关键词匹配到正文")
        if q in title.lower():
            return Decision(action="extract", content=(title + "\n" + body)[:500],
                           reason="fallback: 关键词匹配到标题")
        return Decision(action="done", reason="无 LLM 且关键词未匹配")

    user_msg = make_user_message(obs, query, history)
    raw = ""
    try:
        raw = llm(SYSTEM_PROMPT, user_msg)
        data = _parse_json_lenient(raw)
    except Exception as e:  # noqa: BLE001
        return Decision(action="done", reason=f"LLM 解析失败: {e}",
                        raw={"error": str(e), "raw_prefix": (raw or "")[:200]})

    return Decision(
        action=data.get("action", "done"),
        reason=data.get("reason", ""),
        content=data.get("content", ""),
        search_query=data.get("search_query", ""),
        target_url=data.get("target_url", ""),
        selector=data.get("selector", ""),
        text=data.get("text", ""),
        submit=bool(data.get("submit", False)),
        delay_ms=data.get("delay_ms", 2000),
        raw=data,
    )
