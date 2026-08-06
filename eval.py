#!/usr/bin/env python3
"""
eval.py — agent-eye 决策基准

灵感来源：Uber ADR 的 ADR-Bench（300+ 任务、17 攻击技术、双代理评分）

自己评估自己的决策质量：
  1. 每个 step 后自动评判本次决策是否合理
  2. 累积准确率，发现模式性错误
  3. 输出 JSON 报告给 ethics.py 的研究日志

评分维度：
  - decision_quality:   决策是否合理 (0-1)
  - action_efficiency:  是否选择了最优动作 (0-1)
  - anti_pattern:       是否落入已知陷阱 (0=无, 1=落入)
"""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── 已知反模式（从 ADR-Bench 提取）─────────────────


KNOWN_ANTI_PATTERNS = {
    "infinite_wait_loop": {
        "pattern": ["wait", "wait", "wait"],
        "description": "连续等待超过3次——可能卡在验证码",
        "severity": "high",
    },
    "search_redirect_loop": {
        "pattern": ["search", "search", "search"],
        "description": "连续搜索超过3次——应该换策略而不是换关键词",
        "severity": "medium",
    },
    "empty_extract": {
        "pattern": ["extract"],
        "description": "提取了空内容——应该检查页面是否加载完成",
        "severity": "low",
    },
    "navigate_too_many": {
        "pattern": ["navigate"] * 5,
        "description": "连续导航超过5次——迷路了",
        "severity": "medium",
    },
}


# ─── 评分模型 ──────────────────────────────────


@dataclass
class StepEval:
    """单步评分"""
    step: int
    action: str
    decision_quality: float = 0.0
    action_efficiency: float = 0.0
    anti_pattern: str = ""
    anti_pattern_severity: str = ""
    note: str = ""
    timestamp: str = ""


@dataclass
class SessionEval:
    """整个 session 的评分"""
    session_id: str
    total_steps: int = 0
    avg_quality: float = 0.0
    avg_efficiency: float = 0.0
    anti_patterns_hit: int = 0
    steps: list[StepEval] = field(default_factory=list)


# ─── 评分引擎 ──────────────────────────────────


def evaluate_step(step: int, action: str, reason: str = "", content: str = "",
                  history_actions: Optional[list] = None) -> StepEval:
    """
    评判单步决策。

    Args:
        step: 当前步数
        action: 决策动作 (extract/search/navigate/wait/done)
        reason: 决策理由
        content: 提取的内容（用于检查空提取）
        history_actions: 最近几步的动作列表

    Returns:
        StepEval
    """
    eval_ = StepEval(step=step, action=action, timestamp=datetime.now().isoformat())

    # ── 决策质量评分 ──
    if action == "extract":
        eval_.decision_quality = 1.0 if content and len(content) > 20 else 0.3
        if not content or len(content) <= 20:
            eval_.note = "提取内容过短，可能页面未正确解析"

    elif action == "search":
        eval_.decision_quality = 0.7  # 搜索通常是合理的尝试
        eval_.note = "搜索是探索性动作，7/10"

    elif action == "navigate":
        eval_.decision_quality = 0.8  # 导航需要具体目标
        eval_.note = "导航到新页面"

    elif action == "wait":
        eval_.decision_quality = 0.5  # 等待通常是遇到障碍
        eval_.note = "等待——可能遇到反爬或加载问题"

    elif action == "done":
        eval_.decision_quality = 0.5  # 中性
        eval_.note = "任务结束"

    # ── 动作效率评分 ──
    if history_actions:
        # 检查是否落入反模式
        recent = history_actions[-5:] + [action]

        for name, ap in KNOWN_ANTI_PATTERNS.items():
            pattern_len = len(ap["pattern"])
            if len(recent) >= pattern_len:
                if recent[-pattern_len:] == ap["pattern"]:
                    eval_.anti_pattern = name
                    eval_.anti_pattern_severity = ap["severity"]
                    eval_.action_efficiency = 0.1
                    eval_.note += f" | 检测到反模式: {ap['description']}"
                    return eval_

    eval_.action_efficiency = 0.7  # 默认中等效率
    return eval_


def evaluate_session(history: list[dict]) -> SessionEval:
    """
    对整个 session 的决策历史做综合评判。
    history 格式: [{"step": 1, "action": "extract", "reason": "...", "content": "..."}, ...]
    """
    session = SessionEval(
        session_id=datetime.now().strftime("%Y%m%d_%H%M%S"),
        total_steps=len(history),
    )

    actions_so_far = []
    for h in history:
        step_eval = evaluate_step(
            step=h.get("step", 0),
            action=h.get("action", "?"),
            reason=h.get("reason", ""),
            content=h.get("content", ""),
            history_actions=actions_so_far,
        )
        session.steps.append(step_eval)
        actions_so_far.append(h.get("action", "?"))

    if session.steps:
        session.avg_quality = sum(s.decision_quality for s in session.steps) / len(session.steps)
        session.avg_efficiency = sum(s.action_efficiency for s in session.steps) / len(session.steps)
        session.anti_patterns_hit = sum(1 for s in session.steps if s.anti_pattern)

    return session


# ─── 报告输出 ──────────────────────────────────


def session_report(session: SessionEval) -> dict:
    """输出可序列化的评估报告"""
    return {
        "session_id": session.session_id,
        "total_steps": session.total_steps,
        "avg_quality": round(session.avg_quality, 2),
        "avg_efficiency": round(session.avg_efficiency, 2),
        "anti_patterns_hit": session.anti_patterns_hit,
        "grade": _letter_grade(session),
        "steps": [
            {
                "step": s.step,
                "action": s.action,
                "quality": s.decision_quality,
                "efficiency": s.action_efficiency,
                "anti_pattern": s.anti_pattern,
                "note": s.note,
            }
            for s in session.steps
        ],
    }


def _letter_grade(session: SessionEval) -> str:
    """综合评级"""
    avg = (session.avg_quality + session.avg_efficiency) / 2
    if session.anti_patterns_hit > 0:
        avg -= 0.3 * session.anti_patterns_hit
    if avg >= 0.8: return "A"
    if avg >= 0.6: return "B"
    if avg >= 0.4: return "C"
    return "D"


# ─── CLI ──────────────────────────────────────


if __name__ == "__main__":
    # 模拟测试
    fake_history = [
        {"step": 1, "action": "extract", "reason": "找到了目标内容", "content": "这是一段很长的提取内容" * 10},
        {"step": 2, "action": "wait", "reason": "遇到验证码"},
        {"step": 3, "action": "wait", "reason": "验证码"},
        {"step": 4, "action": "wait", "reason": "验证码"},
        {"step": 5, "action": "wait", "reason": "验证码"},
    ]
    session = evaluate_session(fake_history)
    print(json.dumps(session_report(session), ensure_ascii=False, indent=2))
