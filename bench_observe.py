#!/usr/bin/env python3
"""
bench_observe.py — 观察层对比：Playwright(browser_worker.js) vs camofox-browser

量化两件事：
  1. **token 效率**：同一页面，两种「观察」输出的字符数
     - Playwright 侧：vision.observe() → body_snippet（正文文本）
     - camofox 侧：aria 快照（只含语义元素 + 元素引用 e1/e2）
  2. **可交互元素**：camofox 的元素引用数（refsCount）—— agent 能直接点的东西

这是 Perplexity 报告里「DOM vs AXTree vs 视觉」消融实验的最小版本，
也是 camofox 接入 agent-eye 后判断「值不值」的尺子。

⚠️ camofox 拒绝 file:// 协议（`Blocked URL scheme: file: (only http/https allowed)`），
   所以夹具走**本地 HTTP 服务**（随机端口），两边后端都能访问。

用法:
    python3 bench_observe.py                # 跑对比
    python3 bench_observe.py --save 观察层_v1
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import json
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camofox_client import Camofox, CamofoxError  # noqa: E402

BENCH_DIR = Path(__file__).resolve().parent / ".bench"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """只读静态服务，静默日志。"""

    def log_message(self, *args):  # noqa: D102
        pass


def serve_dir(directory: Path) -> tuple[str, http.server.ThreadingHTTPServer]:
    """在随机端口起本地静态服务器，返回 (base_url, httpd)。"""
    handler = functools.partial(_QuietHandler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


# 测试页：本地夹具（可复现），经本地 HTTP 提供
PAGES = [
    {
        "id": "simple",
        "desc": "简单页（标题+段落+1 链接）",
        "html": ('<html><head><title>Alpha Report 2026</title></head><body>'
                 '<h1>Alpha Report 2026</h1>'
                 '<p>Quantum battery breakthrough shipped in 2026.</p>'
                 '<a href="https://example.com/detail">Read the full report</a>'
                 '</body></html>'),
    },
    {
        "id": "list",
        "desc": "列表页（多条目+多链接）",
        "html": ('<html><head><title>Search Results</title></head><body>'
                 '<h1>Results</h1><ul>' +
                 ''.join(f'<li><a href="/item/{i}">Item {i} — price ¥{i*10}</a></li>'
                         for i in range(1, 9)) +
                 '</ul></body></html>'),
    },
    {
        "id": "form",
        "desc": "表单页（输入+下拉+按钮）",
        "html": ('<html><head><title>Search</title></head><body>'
                 '<h1>Search</h1>'
                 '<form><input type="text" name="q" placeholder="keyword">'
                 '<select name="cat"><option>all</option><option>docs</option></select>'
                 '<button type="submit">Go</button></form>'
                 '<p>Enter a keyword to search the archive.</p>'
                 '</body></html>'),
    },
]


async def observe_playwright(url: str, headless: bool = True) -> dict:
    """用 agent-eye 现有链路（browser_worker.js）观察。"""
    from hand import BrowserSession
    from vision import observe

    t0 = time.time()
    async with BrowserSession(headless=headless) as hand:
        ok, detail = await hand.navigate(url)
        if not ok:
            return {"ok": False, "error": detail, "chars": 0, "interactive": 0,
                    "ms": int((time.time() - t0) * 1000)}
        obs = await observe(hand, url)
    body = obs.get("body_snippet", "") or ""
    # 原始 HTML 字符数 —— 公平对比的第三条基线（aria 快照 vs 完整 HTML）
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            html_chars = len(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        html_chars = 0
    return {
        "ok": True,
        "title": obs.get("title", ""),
        "chars": len(body),
        "html_chars": html_chars,
        "interactive": 0,           # Playwright 侧不产出元素引用
        "text": body,
        "ms": int((time.time() - t0) * 1000),
    }


def observe_camofox(c: Camofox, url: str) -> dict:
    """用 camofox-browser 观察（aria 快照 + 元素引用）。"""
    t0 = time.time()
    tab = None
    try:
        tab = c.create_tab()
        c.navigate(tab, url)
        snap = c.snapshot(tab)
    except CamofoxError as e:
        return {"ok": False, "error": str(e), "chars": 0, "interactive": 0,
                "ms": int((time.time() - t0) * 1000)}
    finally:
        if tab:
            try:
                c.close_tab(tab)
            except CamofoxError:
                pass
    text = snap.get("snapshot", "") or ""
    return {
        "ok": True,
        "chars": len(text),
        "interactive": int(snap.get("refsCount") or 0),
        "truncated": bool(snap.get("truncated")),
        "text": text,
        "ms": int((time.time() - t0) * 1000),
    }


async def main_async(args) -> int:
    c = Camofox()
    if not c.alive():
        print("✗ camofox-browser 没在跑 —— 先执行: bash camofox/start.sh")
        return 2

    tmpdir = Path(tempfile.mkdtemp(prefix="benchobserve_"))
    base_url, httpd = serve_dir(tmpdir)
    rows = []

    print(f"🔬 观察层对比：{len(PAGES)} 个页面 × 2 个后端（夹具走 {base_url}）\n")
    for page in PAGES:
        p = tmpdir / f"{page['id']}.html"
        p.write_text(page["html"], encoding="utf-8")
        url = f"{base_url}/{page['id']}.html"

        pw = await observe_playwright(url)
        cf = observe_camofox(c, url)

        rows.append({"id": page["id"], "desc": page["desc"], "pw": pw, "cf": cf})
        print(f"  [{page['id']}] {page['desc']}")
        print(f"     Playwright : {pw['chars']:>5} 字  {pw['ms']:>5}ms  "
              f"{'✓' if pw['ok'] else '✗ ' + str(pw.get('error',''))[:50]}")
        print(f"     camofox    : {cf['chars']:>5} 字  {cf['ms']:>5}ms  元素引用 {cf['interactive']}  "
              f"{'✓' if cf['ok'] else '✗ ' + str(cf.get('error',''))[:50]}")
        print()

    ok_rows = [r for r in rows if r["pw"]["ok"] and r["cf"]["ok"]]
    pw_chars = [r["pw"]["chars"] for r in ok_rows]
    cf_chars = [r["cf"]["chars"] for r in ok_rows]

    sum_html = sum(r["pw"].get("html_chars", 0) for r in ok_rows)
    metrics = {
        "pages": len(rows),
        "pages_ok": len(ok_rows),
        "pw_text_chars": sum(pw_chars),
        "pw_html_chars": sum_html,
        "cf_total_chars": sum(cf_chars),
        "cf_vs_text_ratio": round(sum(cf_chars) / sum(pw_chars), 3) if sum(pw_chars) else 0,
        "cf_vs_html_ratio": round(sum(cf_chars) / sum_html, 3) if sum_html else 0,
        "cf_interactive_refs": sum(r["cf"]["interactive"] for r in ok_rows),
        "pw_avg_ms": int(statistics.mean([r["pw"]["ms"] for r in ok_rows])) if ok_rows else 0,
        "cf_avg_ms": int(statistics.mean([r["cf"]["ms"] for r in ok_rows])) if ok_rows else 0,
    }

    print("=" * 70)
    print("📊 指标")
    print("-" * 70)
    print(f"  成功页数                {metrics['pages_ok']}/{metrics['pages']}")
    print(f"  ① 纯正文 (Playwright)   {metrics['pw_text_chars']:>6} 字  ← 最省，但不可操作")
    print(f"  ② 原始 HTML             {metrics['pw_html_chars']:>6} 字  ← 最全，但最费")
    print(f"  ③ aria 快照 (camofox)   {metrics['cf_total_chars']:>6} 字  ← 结构 + 可操作")
    print(f"     camofox / 原始HTML    {metrics['cf_vs_html_ratio']:>6}    （<1 = 比 HTML 省）")
    print(f"     camofox / 纯正文      {metrics['cf_vs_text_ratio']:>6}    （>1 = 比纯文本多出结构）")
    print(f"  camofox 元素引用        {metrics['cf_interactive_refs']:>6} 个（可直接 click 的 e1/e2…）")
    print(f"  平均耗时                Playwright {metrics['pw_avg_ms']}ms  /  camofox {metrics['cf_avg_ms']}ms")

    print("\n  示例（camofox 'form' 页快照，注意 [eN] 引用）:")
    for r in rows:
        if r["id"] == "form" and r["cf"]["ok"]:
            for line in r["cf"]["text"].splitlines()[:12]:
                print("   ", line)

    payload = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
               "metrics": metrics,
               "rows": [{k: v for k, v in r.items()} for r in rows]}
    if args.save:
        BENCH_DIR.mkdir(exist_ok=True)
        out = BENCH_DIR / f"baseline_观察层_{args.save}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n💾 已存 {out}")

    httpd.shutdown()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="观察层对比：Playwright vs camofox")
    ap.add_argument("--save", metavar="NAME", help="存档为 baseline_观察层_NAME.json")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
