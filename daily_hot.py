#!/usr/bin/env python3
"""
daily_hot.py — agent-eye 聚合热榜源（"打工人日报"启示）

启示：聚合型信息源是情报机关的高密度饲料。
借鉴 next-daily-hot（今日热榜）的思路：30+ 平台热榜 = 一个仓库。

本模块直接抓各平台热榜（HTML/公开接口），做成统一 TrendItem。
不依赖 next-daily-hot 的 Next.js 部署——它提供思路，我们自建轻量版。

用法:
    python3 daily_hot.py --list                # 列出可用源
    python3 daily_hot.py --source github       # 抓 GitHub 热榜
    python3 daily_hot.py --source zhihu,weibo  # 多源
"""

import json
import re
import sys
import time
import urllib.request

# ─── 源定义 ──────────────────────────────────────

SOURCES = {
    "github": {
        "name": "GitHub 热榜",
        "url": "https://github.com/trending",
        "type": "html",
    },
    "hackernews": {
        "name": "Hacker News",
        "url": "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=20",
        "type": "json",
    },
    "36kr": {
        "name": "36氪",
        "url": "https://gateway.36kr.com/api/mis/nav/home/nav/rank/hot",
        "type": "json",
    },
    "zhihu": {
        "name": "知乎热榜",
        "url": "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=20",
        "type": "json",
        "auth": True,  # 需要登录 cookie
    },
    "bilibili": {
        "name": "B站热门",
        "url": "https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1",
        "type": "json",
    },
}


# ─── 抓取 ──────────────────────────────────────


def _fetch(url: str, headers: dict | None = None, timeout: int = 15) -> str:
    """抓取页面/接口。"""
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _github_trending() -> list[dict]:
    """GitHub 热榜（HTML 解析，仿 next-daily-hot 思路）。"""
    html = _fetch(SOURCES["github"]["url"])
    items = []
    # 每篇文章是 .Box article.Box-row
    for m in re.finditer(r'<article class="Box-row">(.*?)</article>', html, re.S):
        block = m.group(1)
        title_m = re.search(r'<h2[^>]*>.*?href="/([^"]+)"', block, re.S)
        if not title_m:
            continue
        repo = title_m.group(1)
        desc_m = re.search(r'<p class="col-9[^"]*">\s*(.*?)\s*</p>', block, re.S)
        desc = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip() if desc_m else ""
        stars_m = re.search(r'aria-label="([\d,]+) stars"', block)
        stars = int(stars_m.group(1).replace(",", "")) if stars_m else 0
        items.append({
            "title": repo,
            "url": f"https://github.com/{repo}",
            "desc": desc[:200],
            "stars": stars,
            "source": "github",
        })
    return items


def _hackernews() -> list[dict]:
    """Hacker News 首页（官方 API）。"""
    data = json.loads(_fetch(SOURCES["hackernews"]["url"]))
    items = []
    for hit in data.get("hits", []):
        items.append({
            "title": hit.get("title", ""),
            "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
            "points": hit.get("points", 0),
            "comments": hit.get("num_comments", 0),
            "source": "hackernews",
        })
    return items


def _bilibili() -> list[dict]:
    """B站热门（公共 API，无需登录）。"""
    try:
        data = json.loads(_fetch(SOURCES["bilibili"]["url"]))
        items = []
        for v in data.get("data", {}).get("list", []):
            items.append({
                "title": v.get("title", ""),
                "url": f"https://www.bilibili.com/video/{v.get('bvid')}",
                "views": v.get("stat", {}).get("view", 0),
                "desc": v.get("desc", "")[:100],
                "source": "bilibili",
            })
        return items
    except Exception:
        return []


def _zhihu() -> list[dict]:
    """知乎热榜（官方 API，通常免登录）。"""
    try:
        data = json.loads(_fetch(SOURCES["zhihu"]["url"]))
        items = []
        for entry in data.get("data", []):
            target = entry.get("target", {})
            items.append({
                "title": target.get("title", ""),
                "url": f"https://www.zhihu.com/question/{target.get('id')}",
                "heat": entry.get("detail_text", ""),
                "source": "zhihu",
            })
        return items
    except Exception:
        return []


def _36kr() -> list[dict]:
    """36氪热榜。"""
    try:
        data = json.loads(_fetch(SOURCES["36kr"]["url"]))
        items = []
        for entry in data.get("data", {}).get("hotRankList", []):
            items.append({
                "title": entry.get("templateMaterial", {}).get("widgetTitle", ""),
                "url": f"https://36kr.com/p/{entry.get('itemId')}",
                "source": "36kr",
            })
        return items
    except Exception:
        return []


# ─── 统一转换 ──────────────────────────────────────


def to_trend_items(raw_items: list[dict]) -> list[dict]:
    """统一转 trend_scout 格式。"""
    out = []
    for it in raw_items:
        score = 50
        reason = it.get("source", "")
        if it.get("stars"):
            score += min(30, it["stars"] // 100)
            reason += f" ⭐{it['stars']}"
        if it.get("points"):
            score += min(30, it["points"] // 10)
            reason += f" ↑{it['points']}分"
        if it.get("heat"):
            reason += f" {it['heat']}"
        out.append({
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "platform": "daily_hot",
            "source": it.get("source", ""),
            "views": it.get("stars", 0) or it.get("points", 0) or 0,
            "move_score": min(95, score),
            "confidence": 0.7,
            "reason": reason.strip() or "热榜",
        })
    return out


# ─── CLI ──────────────────────────────────────

FETCHERS = {
    "github": _github_trending,
    "hackernews": _hackernews,
    "zhihu": _zhihu,
    "36kr": _36kr,
    "bilibili": _bilibili,
}


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--list" in args:
        for k, v in SOURCES.items():
            print(f"{k:12s} — {v['name']}")
        sys.exit(0)

    sources_arg = []
    if "--source" in args:
        i = args.index("--source")
        sources_arg = args[i + 1].split(",")

    targets = sources_arg or list(FETCHERS.keys())
    all_items = []
    for name in targets:
        if name not in FETCHERS:
            print(f"⚠️ 未知源: {name}（用 --list 查看）")
            continue
        print(f"📡 {SOURCES[name]['name']}...")
        try:
            raw = FETCHERS[name]()
            items = to_trend_items(raw)
            print(f"   ✅ {len(items)} 条")
            all_items.extend(items)
        except Exception as e:
            print(f"   ❌ {e}")
        time.sleep(1)  # 节流

    print(f"\n{'='*60}")
    print(f"  🎯 聚合热榜 — {len(all_items)} 条")
    print(f"{'='*60}")
    for it in sorted(all_items, key=lambda x: x["move_score"], reverse=True)[:30]:
        print(f"[{it['move_score']:>2}分] [{it['source']}] {it['title'][:60]}")
        print(f"        {it['url'][:80]}")

    if "--json" in args:
        print(json.dumps(all_items, ensure_ascii=False, indent=2))
