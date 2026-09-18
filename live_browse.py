#!/usr/bin/env python3
"""
live_browse.py — 真实网站基线（camofox + 真 LLM）

两层用法：
    python3 live_browse.py                       # 单任务（baidu 搜索）
    python3 live_browse.py --suite               # 真实站套件（5 站）
    python3 live_browse.py --url URL --query Q   # 自定义

为什么要有这一层：**自建夹具任务再多，也测不出真实网站的复杂度**
（反爬、动态渲染、导航栏噪音、内容随时变）。真实站是唯一能回答
「到底什么水平」的地方。

站点可达性实测（2026-09-18，国内直连）：
  ✅ 百度 / cn.bing.com / 豆瓣 / 人民网 / CSDN / news.qq.com
  ❌ github.com（需代理）  ⚠️ 知乎（302 重定向）

LLM 每步 20-60s，整轮套件约 5-10 分钟 —— 建议后台跑。
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm_client import get_llm  # noqa: E402
from loop import run_agent  # noqa: E402

BENCH_DIR = Path(__file__).resolve().parent / ".bench"

DEFAULT_URL = "https://www.baidu.com"
DEFAULT_QUERY = "在搜索框输入「固态电池」，提交后提取第一条自然搜索结果的标题"

# ── 真实站套件 ────────────────────────────────────────────────
# assert: min_len = 结果至少多长才算拿到了东西（真实站内容会变，故只做宽松断言）
LIVE_SUITE = [
    {
        "id": "live_baidu_search",
        "desc": "百度：填搜索框 + 提交 + 提取（含广告筛选）",
        "url": "https://www.baidu.com",
        "query": "在搜索框输入「固态电池」，提交后提取第一条自然搜索结果的标题",
        "min_len": 8,
        "human_steps": 3,
        "max_steps": 5,
    },
    {
        "id": "live_bing_search",
        "desc": "必应中国：填搜索框 + 提交 + 提取",
        "url": "https://cn.bing.com",
        "query": "在搜索框输入「光伏 装机量」，提交后提取第一条搜索结果的标题",
        "min_len": 8,
        "human_steps": 3,
        "max_steps": 5,
    },
    {
        "id": "live_people_news",
        "desc": "人民网：首页提取第一条新闻标题",
        "url": "http://www.people.com.cn/",
        "query": "提取页面上第一条新闻的标题",
        "min_len": 6,
        "human_steps": 1,
        "max_steps": 3,
    },
    {
        "id": "live_csdn",
        "desc": "CSDN（反爬站）：提取页面上第一条文章的标题",
        "url": "https://www.csdn.net",
        "query": "提取页面上第一条文章的标题",
        "min_len": 6,
        "human_steps": 1,
        "max_steps": 3,
    },
    {
        "id": "live_qq_news",
        "desc": "腾讯新闻：首页提取一条新闻标题",
        "url": "https://news.qq.com",
        "query": "提取页面上第一条新闻的标题",
        "min_len": 6,
        "human_steps": 1,
        "max_steps": 3,
    },
]


async def run_suite(args) -> int:
    try:
        llm = get_llm(args.provider)
    except Exception as e:  # noqa: BLE001
        print(f"✗ LLM 不可用: {e}")
        return 2

    print(f"🌐 真实站套件：{len(LIVE_SUITE)} 站（engine={args.engine}, llm={args.provider}）\n")
    results = []
    for i, t in enumerate(LIVE_SUITE, 1):
        t0 = time.time()
        try:
            r = await run_agent(t["url"], t["query"], llm=llm,
                                max_steps=t.get("max_steps", 5), engine=args.engine)
        except Exception as e:  # noqa: BLE001
            r = {"success": False, "result": f"EXCEPTION {type(e).__name__}: {e}",
                 "steps_taken": 0, "history": []}
        wall = int((time.time() - t0) * 1000)
        res = (r.get("result") or "")
        ok = bool(r.get("success")) and len(res.strip()) >= t["min_len"]
        flag = "✅" if ok else "❌"
        print(f"  {flag} [{i}/{len(LIVE_SUITE)}] {t['id']:<20} {r.get('steps_taken')} 步 "
              f"{wall/1000:.0f}s  human={t['human_steps']}")
        print(f"      {res[:110].replace(chr(10), ' ')}")
        results.append({
            "id": t["id"], "desc": t["desc"], "url": t["url"],
            "success": ok, "agent_success": bool(r.get("success")),
            "steps": r.get("steps_taken", 0), "human_steps": t["human_steps"],
            "wall_ms": wall, "result": res[:200].replace("\n", " "),
        })

    n_ok = sum(1 for r in results if r["success"])
    ok_rows = [r for r in results if r["success"]]
    h = sum(r["human_steps"] for r in ok_rows)
    a = sum(r["steps"] for r in ok_rows)
    metrics = {
        "sites": len(results),
        "live_success_rate": round(n_ok / len(results), 3) if results else 0.0,
        "success_n": f"{n_ok}/{len(results)}",
        "human_ratio": round(a / h, 3) if h else 0.0,
        "avg_wall_ms": int(statistics.mean([r["wall_ms"] for r in results])) if results else 0,
    }
    print("\n" + "=" * 70)
    print(f"📊 真实站通过 {metrics['success_n']}   human_ratio {metrics['human_ratio']}"
          f"   平均 {metrics['avg_wall_ms']/1000:.1f}s/站")

    if args.save:
        BENCH_DIR.mkdir(exist_ok=True)
        out = BENCH_DIR / f"baseline_真实站_{args.save}.json"
        out.write_text(json.dumps(
            {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "engine": args.engine,
             "provider": args.provider, "metrics": metrics, "results": results},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"💾 已存 {out}")
    return 0 if n_ok == len(results) else 1


async def run_one(args) -> int:
    try:
        llm = get_llm(args.provider)
    except Exception as e:  # noqa: BLE001
        print(f"✗ LLM 不可用: {e}")
        return 2

    print(f"🌐 真实网站端到端（engine={args.engine}, llm={args.provider}）")
    print(f"   起始: {args.url}")
    print(f"   任务: {args.query}\n")

    r = await run_agent(args.url, args.query, llm=llm,
                        max_steps=args.max_steps, engine=args.engine)

    print(f"success : {r.get('success')}")
    print(f"steps   : {r.get('steps_taken')}")
    print(f"stop    : {r.get('stop_reason', '-')}")
    print(f"result  : {(r.get('result') or '')[:400]}\n")
    print("轨迹:")
    for h in r.get("history", []):
        print(f"  {h.get('step')}. {h.get('action'):<9} {str(h.get('reason', ''))[:62]}")
        if h.get("error"):
            print(f"       ⚠️ {str(h['error'])[:90]}")
    return 0 if r.get("success") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="真实网站基线（camofox + 真 LLM）")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--query", default=DEFAULT_QUERY)
    ap.add_argument("--provider", default="kimi", help="LLM provider（默认 kimi）")
    ap.add_argument("--engine", default="camofox", choices=["playwright", "camofox"])
    ap.add_argument("--max-steps", type=int, default=5)
    ap.add_argument("--suite", action="store_true", help="跑真实站套件（5 站）")
    ap.add_argument("--save", metavar="NAME", help="套件存档名")
    a = ap.parse_args()
    return asyncio.run(run_suite(a) if a.suite else run_one(a))


if __name__ == "__main__":
    sys.exit(main())
