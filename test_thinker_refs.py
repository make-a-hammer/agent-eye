#!/usr/bin/env python3
"""
test_thinker_refs.py — 验证「元素引用喂给 LLM」的端到端链路

用 **mock LLM** 模拟真实 LLM 的行为：从 prompt 的「可交互元素」列表里读出 ref，
然后决策用该 ref 点击。断言任务真的完成（到达目标页）。

验证四件事：
  1. obs 里有 `interactive`（元素引用表）
  2. `make_user_message` 把它展示给了 LLM
  3. LLM 能用它做决策（selector="e3"）
  4. `hand` 能执行（click(ref=...)）→ 任务完成

前置: bash camofox/start.sh
跑法: python3 test_thinker_refs.py
"""

import asyncio
import functools
import http.server
import json
import re
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop import run_agent  # noqa: E402
from thinker import make_user_message  # noqa: E402

# 导航栏陷阱：第一个 <a> 是 Home，目标是最后一个
PAGE = ('<html><head><title>Nav Trap</title></head><body>'
        '<nav><a href="/">Home</a><a href="/about">About</a></nav>'
        '<h1>Article</h1><p>content here</p>'
        '<a href="target.html">Read the full article</a>'
        '</body></html>')
TARGET = ('<html><head><title>Target</title></head><body>'
          '<h1>Arrived</h1><p>the article body</p></body></html>')


def make_mock_llm(log: list) -> callable:
    """模拟 LLM：从 prompt 的「可交互元素」里读 ref，用它做决策。"""

    def llm(system: str, user: str) -> str:
        log.append(user)
        m = re.search(r'\[(e\d+)\]\s+link:\s*Read the full article', user)
        if m:
            return json.dumps({"action": "navigate", "selector": m.group(1),
                               "reason": "语义定位：用引用表里的 ref"})
        if "the article body" in user:
            return json.dumps({"action": "extract", "content": "the article body",
                               "reason": "已到达目标页"})
        return json.dumps({"action": "done", "reason": "prompt 里没找到引用表"})

    return llm


def test_prompt_shows_refs() -> bool:
    """单元层：make_user_message 是否展示引用表。"""
    obs = {"url": "http://x", "title": "T", "body_snippet": "b",
           "interactive": [{"ref": "e1", "role": "link", "name": "Read more"}]}
    msg = make_user_message(obs, "找文章")
    ok = ("可交互元素" in msg) and ("[e1]" in msg) and ("Read more" in msg)
    print(f"  {'✅' if ok else '❌'} prompt 含引用表（可交互元素 / [e1] / 名字）")
    return ok


async def test_end_to_end() -> bool:
    """端到端：camofox 引擎 + mock LLM → 用 ref 完成任务。"""
    tmp = Path(tempfile.mkdtemp(prefix="refs_"))
    (tmp / "start.html").write_text(PAGE, encoding="utf-8")
    (tmp / "target.html").write_text(TARGET, encoding="utf-8")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D102
            pass

    h = functools.partial(Quiet, directory=str(tmp))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/start.html"

    log: list = []
    try:
        r = await run_agent(url, "找到并阅读全文", llm=make_mock_llm(log),
                            max_steps=3, engine="camofox")
    finally:
        srv.shutdown()

    got_ref = any(re.search(r'\[e\d+\]\s+link:\s*Read the full article', x) for x in log)
    ok = bool(r.get("success")) and "the article body" in (r.get("result") or "")
    print(f"  {'✅' if got_ref else '❌'} prompt 里带了引用表（[eN] link: Read the full article）")
    print(f"  {'✅' if ok else '❌'} 任务完成 success={r.get('success')} "
          f"steps={r.get('steps_taken')}")
    print(f"       轨迹: {[h.get('action') for h in r.get('history', [])]}")
    print(f"       结果: {(r.get('result') or '')[:70]}")
    return ok


async def main() -> int:
    print("🧪 元素引用 → LLM 链路测试\n")
    print("[1/2] 单元层")
    a = test_prompt_shows_refs()
    print("\n[2/2] 端到端（camofox + mock LLM）")
    b = await test_end_to_end()
    print(f"\n{'✅ 全过' if (a and b) else '❌ 有失败'}")
    return 0 if (a and b) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
