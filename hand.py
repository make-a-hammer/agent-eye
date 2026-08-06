#!/usr/bin/env python3
"""
hand.py — agent-eye v2 手组件（Node.js Playwright 版本）

通过子进程与 browser_worker.js 通信，不依赖 Python greenlet。
所有操作返回 (success: bool, detail: str)。接口兼容原 async 版本。
"""

import json
import subprocess
import os
import asyncio

WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "browser_worker.js")


class BrowserSession:
    """通过 Node.js 子进程管理 Playwright 浏览器会话。"""

    def __init__(self, headless: bool = False, timeout: int = 20000):
        self.headless = headless
        self.timeout = timeout
        self._proc = None
        self._ready = False
        self._current_url = ""

    def start(self):
        env = os.environ.copy()
        self._proc = subprocess.Popen(
            ["node", WORKER_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, bufsize=1,
            env=env,
        )
        line = self._proc.stdout.readline()
        resp = json.loads(line)
        if resp.get("ready"):
            self._ready = True

    def stop(self):
        if self._proc:
            self._send({"action": "exit"})
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def _send(self, msg: dict) -> dict:
        if not self._proc or self._proc.poll() is not None:
            return {"ok": False, "error": "Worker process dead"}
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            return {"ok": False, "error": "Worker no response"}
        return json.loads(line)

    # ── 兼容旧接口 ──────────────────────────────

    @property
    def page(self):
        return self

    @property
    def url(self) -> str:
        return self._current_url

    # ── 操作（async 兼容原 loop.py）──────────────

    async def navigate(self, url: str) -> tuple:
        self._current_url = url
        resp = self._send({"action": "navigate", "url": url, "timeout": self.timeout})
        return resp.get("ok", False), resp.get("title", resp.get("error", "?"))

    async def click(self, selector: str) -> tuple:
        resp = self._send({"action": "click", "selector": selector})
        return resp.get("ok", False), resp.get("selector", resp.get("error", "?"))

    async def type_text(self, selector: str, text: str) -> tuple:
        resp = self._send({"action": "type", "selector": selector, "text": text})
        return resp.get("ok", False), resp.get("selector", resp.get("error", "?"))

    async def scroll(self, amount: int = 500) -> tuple:
        resp = self._send({"action": "scroll", "amount": amount})
        return resp.get("ok", False), f"scrolled {amount}px" if resp.get("ok") else resp.get("error", "?")

    async def wait(self, ms: int = 2000) -> tuple:
        resp = self._send({"action": "wait", "ms": ms})
        return True, f"waited {ms}ms"

    async def shoot(self, path: str | None = None) -> str | None:
        p = path or ".screenshots/latest.png"
        resp = self._send({"action": "screenshot", "path": p})
        return p if resp.get("ok") else None

    # ── 上下文管理 ──────────────────────────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    async def __aenter__(self):
        self.start()
        return self

    async def __aexit__(self, *args):
        self.stop()
