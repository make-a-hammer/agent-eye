#!/usr/bin/env python3
"""
api_map.py — agent-eye API 弹药目录（public-apis 启示）

public-apis (46万star) 是 1400+ 免费 API 的大全。本模块把它变成
agent-eye 的数据源发现器：按关键词/分类检索 API，返回可接入的源。

用法:
    python3 api_map.py --list                 # 统计分类
    python3 api_map.py --search news          # 按关键词搜 API
    python3 api_map.py --category Finance     # 按分类搜
    python3 api_map.py --all --json           # 全量 JSON
"""

import json
import os
import re
import sys
import urllib.request

# ─── 数据获取 ──────────────────────────────────────

DATA_URL = "https://raw.githubusercontent.com/public-apis/public-apis/master/README.md"
CACHE_FILE = os.path.join(os.path.dirname(__file__), ".api_map_cache.json")


def _fetch_readme() -> str:
    """抓取 public-apis README（含完整 API 列表）。"""
    req = urllib.request.Request(DATA_URL, headers={"User-Agent": "agent-eye/2.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_api_list(refresh: bool = False) -> list[dict]:
    """解析 README → API 列表。带本地缓存。"""
    if os.path.exists(CACHE_FILE) and not refresh:
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    text = _fetch_readme()
    apis = _parse_readme(text)

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(apis, f, ensure_ascii=False, indent=1)
    return apis


def _parse_readme(text: str) -> list[dict]:
    """解析 README 的 API 表格格式。

    格式: | 名称 | 描述 | Auth | HTTPS | CORS |
    分类标题: ## Animals（后跟表格）
    """
    apis = []
    category = ""

    lines = text.split("\n")
    for i, line in enumerate(lines):
        # 分类标题: ### Animals（三级标题，后跟表格）
        m = re.match(r"^#{2,3}\s+([A-Za-z][A-Za-z &']+)$", line)
        if m:
            # 下一行必须是表头（API | ...）才算是分类
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt.startswith("API |") or nxt.startswith("| API"):
                category = m.group(1).strip()
            continue
        # 表行: | Dogs | ... |
        if line.startswith("| ") and "|" in line[2:]:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 5 and cells[0] and cells[0] != "API":
                # 跳过表分隔线（|:---|）
                if cells[0].startswith(":") or set(cells[0]) == {"-"} or set(cells[0]) == {":"}:
                    continue
                name, desc, auth, https, cors = cells[0], cells[1], cells[2], cells[3], cells[4]
                # 链接提取
                link = ""
                m2 = re.search(r'\[[^\]]*\]\((https?://[^)\s]+)\)', name)
                if m2:
                    link = m2.group(1)
                    name = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', name)
                apis.append({
                    "name": name,
                    "description": desc,
                    "auth": auth,
                    "https": https,
                    "cors": cors,
                    "category": category,
                    "url": link,
                })
    return apis


# ─── 检索 ──────────────────────────────────────


def search_apis(keyword: str, apis: list[dict], limit: int = 20) -> list[dict]:
    """按关键词在名称+描述+分类中检索。"""
    kw = keyword.lower()
    hits = []
    for a in apis:
        haystack = f"{a['name']} {a['description']} {a['category']}".lower()
        if kw in haystack:
            hits.append(a)
    # 按相关性粗排：名称命中 > 描述命中
    hits.sort(key=lambda a: (
        kw in a["name"].lower(),
        kw in a["category"].lower(),
    ), reverse=True)
    return hits[:limit]


def category_apis(category: str, apis: list[dict]) -> list[dict]:
    """按分类取 API。"""
    c = category.lower()
    return [a for a in apis if a["category"].lower() == c]


def list_categories(apis: list[dict]) -> dict:
    """统计各分类 API 数量。"""
    counts = {}
    for a in apis:
        counts[a["category"]] = counts.get(a["category"], 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def to_trend_items(apis: list[dict]) -> list[dict]:
    """转 trend_scout 格式（数据源候选）。"""
    items = []
    for a in apis:
        items.append({
            "title": a["name"],
            "url": a.get("url", ""),
            "platform": "api_map",
            "source": a["category"],
            "views": 0,
            "move_score": 60 if a.get("auth") in ("", "No", "no", None) else 45,
            "confidence": 0.8,
            "reason": f"{a['description'][:80]} | auth={a.get('auth','?')} https={a.get('https','?')}",
        })
    return items


# ─── CLI ──────────────────────────────────────


if __name__ == "__main__":
    args = sys.argv[1:]
    refresh = "--refresh" in args

    try:
        apis = load_api_list(refresh=refresh)
    except Exception as e:
        print(f"❌ 拉取失败: {e}")
        sys.exit(1)

    print(f"📦 API 目录加载: {len(apis)} 个 API\n")

    if "--list" in args:
        for cat, n in list_categories(apis).items():
            print(f"  {cat:16s} {n:>4} 个")
        sys.exit(0)

    if "--category" in args:
        i = args.index("--category")
        cat = args[i + 1]
        items = category_apis(cat, apis)
        print(f"=== {cat} ({len(items)} 个) ===")
        for a in items[:25]:
            auth = "🔓" if a["auth"] == "No" else "🔒"
            print(f"  {auth} {a['name']} — {a['description'][:60]}")
        sys.exit(0)

    if "--search" in args:
        i = args.index("--search")
        kw = args[i + 1]
        items = search_apis(kw, apis)
        print(f"=== 搜索 '{kw}' ({len(items)} 个) ===")
        for a in items:
            auth = "🔓" if a["auth"] in ("", "No") else "🔒"
            print(f"  {auth} [{a['category']}] {a['name']}")
            print(f"       {a['description'][:70]}")
            if a.get("url"):
                print(f"       {a['url']}")
        sys.exit(0)

    if "--all" in args or "--json" in args:
        print(json.dumps(apis, ensure_ascii=False, indent=1))
        sys.exit(0)

    print("用法:")
    print("  python3 api_map.py --list                # 统计分类")
    print("  python3 api_map.py --search news         # 按关键词搜")
    print("  python3 api_map.py --category Finance    # 按分类搜")
    print("  python3 api_map.py --search 天气 --json  # JSON 输出")
