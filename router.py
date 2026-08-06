#!/usr/bin/env python3
"""
router.py — agent-eye 场景路由矩阵

灵感来源：reverse-skill 的 MASTER-ROUTING 模式
"先识别场景类型，再分派正确策略——不要打开页面就直接行动。"

路由决策树：
  页面 URL → 域名识别 → 页面类型判断 → 策略分派
                                              ├── 搜索策略（search / trending / explore）
                                              ├── 提取策略（article / list / form / media）
                                              └── 回避策略（captcha / login / blocked）
"""

import re
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import Optional


# ─── 场景类型 ──────────────────────────────────


class SceneType:
    """页面场景枚举"""
    SEARCH = "search"         # 搜索结果页
    ARTICLE = "article"       # 文章/帖子详情页
    LIST = "list"             # 列表/分类页
    FORM = "form"             # 表单/登录页
    CAPTCHA = "captcha"       # 验证码/安全验证页
    MEDIA = "media"           # 视频/图片页
    PROFILE = "profile"       # 用户/作者主页
    HOME = "home"             # 首页
    UNKNOWN = "unknown"       # 未知


# ─── 策略分派 ──────────────────────────────────


@dataclass
class Strategy:
    """路由后的行动策略"""
    scene: str
    primary_action: str       # extract / search / navigate / wait / done
    max_steps: int = 5        # 该场景的建议最大步数
    throttle_multiplier: float = 1.0  # 节流倍率（登录页更慢）
    notes: str = ""


# ─── 域名→场景 路由表 ──────────────────────────


DOMAIN_ROUTES = {
    # 搜索引擎
    "google.com": SceneType.SEARCH,
    "scholar.google.com": SceneType.SEARCH,
    "baidu.com": SceneType.SEARCH,
    "bing.com": SceneType.SEARCH,
    "duckduckgo.com": SceneType.SEARCH,
    "lite.duckduckgo.com": SceneType.SEARCH,

    # 学术
    "arxiv.org": SceneType.LIST,
    "scholar.google.com": SceneType.SEARCH,
    "api.openalex.org": SceneType.LIST,
    "semanticscholar.org": SceneType.SEARCH,

    # 视频平台
    "youtube.com": SceneType.MEDIA,
    "bilibili.com": SceneType.MEDIA,
    "douyin.com": SceneType.MEDIA,

    # 社交/论坛
    "github.com": SceneType.LIST,
    "zhihu.com": SceneType.ARTICLE,
    "csdn.net": SceneType.ARTICLE,
    "reddit.com": SceneType.LIST,
    "xiaohongshu.com": SceneType.LIST,

    # 电商
    "taobao.com": SceneType.LIST,
    "amazon.com": SceneType.LIST,
}


# ─── 场景→策略 分派表 ──────────────────────────


STRATEGIES = {
    SceneType.SEARCH: Strategy(
        scene=SceneType.SEARCH,
        primary_action="extract",
        max_steps=3,
        notes="搜索结果页→直接提取标题+链接，不要点进每个结果",
    ),
    SceneType.ARTICLE: Strategy(
        scene=SceneType.ARTICLE,
        primary_action="extract",
        max_steps=5,
        notes="文章详情页→提取正文+元信息",
    ),
    SceneType.LIST: Strategy(
        scene=SceneType.LIST,
        primary_action="extract",
        max_steps=8,
        notes="列表页→提取条目，如有翻页可翻1-2页",
    ),
    SceneType.CAPTCHA: Strategy(
        scene=SceneType.CAPTCHA,
        primary_action="wait",
        throttle_multiplier=3.0,
        max_steps=2,
        notes="验证码页→等待后重试，不要硬闯",
    ),
    SceneType.MEDIA: Strategy(
        scene=SceneType.MEDIA,
        primary_action="extract",
        max_steps=5,
        notes="视频/图片页→提取标题+描述+元数据",
    ),
    SceneType.FORM: Strategy(
        scene=SceneType.FORM,
        primary_action="wait",
        throttle_multiplier=2.0,
        max_steps=1,
        notes="表单/登录页→不自动填写，返回给用户",
    ),
    SceneType.HOME: Strategy(
        scene=SceneType.HOME,
        primary_action="navigate",
        max_steps=5,
        notes="首页→往下滚动找目标内容",
    ),
    SceneType.UNKNOWN: Strategy(
        scene=SceneType.UNKNOWN,
        primary_action="extract",
        max_steps=3,
        notes="未知页面→保守：只提取可见内容",
    ),
}


# ─── URL 路径→精确场景 ──────────────────────────


URL_PATH_PATTERNS = [
    # /search?q=... /s?wd=... → 搜索页
    (r"/search\b", SceneType.SEARCH),
    (r"/s\b", SceneType.SEARCH),
    (r"/scholar\b", SceneType.SEARCH),
    # /video/... /watch?... → 媒体
    (r"/watch\b", SceneType.MEDIA),
    (r"/video/", SceneType.MEDIA),
    # /login /signin → 表单
    (r"/login", SceneType.FORM),
    (r"/signin", SceneType.FORM),
    (r"/signup", SceneType.FORM),
    # /captcha → 验证码
    (r"/captcha", SceneType.CAPTCHA),
    (r"/verify", SceneType.CAPTCHA),
    (r"/security-verify", SceneType.CAPTCHA),
    # /@username → 个人主页
    (r"/@\w+", SceneType.PROFILE),
    (r"/user/", SceneType.PROFILE),
    # /abs/... → 论文摘要
    (r"/abs/", SceneType.ARTICLE),
    # /topics /categories → 列表
    (r"/topics?", SceneType.LIST),
    (r"/categories?", SceneType.LIST),
    (r"/explore\b", SceneType.LIST),
]


# ─── 路由决策函数 ──────────────────────────────


def route(url: str, page_title: str = "", body_snippet: str = "") -> Strategy:
    """
    主路由入口：给定 URL + 页面信息，返回最佳策略。

    Args:
        url: 完整的页面 URL
        page_title: 页面标题（可选，用于辅助判断）
        body_snippet: 页面正文片段（可选，用于检测验证码等）

    Returns:
        Strategy 对象
    """
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().replace("www.", "")
    path = parsed.path.lower()

    # 1. 精确 URL 路径匹配
    for pattern, scene in URL_PATH_PATTERNS:
        if re.search(pattern, path):
            return STRATEGIES[scene]

    # 2. 域名匹配
    for domain, scene in DOMAIN_ROUTES.items():
        if domain in netloc:
            return STRATEGIES[scene]

    # 3. 内容检测（验证码特征）
    captcha_signals = ["验证码", "captcha", "verify you are human", "机器人", "安全验证"]
    if any(s in page_title.lower() or s in body_snippet.lower() for s in captcha_signals):
        return STRATEGIES[SceneType.CAPTCHA]

    # 4. 默认：未知
    return STRATEGIES[SceneType.UNKNOWN]


def route_from_obs(obs: dict) -> Strategy:
    """
    从 vision.py 的 Observation 直接路由。
    obs 格式: {"url": ..., "title": ..., "body_snippet": ...}
    """
    return route(
        url=obs.get("url", ""),
        page_title=obs.get("title", ""),
        body_snippet=obs.get("body_snippet", ""),
    )


# ─── 路由历史分析 ──────────────────────────────


def should_pause(history: list[dict], max_consecutive_failures: int = 3) -> bool:
    """
    ADR 故障检测模式：连续失败 N 次自动建议暂停。
    """
    if len(history) < max_consecutive_failures:
        return False
    recent = history[-max_consecutive_failures:]
    fail_actions = {"wait", "done", "captcha"}
    return all(h.get("action", "") in fail_actions for h in recent)


# ─── 路由统计 ──────────────────────────────────


def route_stats(history: list[dict]) -> dict:
    """分析历史决策，输出路由效率。"""
    total = len(history)
    if total == 0:
        return {"total_steps": 0, "success_rate": 0, "avg_steps_per_page": 0}

    successes = sum(1 for h in history if h.get("action") == "extract")
    return {
        "total_steps": total,
        "success_rate": successes / total if total else 0,
        "actions": {a: sum(1 for h in history if h.get("action") == a) for a in set(h.get("action", "?") for h in history)},
    }


if __name__ == "__main__":
    # 自测
    tests = [
        ("https://scholar.google.com/scholar?q=transformer", "search"),
        ("https://arxiv.org/abs/1706.03762", "article"),
        ("https://www.baidu.com/s?wd=AI", "search"),
        ("https://github.com/topics/arxiv", "list"),
        ("https://www.youtube.com/watch?v=123", "media"),
        ("https://example.com/login", "form"),
        ("https://unknown-site.com/random", "unknown"),
    ]
    for url, expected in tests:
        s = route(url)
        status = "✅" if s.scene == expected else f"❌ (got {s.scene})"
        print(f"{status} {url[:50]:50s} → {s.primary_action} ({s.notes[:40]})")
