#!/usr/bin/env python3
"""
scihub_source.py — agent-eye Sci-Hub 数据源

学术论文获取（Track A 代找文献链路）。
Sci-Hub 有 Cloudflare 浏览器验证，走 Node.js worker 渲染抓取。

用法:
    python3 scihub_source.py 10.1038/nature14539        # 按 DOI 取论文
    python3 scihub_source.py --search "machine learning"  # 按标题搜（弱）

环境变量:
    SCIHUB_MIRROR — 镜像域名，默认 sci-hub.wf
"""

import json
import os
import re
import subprocess
import sys

MIRRORS = ["sci-hub.wf", "sci-hub.se", "sci-hub.ru", "sci-hub.st", "sci-hub.ren"]
MIRROR = os.environ.get("SCIHUB_MIRROR", "sci-hub.wf")

WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "browser_worker.js")


def _via_worker(url: str, timeout: int = 30) -> str:
    """通过 Node.js worker 渲染页面，绕过 Cloudflare 浏览器验证。"""
    worker = subprocess.Popen(
        ["node", WORKER_SCRIPT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
        env=os.environ.copy(),
    )
    try:
        # 等 ready
        ready = worker.stdout.readline()
        # 导航
        worker.stdin.write(json.dumps({"action": "navigate", "url": url, "timeout": timeout * 1000}) + "\n")
        worker.stdin.flush()
        nav = worker.stdout.readline()
        # 提取 HTML
        worker.stdin.write(json.dumps({"action": "extract", "raw_html": True}) + "\n")
        worker.stdin.flush()
        resp = worker.stdout.readline()
        worker.stdin.write(json.dumps({"action": "exit"}) + "\n")
        worker.stdin.flush()
        return resp
    finally:
        try:
            worker.kill()
        except Exception:
            pass


def fetch_by_doi(doi: str) -> dict | None:
    """按 DOI 获取论文信息 + PDF 链接。"""
    doi = doi.strip().lower()
    if not doi.startswith("10."):
        return None

    for mirror in [MIRROR] + [m for m in MIRRORS if m != MIRROR]:
        url = f"https://{mirror}/{doi}"
        raw = _via_worker(url)
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not data.get("ok"):
            continue
        html = data.get("html", "") or data.get("body", "")
        if not html or "Checking your browser" in html:
            continue

        title = _extract_title(html)
        pdf_url = _extract_pdf_url(html)
        if not pdf_url:
            continue

        return {
            "doi": doi,
            "title": title,
            "pdf_url": pdf_url if pdf_url.startswith("http") else f"https://{mirror}{pdf_url}",
            "mirror": mirror,
            "source": "scihub",
            "move_score": 85,  # 学术需求稳定
            "confidence": 0.85,
            "reason": f"Sci-Hub 命中 DOI {doi}",
        }
    return None


def _extract_title(html: str) -> str:
    m = re.search(r'<title>(.*?)</title>', html, re.S)
    if m:
        t = m.group(1).strip()
        return t[:200]
    m = re.search(r'id="citation"[^>]*>.*?<h1[^>]*>(.*?)</h1>', html, re.S)
    return m.group(1).strip()[:200] if m else ""


def _extract_pdf_url(html: str) -> str | None:
    # iframe/embed src
    for m in re.finditer(r'(?:iframe|embed)[^>]*src=["\']([^"\']+)', html, re.I):
        src = m.group(1)
        if src.endswith(".pdf") or "pdf" in src.lower() or "download" in src.lower():
            return src
    # 直接 .pdf 链接
    for m in re.finditer(r'https?://[^"\'\s<>]+\.pdf[^"\'\s<>]*', html, re.I):
        return m.group(0)
    return None


def to_trend_items(papers: list[dict]) -> list[dict]:
    """转 trend_scout 标准格式（学术源）。"""
    items = []
    for p in papers:
        if not p:
            continue
        items.append({
            "title": p.get("title", p.get("doi", "")),
            "url": p.get("pdf_url", ""),
            "platform": "scihub",
            "source": p.get("source", "scihub"),
            "views": 0,
            "move_score": p.get("move_score", 80),
            "confidence": p.get("confidence", 0.8),
            "reason": p.get("reason", "学术论文"),
            "doi": p.get("doi", ""),
        })
    return items


def fetch_by_title(title: str) -> list[dict]:
    """按标题弱搜索——返回空（Sci-Hub 无搜索，需 DOI）。"""
    print(f"  ⚠️ Sci-Hub 不支持标题搜索。请先查 DOI（Crossref: https://api.crossref.org/works?query={title}）")
    return []


# ─── CLI ──────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 scihub_source.py <DOI>")
        print("示例: python3 scihub_source.py 10.1038/nature14539")
        sys.exit(1)

    arg = sys.argv[1].strip()
    if arg.startswith("10."):
        paper = fetch_by_doi(arg)
        if paper:
            print(f"✅ {paper['title'][:80]}")
            print(f"   DOI: {paper['doi']}")
            print(f"   PDF: {paper['pdf_url']}")
            print(f"   镜像: {paper['mirror']}")
        else:
            print("❌ 未获取到（所有镜像都被拦或 DOI 不存在）")
    else:
        fetch_by_title(arg)
