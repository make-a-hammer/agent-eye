#!/usr/bin/env python3
"""
live_browse.py — 真实网站端到端（camofox + 真 LLM）

跑一个真实的多步浏览任务，验证整条链路在真实网站上能否工作：
    navigate → LLM 看「可交互元素」→ type 填搜索框 + submit → extract 结果

用法:
    python3 live_browse.py                       # 默认任务（百度搜索）
    python3 live_browse.py --url URL --query Q   # 自定义
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm_client import get_llm  # noqa: E402
from loop import run_agent  # noqa: E402

DEFAULT_URL = "https://www.baidu.com"
DEFAULT_QUERY = "在搜索框输入「固态电池」，提交后提取第一条搜索结果的标题"


async def main(args) -> int:
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
        line = f"  {h.get('step')}. {h.get('action'):<9} {str(h.get('reason', ''))[:62]}"
        print(line)
        if h.get("error"):
            print(f"       ⚠️ {str(h['error'])[:90]}")
    return 0 if r.get("success") else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="真实网站端到端（camofox + 真 LLM）")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--query", default=DEFAULT_QUERY)
    ap.add_argument("--provider", default="kimi", help="LLM provider（默认 kimi）")
    ap.add_argument("--engine", default="camofox", choices=["playwright", "camofox"])
    ap.add_argument("--max-steps", type=int, default=6)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a)))
