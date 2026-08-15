#!/usr/bin/env python3
"""
sources.py — agent-eye v2 搜索源配置

替代 Google Scholar / Google Search 的反爬友好源。
按资源类型智能路由：论文 → OpenAlex + arXiv，代码 → GitHub，通用 → DuckDuckGo。
"""

from urllib.parse import quote

# OpenBiliClaw 适配器（可选依赖）
try:
    from openbiliclaw_adapter import discover as obc_discover, to_trend_items as obc_to_trend
    HAS_OBC = True
except ImportError:
    HAS_OBC = False

# Telegram 数据源（Bot API，可选）
try:
    from telegram_source import get_channel_messages as tg_get_messages, to_trend_items as tg_to_trend
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False

# Sci-Hub 学术源（Node.js worker 渲染绕 Cloudflare，可选）
try:
    from scihub_source import fetch_by_doi as sh_fetch_by_doi, to_trend_items as sh_to_trend
    HAS_SCIHUB = True
except ImportError:
    HAS_SCIHUB = False


def generate_search_urls(query: str, source_types: list[str] | None = None) -> list[str]:
    """
    生成搜索 URL 列表。按查询内容推断来源类型。

    Args:
        query: 搜索关键词
        source_types: 手动指定来源类型列表
                      可选: 'paper', 'code', 'web', 'academic'
    """
    encoded = quote(query)
    urls = []

    types = source_types or _infer_source_types(query)

    for t in types:
        if t == "paper" or t == "academic":
            # arXiv API（返回 XML/Atom，可直接解析）
            urls.append(
                f"https://export.arxiv.org/api/query?"
                f"search_query=all:{encoded}&start=0&max_results=5"
            )
            # OpenAlex（返回 JSON，免费无限制）
            urls.append(
                f"https://api.openalex.org/works?"
                f"search={encoded}&per_page=5"
            )
        elif t == "code":
            urls.append(
                f"https://github.com/search?"
                f"q={encoded}&type=repositories&s=stars&o=desc"
            )
        elif t == "web":
            # DuckDuckGo Lite（HTML 友好，少反爬）
            urls.append(f"https://lite.duckduckgo.com/lite/?q={encoded}")

    return urls or [f"https://lite.duckduckgo.com/lite/?q={encoded}"]


def _infer_source_types(query: str) -> list[str]:
    """从查询内容推断需要搜索的来源类型。"""
    q = query.lower()
    types = []

    # 学术关键词
    academic_kw = [
        "paper", "论文", "arxiv", "doi", "pdf", "preprint", "预印本",
        "theorem", "定理", "proof", "证明", "algorithm", "算法",
        "neural", "transformer", "attention", "模型", "model",
        "dataset", "数据集", "benchmark", "experiment", "实验",
    ]
    if any(kw in q for kw in academic_kw):
        types.append("paper")

    # 代码关键词
    code_kw = ["github", "repo", "repository", "code", "代码", "pip", "npm", "import"]
    if any(kw in q for kw in code_kw):
        types.append("code")

    # 默认回退
    if not types:
        types.append("web")

    return types


# ─── 单页提取 URL（给 visual_fallback 用）─────────────────

def generate_extract_urls(query: str, max_urls: int = 3) -> list[str]:
    """
    生成用于直接提取的页面 URL（非 API）。
    优先选可直连、HTML 友好的页面。
    """
    encoded = quote(query)
    return [
        f"https://export.arxiv.org/search/?query={encoded}&searchtype=all",
        f"https://scholar.google.com/scholar?q={encoded}&hl=en",  # 保留 fallback
        f"https://lite.duckduckgo.com/lite/?q={encoded}",
    ][:max_urls]
