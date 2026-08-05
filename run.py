#!/usr/bin/env python3
"""
run.py — agent-eye v2 一键运行入口

用法:
    python3 run.py "你想找什么" [URL]
    python3 run.py "找 Transformer 论文" https://arxiv.org/abs/1706.03762
"""

import asyncio
import sys
from loop import run_agent
from llm_client import create_llm


async def main():
    if len(sys.argv) < 2:
        print("用法: python3 run.py <查询> [URL]")
        print("示例: python3 run.py '找 AI 相关文章'")
        print("      python3 run.py '找论文' https://arxiv.org/abs/1706.03762")
        sys.exit(1)

    query = sys.argv[1]
    start_url = sys.argv[2] if len(sys.argv) > 2 else None

    if not start_url:
        from sources import generate_search_urls, _infer_source_types
        types = _infer_source_types(query)
        if "paper" in types:
            # 学术查询直接走 arXiv 网页搜索
            from urllib.parse import quote
            start_url = f"https://arxiv.org/search/?query={quote(query)}&searchtype=all"
        else:
            urls = generate_search_urls(query)
            start_url = urls[0] if urls else f"https://lite.duckduckgo.com/lite/?q={quote(query)}"
        print(f"   自动选择源: {start_url[:80]}...")

    print(f"🔍 agent-eye v2")
    print(f"   查询: {query}")
    print(f"   起始: {start_url}")
    print(f"   模型: DeepSeek")
    print()

    llm = create_llm(provider="deepseek")

    result = await run_agent(
        start_url=start_url,
        query=query,
        llm=llm,
        max_steps=10,
        headless=True,
    )

    print(f"{'✅ 成功' if result['success'] else '⏹️ 结束'}")
    print(f"   步数: {result['steps_taken']}")
    print()

    if result["success"]:
        print("📥 提取内容:")
        print(result["result"])
    else:
        print(f"   原因: {result['result']}")

    print()
    print("📋 决策历史:")
    for h in result["history"]:
        print(f"   [{h['step']}] {h['action']:8s} — {h['reason']}")


if __name__ == "__main__":
    asyncio.run(main())
