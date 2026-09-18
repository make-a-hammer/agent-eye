#!/usr/bin/env python3
"""
analyze_xianyu.py — 闲鱼实测数据对比分析

读取 xianyu_<关键词>.tsv（gf_search.js 产物），输出品类对比表。

信号解读（按 xianyu-scout 技能的信号强度排序）：
    强信号  = 高价商品仍有高想要数（≥10元 还有想要 → 付费意愿）
    中信号  = 想要数绝对值
    弱信号  = 1元商品的想要数（收藏噪音）
"""

import csv
import glob
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def load(path: str) -> list[tuple[float, int, str]]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            try:
                p = float(row.get("price") or 0)
            except ValueError:
                p = 0.0
            try:
                w = int(float(row.get("want") or 0))
            except ValueError:
                w = 0
            rows.append((p, w, (row.get("title") or "").strip()))
    return rows


def analyze(rows: list[tuple[float, int, str]]) -> dict:
    if not rows:
        return {}
    prices = [p for p, _, _ in rows if p > 0]
    wants = [w for _, w, _ in rows]
    total_want = sum(wants)
    n = len(rows)

    # 高价档的付费意愿（强信号）
    paid = [(p, w) for p, w, _ in rows if p >= 10 and w > 0]
    paid_want = sum(w for _, w in paid)

    return {
        "n": n,                                    # 采样商品数
        "total_want": total_want,                  # 总想要数
        "avg_want": round(total_want / n, 1),      # 平均想要
        "max_want": max(wants) if wants else 0,    # 单品最高想要
        "price_med": sorted(prices)[len(prices)//2] if prices else 0,
        "price_max": max(prices) if prices else 0,
        "paid_n": len(paid),                       # ≥10元且有想要的商品数
        "paid_want": paid_want,                    # 高价档贡献的想要数
        "paid_ratio": round(100 * paid_want / total_want, 1) if total_want else 0,
        "index": round(total_want / n, 2),         # 需求指数 = 总想要/商品数
    }


def main():
    files = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "xianyu_*.tsv")))
    if len(sys.argv) > 1:
        files = [f for f in files if any(a in f for a in sys.argv[1:])]
    if not files:
        print("未找到 xianyu_*.tsv")
        return

    results = {}
    for f in files:
        kw = os.path.basename(f).replace("xianyu_", "").replace(".tsv", "")
        rows = load(f)
        st = analyze(rows)
        if st:
            results[kw] = (st, rows)

    # 按需求指数排序
    order = sorted(results.items(), key=lambda kv: -kv[1][0]["index"])

    print("=" * 108)
    print(f"{'关键词':<14}{'商品':>5}{'总想要':>8}{'均想要':>7}{'最高':>6}"
          f"{'价中位':>7}{'最高价':>7}{'≥10元中':>8}{'高价占比':>9}{'指数':>7}")
    print("-" * 108)
    for kw, (st, _) in order:
        print(f"{kw:<14}{st['n']:>5}{st['total_want']:>8}{st['avg_want']:>7}"
              f"{st['max_want']:>6}{st['price_med']:>7.0f}{st['price_max']:>7.0f}"
              f"{st['paid_n']:>8}{st['paid_ratio']:>8.1f}%{st['index']:>7.2f}")

    # 高价档 TOP 商品（付费意愿最硬的证据）
    print("\n" + "=" * 108)
    print("高价档 TOP15（价格 ≥10 元且想要数最高 —— 强信号）")
    print("-" * 108)
    paid_all = []
    for kw, (_, rows) in results.items():
        for p, w, t in rows:
            if p >= 10 and w > 0:
                paid_all.append((w, p, kw, t))
    paid_all.sort(reverse=True)
    for w, p, kw, t in paid_all[:15]:
        print(f"  [{kw}] ¥{p:.0f} {w:>4}想要  {t[:62]}")


if __name__ == "__main__":
    main()
