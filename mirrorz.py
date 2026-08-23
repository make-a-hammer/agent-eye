#!/usr/bin/env python3
"""
mirrorz.py — 教育网镜像源查询器（agent-eye 数据源）

原理: 各高校镜像站提供标准 mirrorz.json，本模块拉取聚合后按软件查站点。

用法:
    python3 mirrorz.py pypi                 # 查 pypi 有哪些教育网镜像
    python3 mirrorz.py ubuntu               # 查 ubuntu 镜像
    python3 mirrorz.py --speed pypi         # 测速并推荐最快
    python3 mirrorz.py --list               # 列出所有软件名
    python3 mirrorz.py --json pypi          # JSON 输出
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import socket

# 数据源: 提供 mirrorz.json 的高校站点（失效自动跳过）
SOURCES = [
    "https://mirrors.ustc.edu.cn/static/json/mirrorz.json",      # 中科大
    "https://mirrors.zju.edu.cn/api/mirrorz.json",               # 浙大
    "https://mirror.sjtu.edu.cn/mirrorz/siyuan.json",            # 上交
    "https://mirrors.nju.edu.cn/.mirrorz/site.json",             # 南大
    "https://mirrors.sustech.edu.cn/mirrorz/mirrorz.json",       # 南科大
    "https://mirrors.bfsu.edu.cn/static/status/disk.json",       # 北外
]

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mirrorz_cache.json")
CACHE_TTL = 3600  # 缓存 1 小时


def _fetch(url: str, timeout: int = 12) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def load_all(refresh: bool = False) -> list[dict]:
    """拉取/缓存所有站点的 mirrorz 数据。返回 [{site, mirrors:[...]}]"""
    if not refresh and os.path.exists(CACHE_FILE):
        age = time.time() - os.path.getmtime(CACHE_FILE)
        if age < CACHE_TTL:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)

    all_data = []
    for url in SOURCES:
        raw = _fetch(url)
        if not raw:
            print(f"  ⚠️ 跳过 {url.split('/')[2]}")
            continue
        try:
            d = json.loads(raw)
            if isinstance(d, list):
                # 数组格式: [{name,url,cname,...}] 直接当 mirrors
                mirrors = d
                site_abbr = url.split("/")[2]
                site_url = f"https://{site_abbr}"
            else:
                mirrors = d.get("mirrors") or []
                site = d.get("site") or {}
                site_abbr = site.get("abbr", url.split("/")[2])
                site_url = site.get("url", f"https://{site_abbr}")
            all_data.append({"site": site_abbr, "site_url": site_url, "mirrors": mirrors})
        except json.JSONDecodeError:
            continue

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False)
    return all_data


def list_softwares(all_data: list[dict] | None = None) -> list[str]:
    """列出所有被镜像的软件名。"""
    all_data = all_data or load_all()
    names = set()
    for d in all_data:
        for m in d["mirrors"]:
            cname = m.get("cname") or m.get("name") or ""
            if cname:
                names.add(cname)
    return sorted(names)


def find_sites(software: str, all_data: list[dict] | None = None) -> list[dict]:
    """按软件名（大小写不敏感，支持中英文）查所有镜像站点。"""
    all_data = all_data or load_all()
    sw = software.lower()
    results = []
    for d in all_data:
        site = d["site"]
        site_url = (d.get("site_url") or "").rstrip("/")
        for m in d["mirrors"]:
            name = (m.get("name") or "").lower()
            cname = m.get("cname") or ""
            path = m.get("url") or m.get("path") or ""
            url = path if path.startswith("http") else f"{site_url}{path}"
            if sw in name or sw in cname.lower() or sw in url.lower():
                results.append({
                    "site": site,
                    "software": m.get("name", cname),
                    "cname": cname,
                    "url": url,
                    "status": m.get("status", "?") or "?",
                })
    return results


def speedtest(url: str, timeout: int = 8) -> float | None:
    """测速: 返回响应时间（秒），失败返回 None。"""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(500)
        return time.time() - t0
    except Exception:
        return None


def recommend(software: str, all_data: list[dict] | None = None) -> list[dict]:
    """查站点 + 测速，按速度排序推荐。"""
    sites = find_sites(software, all_data)
    if not sites:
        return []
    for s in sites:
        s["speed"] = speedtest(s["url"])
    sites.sort(key=lambda x: (x["speed"] is None, x["speed"] or 999))
    return sites


# ─── 输出 ──────────────────────────────────────


def print_sites(sites: list[dict], software: str):
    if not sites:
        print(f"❌ 没找到 '{software}' 的镜像（试试 --list 看全部软件名）")
        return
    print(f"\n📦 '{software}' 教育网镜像站点（{len(sites)} 个）:")
    print(f"{'站点':<10} {'软件':<20} {'速度':>8}  地址")
    print("-" * 70)
    for s in sites[:12]:
        spd = f"{s['speed']:.2f}s" if s.get("speed") is not None else "超时"
        mark = "🏆" if s.get("speed") is not None and s["speed"] < 1 else ""
        print(f"{s['site']:<10} {s['software'][:20]:<20} {spd:>8}  {mark} {s['url'][:50]}")


def main():
    p = argparse.ArgumentParser(description="教育网镜像源查询器")
    p.add_argument("software", nargs="?", help="软件名（pypi/ubuntu/anaconda/node...）")
    p.add_argument("--list", action="store_true", help="列出所有软件")
    p.add_argument("--speed", action="store_true", help="测速推荐")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--refresh", action="store_true", help="强制刷新缓存")
    args = p.parse_args()

    if args.list:
        names = list_softwares()
        if args.json:
            print(json.dumps(names, ensure_ascii=False))
        else:
            print(f"共 {len(names)} 个软件源:")
            print("、".join(names))
        return

    if not args.software:
        p.print_help()
        return

    print(f"🔍 查询 '{args.software}' 教育网镜像...")
    all_data = load_all(refresh=args.refresh)
    sites = recommend(args.software, all_data) if args.speed else find_sites(args.software, all_data)

    if args.json:
        print(json.dumps(sites, ensure_ascii=False, indent=2))
    else:
        print_sites(sites, args.software)


if __name__ == "__main__":
    main()
