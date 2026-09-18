#!/usr/bin/env python3
"""
searchtme_source.py — search-t.me 的 Telegram 频道发现适配器

search-t.me 是 Telegram 频道/群组搜索引擎（多语言，含 **zh 子域**）。
本模块给它做「频道发现层」，补 agent-eye `telegram_source.py` 缺的能力
（此前只能手动找频道）。

能力：
    search(keyword)               → 频道列表 [{handle, name, url}]
    detail(handle)                → 频道详情 {handle, name, subs, desc, url, category}
    discover(keyword, with_detail)→ 搜索 + 前 N 个抓详情（订阅数/描述）

为什么用 JSON-LD 而不是爬 HTML：
    页面内嵌结构化数据，比 CSS 选择器稳得多（改版不易碎）：
      · 列表页 /catalog/all?q=KW → ItemList（position / url / name）
      · 详情页 /channel/HANDLE   → ProfilePage.mainEntity
        （name / url / description / interactionStatistic.userInteractionCount）

覆盖率现实（2026-09-18 实测）：
    ✅ 技术/垂直类中文频道有货（编程 / AI工具 / 跨境电商 均搜到真实频道）
    ❌ 深度小众主题覆盖差（"固态电池" 零结果）
    ⚠️ 结果带噪音（JSON-LD 的 @context/@type 会被裸正则误捉，故按 @type 过滤）
    → 定位：**发现器**，不能当唯一来源；中文覆盖不如俄语生态

合规：只读公开目录页，不登录、不碰用户账号、不带凭据。

用法:
    python3 searchtme_source.py --search 编程
    python3 searchtme_source.py --detail githubtrendinghub
    python3 searchtme_source.py --discover "AI工具" --top 3
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

BASE = "https://zh.search-t.me"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _ld_blocks(html: str) -> list:
    """
    提取页面内所有 JSON-LD 块（已解析成对象）。

    ⚠️ 站点的反爬手段：把 JSON-LD 里的 `schema.org` 替换成 `***`，
    吞掉了收尾引号 → `{"@context":"https://***@type":"WebSite",...}` 是**非法 JSON**，
    直接 json.loads 全失败（实测 2026-09-18，表现为「搜到 0 个频道」）。
    这里先做修复再解析。
    """
    out = []
    for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        s = re.sub(r'"https://\*+', '"https://schema.org",', m)  # 还原被吞的 context
        try:
            out.append(json.loads(s))
        except json.JSONDecodeError:
            continue
    return out


def _iter_typed(blocks: list, type_name: str):
    """遍历所有块里的指定 @type 对象（块可能是 dict 或 list）。"""
    for b in blocks:
        items = b if isinstance(b, list) else [b]
        for it in items:
            if isinstance(it, dict) and it.get("@type") == type_name:
                yield it


def search(keyword: str, limit: int = 20) -> list[dict]:
    """搜频道 → [{handle, name, url}]。

    ⚠️ ListItem 不在顶层，而是嵌在 `ItemList.itemListElement` 里（实测）。
    """
    url = f"{BASE}/catalog/all?q={urllib.parse.quote(keyword)}"
    html = _get(url)
    out, seen = [], set()
    for lst in _iter_typed(_ld_blocks(html), "ItemList"):
        for it in (lst.get("itemListElement") or []):
            if not isinstance(it, dict):
                continue
            m = re.search(r"/channel/([A-Za-z0-9_]{3,40})", it.get("url", "") or "")
            if not m:
                continue
            h = m.group(1)
            if h in seen:
                continue
            seen.add(h)
            out.append({"handle": h, "name": (it.get("name") or "").strip(),
                        "url": f"https://t.me/{h}"})
            if len(out) >= limit:
                return out
    return out


def detail(handle: str) -> dict:
    """频道详情 → {handle, name, subs, desc, url}（缺失字段为空）。"""
    html = _get(f"{BASE}/channel/{handle}")
    for it in _iter_typed(_ld_blocks(html), "ProfilePage"):
        e = it.get("mainEntity") or {}
        subs = (e.get("interactionStatistic") or {}).get("userInteractionCount")
        return {
            "handle": handle,
            "name": (e.get("name") or "").strip(),
            "desc": (e.get("description") or "").strip(),
            "url": e.get("url") or f"https://t.me/{handle}",
            "subs": subs,
        }
    return {"handle": handle, "name": "", "desc": "", "url": f"https://t.me/{handle}", "subs": None}


def discover(keyword: str, top: int = 3, limit: int = 20) -> list[dict]:
    """搜索 + 给前 top 个补详情（订阅数/描述）。"""
    rows = search(keyword, limit=limit)
    for r in rows[:top]:
        try:
            r.update({k: v for k, v in detail(r["handle"]).items() if k != "handle"})
        except Exception as e:  # noqa: BLE001
            r["error"] = f"{type(e).__name__}: {e}"
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="search-t.me Telegram 频道发现")
    ap.add_argument("--search", metavar="KW", help="搜频道关键词")
    ap.add_argument("--detail", metavar="HANDLE", help="看频道详情")
    ap.add_argument("--discover", metavar="KW", help="搜索 + 前 N 个详情")
    ap.add_argument("--top", type=int, default=3, help="discover 时抓几个详情（默认 3）")
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()

    if a.detail:
        d = detail(a.detail)
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0

    if a.discover:
        rows = discover(a.discover, top=a.top, limit=a.limit)
    elif a.search:
        rows = search(a.search, limit=a.limit)
    else:
        ap.print_help()
        return 0

    print(f"找到 {len(rows)} 个频道：")
    for r in rows:
        subs = f"{r['subs']:,}" if isinstance(r.get("subs"), int) else "?"
        desc = (r.get("desc") or "").replace("\n", " ")[:60]
        print(f"  @{r['handle']:<26} {subs:>9} 订阅  {r['name'][:38]}")
        if desc:
            print(f"      {desc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
