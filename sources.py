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
    return classify_query(query)["sources"]


# ─── 查询理解分类（升级：六类路由）─────────────────

# 查询类型 → 推荐数据源 + 特征标记
# 源名对应 intel.py 的 SOURCES 字典
QUERY_TYPES = {
    "factual":      {"sources": ["web", "wikidata"],                    "freshness": False, "authority": True,  "diversity": False},
    "comparative":  {"sources": ["web", "openalex_deep", "github"],     "freshness": True,  "authority": True,  "diversity": True},
    "research":     {"sources": ["openalex_deep", "web", "trends"],      "freshness": True,  "authority": True,  "diversity": True},
    "technical":    {"sources": ["github", "web"],                      "freshness": True,  "authority": True,  "diversity": False},
    "operational":  {"sources": ["web", "trends"],                      "freshness": True,  "authority": False, "diversity": False},
    "computational": {"sources": ["web", "openalex_deep"],              "freshness": False, "authority": True,  "diversity": False},
    "high_risk":    {"sources": ["openalex_deep", "web", "trends"],      "freshness": True,  "authority": True,  "diversity": True},
}

# 关键词 → 查询类型
_TYPE_KEYWORDS = {
    "comparative": ["vs", "对比", "比较", "哪个好", "区别", "差异", "优缺点", "哪个更"],
    "research":    ["研究", "综述", "趋势", "现状", "进展", "论文", "paper", "survey",
                    "arxiv", "doi", "预印本", "文献", "state of the art", "最新"],
    "computational": ["计算", "统计", "多少", "比例", "增长率", "同比", "calculate",
                      "how many", "percentage", "average", "总和"],
    "operational": ["下载", "登录", "注册", "安装", "购买", "预订", "填表", "提交",
                    "download", "login", "install", "buy", "book", "submit"],
    "technical":   ["开源", "仓库", "工具", "库", "框架", "插件", "软件", "github",
                    "repo", "repository", "代码", "pip", "npm", "import", "sdk",
                    "library", "framework", "open source"],
    "high_risk":   ["医疗", "诊断", "法律", "诉讼", "投资", "股票", "药", "剂量",
                    "medical", "legal", "invest", "stock", "diagnosis", "处方"],
}


# computational 例外：这些是复合名词，不代表计算意图
_COMPUTATIONAL_EXCEPTIONS = [
    "量子计算", "云计算", "边缘计算", "计算器", "计算机", "科学计算",
    "计算机视觉", "计算机科学", "计算化学", "量子计算机", "高性能计算",
]


def classify_query(query: str) -> dict:
    """
    查询理解分类（六类），决定用哪些源 + 加权特征。

    Returns:
        {
          "type": "research",
          "sources": ["papers", "web", "trends"],
          "freshness": True, "authority": True, "diversity": True
        }
    比旧版只分 paper/code/web 更细：指导路由 + 结果加权。
    """
    q = query.lower()
    matched = None

    # 高风险优先（安全第一）
    for t in ["high_risk", "operational", "computational", "comparative",
              "technical", "research"]:
        if any(kw in q for kw in _TYPE_KEYWORDS[t]):
            # computational 例外：复合名词（量子计算/云计算）不等于计算意图
            if t == "computational" and any(ex in q for ex in _COMPUTATIONAL_EXCEPTIONS):
                continue
            matched = t
            break

    # 代码/技术类特殊处理（github 深挖源）
    if any(kw in q for kw in ["github", "repo", "repository", "代码", "pip", "npm",
                              "import", "开源", "仓库"]):
        cfg = dict(QUERY_TYPES["technical"])
        cfg["type"] = "technical"
        return cfg

    if matched is None:
        # 学术关键词兜底
        academic_kw = ["paper", "论文", "arxiv", "doi", "theorem", "定理", "algorithm", "算法",
                       "neural", "transformer", "模型", "dataset", "数据集", "benchmark"]
        matched = "research" if any(kw in q for kw in academic_kw) else "factual"

    cfg = dict(QUERY_TYPES[matched])
    cfg["type"] = matched
    return cfg


# ─── 结果合并加权（升级：时效 + 权威 + 多样性）─────

# 高权威域名（一手来源）——精确匹配，避免 .org 类泛化误判
AUTHORITY_DOMAINS = [
    ".gov", ".edu", ".gov.cn", ".edu.cn",
    "arxiv.org", "nature.com", "science.org", "ieee.org", "acm.org",
    "nih.gov", "who.int", "github.com", "wikipedia.org",
    "xinhuanet.com", "people.com.cn", "news.cn",
    "tmtpost.com", "trendforce.cn",   # 行业一手媒体
]

# 明确不算权威的来源（聚合器/解析器，避免误加分）
NON_AUTHORITY_DOMAINS = ["doi.org", "dx.doi.org", "t.co", "bit.ly", "google.com", "bing.com"]


def score_item(item: dict, qtype: dict | None = None,
               seen_domains: set | None = None) -> float:
    """
    多源结果综合打分（0-100）。

    score = 基础分 + 权威性加分 - 同站重复惩罚

    Args:
        item: {"title","url","source","snippet","published_days"(可选)}
        qtype: classify_query() 的结果（决定是否启用时效/权威/多样性加权）
        seen_domains: 已出现过的域名集合（用于多样性惩罚）
    """
    qtype = qtype or QUERY_TYPES["factual"]
    score = 50.0

    url = (item.get("url") or "").lower()

    # ① 权威性（一手来源加分；聚合器/解析器不加）
    if qtype.get("authority"):
        is_non_auth = any(d in url for d in NON_AUTHORITY_DOMAINS)
        if not is_non_auth and any(d in url for d in AUTHORITY_DOMAINS):
            score += 20

    # ② 时效性（新内容加分）
    if qtype.get("freshness"):
        days = item.get("published_days")
        if days is not None:
            if days <= 3:
                score += 15
            elif days <= 7:
                score += 10
            elif days <= 30:
                score += 5
            elif days > 365:
                score -= 10

    # ③ 多样性（同域名重复降权）
    if qtype.get("diversity") and seen_domains is not None:
        from urllib.parse import urlparse
        try:
            domain = urlparse(url).netloc
            if domain and domain in seen_domains:
                score -= 15
        except Exception:
            pass

    # ④ 内容充实度（有摘要的优于空摘要）
    if item.get("snippet"):
        score += min(10, len(item["snippet"]) / 50)

    return max(0.0, min(100.0, score))


def score_and_dedup(items: list[dict], query: str) -> list[dict]:
    """
    对多源结果统一打分 + 同站多样性降权 + 排序。
    在 intel.py 合并多源结果后调用。
    """
    qtype = classify_query(query)
    seen_domains = set()
    scored = []

    # 先按原始顺序打权威/时效分
    for it in items:
        it["_score"] = score_item(it, qtype, seen_domains)
        scored.append(it)

    # 按分排序（高分优先占位）
    scored.sort(key=lambda x: x["_score"], reverse=True)

    # 多样性：高分优先，同域名降权
    from urllib.parse import urlparse
    final, used = [], set()
    for it in scored:
        try:
            domain = urlparse(it.get("url") or "").netloc
        except Exception:
            domain = ""
        if qtype.get("diversity") and domain and domain in used:
            it["_score"] = max(0, it["_score"] - 15)  # 同站惩罚
        if domain:
            used.add(domain)
        final.append(it)

    final.sort(key=lambda x: x["_score"], reverse=True)
    return final


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
