#!/usr/bin/env python3
"""
sources_deep.py — 深挖数据源（学术 + 知识图谱）

源自架构报告 §1「加数据源」+ §2「加深单数据源」：
    - 不只搜标题，而是能查询、验证、构建上下文、追溯关系
    - 一手权威层：OpenAlex / Crossref / Wikidata

三个深挖源：
    ① OpenAlex 深挖   — 论文 + 作者 + 机构 + 引用网络
    ② Crossref        — DOI 解析 + 引用元数据（OpenAlex 的替代源）
    ③ Wikidata        — 实体消歧 + 关系查询（结构化事实）

用法:
    from sources_deep import fetch_openalex_deep, fetch_crossref, fetch_wikidata
    items = fetch_openalex_deep("solid state battery", limit=5)
"""

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用缓存 + 抗脆弱
try:
    from cache import get_cache
    HAS_CACHE = True
except ImportError:
    HAS_CACHE = False

try:
    from resilience import get_manager
    HAS_RESILIENCE = True
except ImportError:
    HAS_RESILIENCE = False


def _proxy() -> str | None:
    """检测代理（国外 API 需要）。"""
    try:
        from ethics import ProxyConfig
        return ProxyConfig.detect()
    except Exception:
        return None


def _get_json(url: str, timeout: int = 30) -> dict | None:
    """带缓存 + 代理的 JSON 获取。"""
    cache = get_cache() if HAS_CACHE else None

    # 缓存命中
    if cache:
        hit = cache.get(url)
        if hit:
            try:
                return json.loads(hit["content"])
            except json.JSONDecodeError:
                pass

    # 抗脆弱检查
    if HAS_RESILIENCE:
        rm = get_manager()
        if not rm.allow(url):
            print(f"  ⚠️ 断路器开启，跳过 {urllib.parse.urlparse(url).netloc}")
            return None

    proxy = _proxy()
    try:
        if proxy:
            host = proxy.replace("socks5h://", "").replace("socks5://", "")
            r = subprocess.run(
                ["curl", "-s", "--insecure", "-x", f"http://{host}", url,
                 "-H", "User-Agent: agent-eye/3.0 (research; contact via repo)"],
                capture_output=True, text=True, timeout=timeout)
            raw = r.stdout
        else:
            req = urllib.request.Request(
                url, headers={"User-Agent": "agent-eye/3.0 (research)"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")

        data = json.loads(raw)
        if HAS_RESILIENCE:
            get_manager().record_success(url)
        if cache:
            cache.put(url, raw, ttl_hours=168, source_id=urllib.parse.urlparse(url).netloc)
        return data
    except Exception as e:
        if HAS_RESILIENCE:
            get_manager().record_failure(url, reason=type(e).__name__)
        return None


# ─── ① OpenAlex 深挖 ───────────────────────────


def fetch_openalex_deep(query: str, limit: int = 5) -> list[dict]:
    """
    OpenAlex 深挖：论文 + 作者 + 机构 + 引用网络。

    比基础版多提取：作者机构、引用数、开放获取状态、主题概念、DOI。
    报告 §2 的「五层深度」中的元数据层 + 语义层。
    """
    url = ("https://api.openalex.org/works?"
           f"search={urllib.parse.quote(query)}"
           f"&per_page={limit}"
           "&select=id,title,doi,publication_year,cited_by_count,"
           "authorships,primary_location,open_access,concepts,abstract_inverted_index")
    data = _get_json(url)
    if not data:
        return []

    items = []
    for w in data.get("results", []):
        # 作者 + 机构（语义层）
        authors, institutions = [], []
        for a in (w.get("authorships") or [])[:5]:
            name = (a.get("author") or {}).get("display_name")
            if name:
                authors.append(name)
            for inst in (a.get("institutions") or [])[:1]:
                iname = inst.get("display_name")
                if iname and iname not in institutions:
                    institutions.append(iname)

        # 主题概念
        concepts = [c.get("display_name") for c in (w.get("concepts") or [])[:5]
                    if c.get("display_name")]

        # 开放获取状态
        oa = w.get("open_access") or {}
        oa_status = "OA" if oa.get("is_oa") else "closed"

        # 摘要重建（OpenAlex 用倒排索引存摘要）
        abstract = _rebuild_abstract(w.get("abstract_inverted_index"))

        loc = w.get("primary_location") or {}
        src_name = (loc.get("source") or {}).get("display_name", "")

        items.append({
            "title": (w.get("title") or "?")[:150],
            "url": w.get("doi") or w.get("id", ""),
            "source": "openalex",
            "snippet": (
                f"作者: {', '.join(authors[:3])}"
                + (f" | 机构: {institutions[0]}" if institutions else "")
                + f" | {w.get('publication_year','?')} | 引用: {w.get('cited_by_count',0)}"
                + f" | {oa_status}"
                + (f" | {src_name}" if src_name else "")
                + (f"\n摘要: {abstract[:300]}" if abstract else "")
            ),
            "authors": authors,
            "institutions": institutions,
            "concepts": concepts,
            "cited_by": w.get("cited_by_count", 0),
            "year": w.get("publication_year"),
            "open_access": oa.get("is_oa", False),
        })
    return items


def _rebuild_abstract(inverted: dict | None) -> str:
    """从 OpenAlex 倒排索引重建摘要文本。"""
    if not inverted:
        return ""
    positions = []
    for word, idxs in inverted.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


# ─── ② Crossref（OpenAlex 的替代源）────────────


def fetch_crossref(query: str, limit: int = 5) -> list[dict]:
    """
    Crossref：DOI 元数据查询。作为 OpenAlex 的备源。

    报告 §3「来源替代图」：论文元数据 API 失败时可切换 Crossref。
    """
    url = ("https://api.crossref.org/works?"
           f"query={urllib.parse.quote(query)}&rows={limit}"
           "&select=DOI,title,author,issued,container-title,is-referenced-by-count,abstract")
    data = _get_json(url)
    if not data:
        return []

    items = []
    for w in (data.get("message") or {}).get("items", []):
        title = (w.get("title") or ["?"])[0]
        authors = [
            f"{a.get('given','')} {a.get('family','')}".strip()
            for a in (w.get("author") or [])[:3]
        ]
        year = ""
        issued = (w.get("issued") or {}).get("date-parts") or [[]]
        if issued and issued[0]:
            year = issued[0][0]
        journal = (w.get("container-title") or [""])[0]
        abstract = (w.get("abstract") or "")[:300]

        items.append({
            "title": title[:150],
            "url": f"https://doi.org/{w.get('DOI','')}",
            "source": "crossref",
            "snippet": (
                f"作者: {', '.join(authors) if authors else '?'}"
                f" | {year} | 引用: {w.get('is-referenced-by-count', 0)}"
                + (f" | {journal}" if journal else "")
                + (f"\n摘要: {abstract}" if abstract else "")
            ),
            "doi": w.get("DOI", ""),
            "year": year,
        })
    return items


# ─── ③ Wikidata（结构化事实）───────────────────


def fetch_wikidata(query: str, limit: int = 5, lang: str = "zh") -> list[dict]:
    """
    Wikidata：实体搜索（结构化事实层）。

    报告 §1「结构化知识层」：实体消歧、关系查询。
    用 wbsearchentities API（比 SPARQL 轻，适合快速查实体）。
    """
    url = ("https://www.wikidata.org/w/api.php?action=wbsearchentities"
           f"&search={urllib.parse.quote(query)}&language={lang}"
           f"&uselang={lang}&limit={limit}&format=json")
    data = _get_json(url)
    if not data:
        return []

    items = []
    for e in data.get("search", []):
        qid = e.get("id", "")
        label = e.get("label", "?")
        desc = e.get("description", "")
        items.append({
            "title": f"{label}（{qid}）",
            "url": f"https://www.wikidata.org/wiki/{qid}",
            "source": "wikidata",
            "snippet": f"实体类型: {desc}"
                       + (f" | 别名: {', '.join(e.get('aliases', []))}" if e.get("aliases") else ""),
            "qid": qid,
            "description": desc,
        })
    return items


# ─── ④ GitHub 深挖 ────────────────────────────


def fetch_github_deep(query: str, limit: int = 5) -> list[dict]:
    """
    GitHub 仓库深挖（报告 §2 深度：元数据层 + 语义层）。

    比基础版多提取：stars/forks/language/license/last_push/issues/topics/描述。
    """
    url = ("https://api.github.com/search/repositories?"
           f"q={urllib.parse.quote(query)}&sort=stars&order=desc&per_page={limit}")
    data = _get_json(url)
    if not data:
        return []

    items = []
    for r in data.get("items", [])[:limit]:
        topics = ", ".join((r.get("topics") or [])[:5])
        lic = (r.get("license") or {}).get("spdx_id", "")
        items.append({
            "title": f"{r.get('full_name','?')}",
            "url": r.get("html_url", ""),
            "source": "github",
            "snippet": (
                f"⭐{r.get('stargazers_count',0):,} | 🍴{r.get('forks_count',0):,}"
                f" | {r.get('language') or '?'}"
                + (f" | {lic}" if lic else "")
                + f" | issues:{r.get('open_issues_count',0)}"
                + f" | 更新:{str(r.get('pushed_at',''))[:10]}"
                + (f"\n标签: {topics}" if topics else "")
                + f"\n{(r.get('description') or '')[:250]}"
            ),
            "stars": r.get("stargazers_count", 0),
            "forks": r.get("forks_count", 0),
            "language": r.get("language"),
            "license": lic,
            "topics": r.get("topics", []),
            "pushed_at": r.get("pushed_at", ""),
            "open_issues": r.get("open_issues_count", 0),
            "published_days": _days_since(r.get("pushed_at", "")),
        })
    return items


def _days_since(iso_date: str) -> int | None:
    """ISO 日期 → 距今天数。"""
    if not iso_date:
        return None
    try:
        from datetime import datetime, timezone
        d = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - d).days
    except Exception:
        return None


# ─── ⑤ YouTube 深挖 ───────────────────────────


def fetch_youtube_deep(query: str, limit: int = 5) -> list[dict]:
    """
    YouTube 视频深挖（yt-dlp 拿完整元数据）。

    比 trend_scout 提取更多：时长/频道/发布日期/描述/字幕可用性。
    """
    try:
        cmd = ["yt-dlp", "--flat-playlist", "--dump-json",
               "--playlist-end", str(limit), f"ytsearch{limit}:{query}"]
        # 代理（国外站点）
        proxy = _proxy()
        if proxy:
            cmd[1:1] = ["--proxy", proxy]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except Exception:
        return []

    items = []
    for line in r.stdout.strip().split("\n")[:limit]:
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue

        views = d.get("view_count") or 0
        likes = d.get("like_count") or 0
        dur = d.get("duration") or 0
        upload = d.get("upload_date", "")
        days = _days_since(
            f"{upload[:4]}-{upload[4:6]}-{upload[6:8]}T00:00:00Z" if len(upload) == 8 else "")

        def _fmt_dur(s: int) -> str:
            """秒 → H:MM:SS 或 M:SS。"""
            if not s:
                return ""
            h, m, sec = s // 3600, (s % 3600) // 60, s % 60
            return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

        # flat-playlist 模式不返回 like_count，仅在有时显示
        stats = f"频道: {d.get('uploader') or d.get('channel') or '?'}"
        if views:
            stats += f" | 播放: {views:,}"
        if likes:
            stats += f" | 赞: {likes:,}"

        items.append({
            "title": (d.get("title") or "?")[:150],
            "url": d.get("webpage_url") or f"https://youtube.com/watch?v={d.get('id','')}",
            "source": "youtube",
            "snippet": (
                stats
                + (f" | 时长: {_fmt_dur(dur)}" if dur else "")
                + (f" | {upload[:4]}-{upload[4:6]}-{upload[6:8]}" if len(upload) == 8 else "")
                + (f"\n{(d.get('description') or '')[:200]}" if d.get("description") else "")
            ),
            "views": views,
            "likes": likes,
            "duration_s": dur,
            "channel": d.get("uploader") or d.get("channel", ""),
            "published_days": days,
        })
    return items


# ─── 统一接口（供 intel.py 调用）────────────────


def fetch_deep(source: str, query: str, limit: int = 5) -> list[dict]:
    """统一入口：按源名调用。"""
    fns = {
        "openalex_deep": fetch_openalex_deep,
        "crossref": fetch_crossref,
        "wikidata": fetch_wikidata,
        "github": fetch_github_deep,
        "youtube_deep": fetch_youtube_deep,
    }
    fn = fns.get(source)
    if not fn:
        return []
    try:
        return fn(query, limit)
    except Exception as e:
        print(f"  ⚠️ {source}: {str(e)[:80]}")
        return []


# ─── CLI ─────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="深挖数据源")
    p.add_argument("query", help="查询词")
    p.add_argument("--source", default="openalex_deep",
                   choices=["openalex_deep", "crossref", "wikidata"])
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    print(f"🔍 {args.source}: '{args.query}'")
    items = fetch_deep(args.source, args.query, args.limit)

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        for i, it in enumerate(items, 1):
            print(f"\n[{i}] {it['title']}")
            print(f"    {it['snippet'][:200]}")
            print(f"    {it['url'][:90]}")
