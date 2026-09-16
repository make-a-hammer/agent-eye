#!/usr/bin/env python3
"""
extractors.py — 站点专用提取器 + 文档分类器

源自架构报告 §4：
    "通用正文抽取器只能覆盖最常见的新闻页。真正高质量的检索系统
     必须为高价值站点建立专用提取器，并配套回归测试。"

架构:
    HTML/渲染响应
      → ① 规范化 URL（去 utm/追踪参数）
      → ② 文档分类器（article/paper_pdf/api_doc/forum_thread/...）
      → ③ 站点路由器（命中专用提取器）
      → ④ 通用提取器（回退）
      → ⑤ 统一输出对象（含置信度）

用法:
    from extractors import extract
    doc = extract("https://arxiv.org/abs/1706.03762", html)
    print(doc["document_type"], doc["title"], doc["extraction_confidence"])
"""

import hashlib
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# ─── ① URL 规范化 ──────────────────────────────

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "share_token", "share_source", "ref", "referrer",
    "fbclid", "gclid", "msclkid", "yclid", "_ga", "sessionid", "sid",
}


def canonicalize(url: str) -> str:
    """去掉追踪参数、排序 query、去 fragment → 避免同文多份。"""
    try:
        p = urlparse(url)
        # 过滤追踪参数并排序
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
        q.sort()
        # 去尾斜杠（根路径除外）
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme, p.netloc.lower(), path,
                           p.params, urlencode(q), ""))
    except Exception:
        return url


# ─── ② 文档分类器 ──────────────────────────────

DOC_PATTERNS = {
    "paper_pdf":     [r"\.pdf($|\?)", r"arxiv\.org/(abs|pdf)", r"/doi/", r"biorxiv|medrxiv"],
    "paper_meta":    [r"openalex\.org", r"crossref\.org", r"semanticscholar\.org"],
    "api_doc":       [r"/docs?/", r"api[-.]", r"developer\.", r"/reference/"],
    "dataset_page":  [r"/dataset", r"kaggle\.com/data", r"huggingface\.co/datasets", r"/data/"],
    "forum_thread":  [r"stackoverflow\.com/questions", r"reddit\.com/r/", r"v2ex\.com/t/",
                      r"zhihu\.com/question", r"discourse", r"/forum/"],
    "product_page":  [r"/product/", r"/item/", r"amazon\.", r"taobao\.", r"jd\.com/item"],
    "search_result": [r"google\.com/search", r"bing\.com/search", r"duckduckgo\.com/\?q",
                      r"baidu\.com/s\?", r"/search\?"],
    "video_page":    [r"youtube\.com/watch", r"bilibili\.com/video", r"vimeo\.com/"],
    "repo_page":     [r"github\.com/[\w-]+/[\w-]+$", r"gitlab\.com", r"gitee\.com"],
    "scholar_page":  [r"scholar\.google", r"cnki\.net", r"wanfangdata"],
}


def classify_document(url: str, html: str = "") -> str:
    """判断文档类型（报告 §4 的文档分类器）。"""
    u = url.lower()
    for doc_type, patterns in DOC_PATTERNS.items():
        if any(re.search(p, u) for p in patterns):
            return doc_type
    # 有正文特征的算 article
    if html and re.search(r"<article|itemprop=[\"']?articleBody", html, re.I):
        return "article"
    return "article" if html else "unknown"


# ─── ④ 通用提取器（回退）───────────────────────


def _strip_tags(html: str) -> str:
    t = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    t = re.sub(r"<style[^>]*>.*?</style>", "", t, flags=re.S | re.I)
    t = re.sub(r"<nav[^>]*>.*?</nav>", "", t, flags=re.S | re.I)
    t = re.sub(r"<footer[^>]*>.*?</footer>", "", t, flags=re.S | re.I)
    t = re.sub(r"<header[^>]*>.*?</header>", "", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"&nbsp;?", " ", t)
    t = re.sub(r"&amp;", "&", t)
    t = re.sub(r"&lt;", "<", t)
    t = re.sub(r"&gt;", ">", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def extract_generic(url: str, html: str) -> dict:
    """通用提取：标题 + 正文 + 章节结构。"""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        title = _strip_tags(m.group(1))[:200]
    if not title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
        if m:
            title = _strip_tags(m.group(1))[:200]

    # 章节结构（保留 H1/H2/H3 层级）
    sections = []
    for m in re.finditer(r"<h([123])[^>]*>(.*?)</h\1>", html, re.S | re.I):
        level, text = int(m.group(1)), _strip_tags(m.group(2))[:200]
        if text:
            sections.append({"level": level, "heading": text})

    body = _strip_tags(html)
    # 正文边界：去掉疑似导航/页脚（短片段拼接）
    body = re.sub(r"(首页|导航|登录|注册|版权|©|Copyright)\s*", "", body)

    m = re.search(r"<meta[^>]+property=[\"']article:published_time[\"'][^>]+content=[\"']([^\"']+)", html, re.I)
    published = m.group(1) if m else ""

    lang = ""
    m = re.search(r"<html[^>]+lang=[\"']([\w-]+)", html, re.I)
    if m:
        lang = m.group(1)

    return {
        "title": title,
        "body": body[:20000],
        "sections": sections[:50],
        "published_at": published,
        "language": lang,
    }


# ─── ③ 站点专用提取器 ──────────────────────────


def extract_arxiv(url: str, html: str) -> dict:
    """arXiv 摘要页专用。"""
    base = extract_generic(url, html)
    # arXiv 特色字段
    m = re.search(r'<meta name="citation_title" content="([^"]+)"', html)
    if m:
        base["title"] = m.group(1)
    authors = re.findall(r'<meta name="citation_author" content="([^"]+)"', html)
    base["authors"] = authors
    m = re.search(r'<meta name="citation_date" content="([^"]+)"', html)
    if m:
        base["published_at"] = m.group(1)
    m = re.search(r'<blockquote class="abstract[^>]*>(.*?)</blockquote>', html, re.S)
    if m:
        base["abstract"] = _strip_tags(m.group(1)).replace("Abstract:", "").strip()
    m = re.search(r'<td class="tablecell arxivdoi">.*?<a[^>]+href="([^"]+)"', html, re.S)
    orig = re.search(r'<meta name="citation_doi" content="([^"]+)"', html)
    if orig:
        base["doi"] = orig.group(1)
    base["extraction_confidence"] = 0.95
    return base


def extract_github(url: str, html: str) -> dict:
    """GitHub 仓库页专用。"""
    base = extract_generic(url, html)
    m = re.search(r'<meta property="og:description" content="([^"]*)"', html)
    if m:
        base["description"] = m.group(1)
    stars = re.search(r'id="repo-stars-counter-star"[^>]*title="([\d,]+)"', html)
    if stars:
        base["stars"] = stars.group(1)
    forks = re.search(r'id="repo-network-counter"[^>]*title="([\d,]+)"', html)
    if forks:
        base["forks"] = forks.group(1)
    lang = re.search(r'itemprop="programmingLanguage">([^<]+)<', html)
    if lang:
        base["language"] = lang.group(1).strip()
    lic = re.search(r'<a[^>]+href="[^"]*LICENSE[^"]*"[^>]*>\s*([^<]+?)\s*</a>', html)
    if lic:
        base["license"] = lic.group(1).strip()
    base["extraction_confidence"] = 0.92
    return base


def extract_zhihu(url: str, html: str) -> dict:
    """知乎问答/专栏专用。"""
    base = extract_generic(url, html)
    m = re.search(r'<h1[^>]*class="QuestionHeader-title"[^>]*>(.*?)</h1>', html, re.S)
    if m:
        base["title"] = _strip_tags(m.group(1))
    # 答案列表
    answers = re.findall(r'<div[^>]+class="RichContent-inner"[^>]*>(.*?)</div>', html, re.S)
    if answers:
        base["answers"] = [_strip_tags(a)[:2000] for a in answers[:5]]
    base["extraction_confidence"] = 0.85
    return base


def extract_paper_generic(url: str, html: str) -> dict:
    """学术页面通用（含 citation_* meta）。"""
    base = extract_generic(url, html)
    m = re.search(r'<meta name="citation_title" content="([^"]+)"', html)
    if m:
        base["title"] = m.group(1)
    base["authors"] = re.findall(r'<meta name="citation_author" content="([^"]+)"', html)
    m = re.search(r'<meta name="citation_doi" content="([^"]+)"', html)
    if m:
        base["doi"] = m.group(1)
    m = re.search(r'<meta name="citation_journal_title" content="([^"]+)"', html)
    if m:
        base["journal"] = m.group(1)
    base["extraction_confidence"] = 0.9
    return base


# 站点路由表：域名 → 提取器
SITE_EXTRACTORS = {
    "arxiv.org": extract_arxiv,
    "github.com": extract_github,
    "zhihu.com": extract_zhihu,
    "openalex.org": extract_paper_generic,
    "crossref.org": extract_paper_generic,
    "nature.com": extract_paper_generic,
    "science.org": extract_paper_generic,
    "ieee.org": extract_paper_generic,
    "acm.org": extract_paper_generic,
    "ncbi.nlm.nih.gov": extract_paper_generic,
}


# ─── 统一接口 ──────────────────────────────────


def extract(url: str, html: str) -> dict:
    """
    统一提取入口（报告 §4 输出规范）。

    Returns:
        {
          "canonical_url", "source_id", "document_type", "title", "authors",
          "published_at", "language", "sections", "body", "content_hash",
          "extraction_confidence", "extractor"
        }
    """
    canon = canonicalize(url)
    doc_type = classify_document(canon, html)
    domain = urlparse(canon).netloc

    # 站点专用提取器优先
    extractor = None
    for site_domain, fn in SITE_EXTRACTORS.items():
        if site_domain in domain:
            extractor = fn
            extractor_name = f"site:{site_domain}"
            break

    if extractor:
        result = extractor(canon, html)
    else:
        result = extract_generic(canon, html)
        extractor_name = "generic"

    # 补全统一字段
    result.setdefault("extraction_confidence", 0.7 if extractor_name == "generic" else 0.9)
    result.update({
        "canonical_url": canon,
        "source_id": domain,
        "document_type": doc_type,
        "content_hash": hashlib.sha256(
            (result.get("body", "") or result.get("title", "")).encode("utf-8")
        ).hexdigest()[:16],
        "extractor": extractor_name,
    })
    return result


# ─── CLI ─────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json
    import subprocess

    p = argparse.ArgumentParser(description="站点专用提取器")
    p.add_argument("url", help="要提取的 URL")
    p.add_argument("--json", action="store_true")
    p.add_argument("--proxy", default="", help="代理（如 socks5h://127.0.0.1:10808）")
    args = p.parse_args()

    # 抓取
    cmd = ["curl", "-sL", "--insecure", "-m", "30", args.url,
           "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)"]
    if args.proxy:
        cmd = cmd[:2] + ["-x", args.proxy] + cmd[2:]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    html = r.stdout

    print(f"📄 抓取: {len(html)} 字符")
    print(f"🔗 规范化: {canonicalize(args.url)}")
    print(f"📋 类型: {classify_document(canonicalize(args.url), html)}")
    print()
    doc = extract(args.url, html)

    if args.json:
        print(json.dumps({k: v for k, v in doc.items() if k != "body"},
                         ensure_ascii=False, indent=2))
    else:
        print(f"提取器: {doc['extractor']} | 置信度: {doc['extraction_confidence']}")
        print(f"标题: {doc.get('title','')[:80]}")
        if doc.get("authors"):
            print(f"作者: {', '.join(doc['authors'][:3])}")
        if doc.get("published_at"):
            print(f"发布: {doc['published_at']}")
        print(f"章节: {len(doc.get('sections', []))} 个")
        print(f"正文: {len(doc.get('body',''))} 字符")
        print(f"哈希: {doc['content_hash']}")
