#!/usr/bin/env python3
"""
hand.py — agent-eye v2 手组件（Node.js Playwright 版本）

通过子进程与 browser_worker.js 通信，不依赖 Python greenlet。
所有操作返回 (success: bool, detail: str)。接口兼容原 async 版本。
"""

import json
import re
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


# ── camofox 后端（第二浏览器引擎，2026-09-18）─────────────────────
#
# 与 BrowserSession 的差别：
#   - 观察返回 **aria 快照**（语义 YAML + 元素引用 e1/e2），不是正文纯文本
#   - click 可以用**元素引用**（ref='e3'）—— 比 CSS selector / 像素坐标稳
#   - 生命周期是「检查服务活着 + 建 tab」，不 spawn 子进程
#   - 不继承 `_send` 的 JSON-RPC over stdin/stdout，而是映射到 camofox REST
#
# 因为 vision.observe() 靠 `hasattr(_send)` 判后端，这里实现了同名 `_send`，
# 所以 vision / thinker / loop **无需感知**是哪个引擎。
#
# 前置: bash camofox/start.sh

class CamofoxSession:
    """camofox-browser 后端（HTTP 独立进程），接口与 BrowserSession 对齐。"""

    def __init__(self, base: str = "http://127.0.0.1:9377",
                 user_id: str = "agent-eye", timeout: int = 60000, **kwargs):
        # **kwargs 吞掉 headless 等 Playwright 专有参数，保持构造签名兼容
        self.base = base
        self.user_id = user_id
        self.timeout = timeout
        self._c = None
        self._tab = None
        self._current_url = ""
        self._last_snapshot = ""

    # ── 生命周期 ────────────────────────────────
    def start(self):
        from camofox_client import Camofox
        self._c = Camofox(base=self.base, user_id=self.user_id)
        if not self._c.alive():
            raise RuntimeError(
                f"camofox-browser 没在跑（{self.base}）。先执行: bash camofox/start.sh")
        self._tab = self._c.create_tab()

    def stop(self):
        if self._c and self._tab:
            try:
                self._c.close_tab(self._tab)
            except Exception:  # noqa: BLE001
                pass
        self._tab = None

    @property
    def page(self):
        return self

    @property
    def url(self) -> str:
        return self._current_url

    # ── 与 BrowserSession 对齐的 _send 协议 ─────
    def _send(self, msg: dict) -> dict:
        action = msg.get("action")
        if not self._c or not self._tab:
            return {"ok": False, "error": "CamofoxSession 未 start()"}
        try:
            if action == "navigate":
                r = self._c.navigate(self._tab, msg["url"])
                self._current_url = r.get("url") or msg["url"]
                return {"ok": bool(r.get("navigationOk", True)),
                        "title": self._title(), "url": self._current_url,
                        "httpStatus": r.get("httpStatus")}

            if action == "extract":
                snap = self._c.snapshot(self._tab)
                self._last_snapshot = snap.get("snapshot") or ""
                self._current_url = snap.get("url") or self._current_url
                return {"ok": True, "title": self._title(),
                        "body": _snapshot_to_text(self._last_snapshot,
                                                  msg.get("max_chars", 2000)),
                        "meta": "", "refsCount": snap.get("refsCount")}

            if action == "click":
                self._c.click(self._tab, ref=msg.get("ref"), selector=msg.get("selector"))
                return {"ok": True, "selector": msg.get("ref") or msg.get("selector")}

            if action == "type":
                self._c.type_text(self._tab, msg.get("text", ""),
                                  ref=msg.get("ref"), selector=msg.get("selector"),
                                  submit=msg.get("submit", False))
                return {"ok": True, "selector": msg.get("ref") or msg.get("selector")}

            if action == "scroll":
                # BrowserSession 语义是像素；camofox 是 direction + amount(屏)
                px = int(msg.get("amount", 500))
                self._c.scroll(self._tab, direction="down" if px >= 0 else "up",
                               amount=max(1, abs(px) // 600))
                return {"ok": True}

            if action == "wait":
                self._c.wait(self._tab, msg.get("ms", 1000))
                return {"ok": True}

            if action == "screenshot":
                path = msg.get("path") or ".screenshots/latest.png"
                d = os.path.dirname(path)
                if d:
                    os.makedirs(d, exist_ok=True)
                self._c.screenshot(self._tab, path)
                return {"ok": True, "path": path}

            if action == "exit":
                return {"ok": True}

        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": False, "error": f"unsupported action: {action}"}

    def _title(self) -> str:
        """
        aria 快照不含 doctitle：
          1. 先取首个 heading（多数内容页够用）
          2. 没有 heading（如只有 <title> 的页面）→ evaluate 直接问 DOM
        """
        for line in self._last_snapshot.splitlines():
            s = line.strip()
            if s.startswith("- heading"):
                q1, q2 = s.find('"'), s.rfind('"')
                if 0 <= q1 < q2:
                    return s[q1 + 1:q2]
        try:
            v = self._c.evaluate(self._tab, "document.title")
            if v:
                return str(v).strip()
        except Exception:  # noqa: BLE001
            pass
        return self._current_url

    # ── async 操作（与 BrowserSession 同名同签名）──
    async def navigate(self, url: str) -> tuple:
        self._current_url = url
        r = self._send({"action": "navigate", "url": url})
        return r.get("ok", False), r.get("title") or r.get("error", "?")

    async def click(self, selector: str) -> tuple:
        # 元素引用优先：形如 e1/e2 当 ref 用，否则当 CSS selector
        is_ref = bool(re.fullmatch(r"e\d+", selector or ""))
        r = self._send({"action": "click", "ref" if is_ref else "selector": selector})
        return r.get("ok", False), r.get("selector") or r.get("error", "?")

    async def type_text(self, selector: str, text: str) -> tuple:
        is_ref = bool(re.fullmatch(r"e\d+", selector or ""))
        r = self._send({"action": "type", "text": text,
                        "ref" if is_ref else "selector": selector})
        return r.get("ok", False), r.get("error") or "ok"

    async def scroll(self, amount: int = 500) -> tuple:
        r = self._send({"action": "scroll", "amount": amount})
        return r.get("ok", False), (f"scrolled {amount}px" if r.get("ok")
                                    else r.get("error", "?"))

    async def wait(self, ms: int = 2000) -> tuple:
        self._send({"action": "wait", "ms": ms})
        return True, f"waited {ms}ms"

    async def shoot(self, path: str | None = None) -> str | None:
        p = path or ".screenshots/latest.png"
        r = self._send({"action": "screenshot", "path": p})
        return p if r.get("ok") else None

    # ── camofox 独有：元素引用 ──────────────────
    def refs(self) -> list:
        """从最近快照解析 [eN] 引用 → [{ref, role, name}]。"""
        out = []
        for line in self._last_snapshot.splitlines():
            m = re.search(r'- (\w[\w-]*) (.*?)\s*\[(e\d+)\]', line)
            if m:
                out.append({"ref": m.group(3), "role": m.group(1),
                            "name": m.group(2).strip().strip('"')})
        return out

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


def _snapshot_to_text(snapshot: str, max_chars: int = 2000) -> str:
    """aria YAML → 紧凑文本（去缩进与行首角色前缀，保留可读内容）。"""
    out = []
    total = 0
    for line in snapshot.splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r'^- (?:\w[\w-]* )?', '', s)
        out.append(s)
        total += len(s)
        if total >= max_chars:
            break
    return "\n".join(out)[:max_chars]
