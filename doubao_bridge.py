#!/usr/bin/env python3
"""
doubao_bridge.py — 豆包网页版常驻对话桥

基于 doubao_worker.js（Node 常驻进程）：
- 浏览器常驻，会话保持（不每次开新对话）
- 人类节奏：随机延迟、逐字打字
- 可传本地图片 → 豆包识图

用法:
    from doubao_bridge import DoubaoBridge
    db = DoubaoBridge()
    db.start()
    ans = db.ask("分析这张图", images=["/path/img.png"])
    db.stop()

    # CLI:
    python3 doubao_bridge.py "问题" --image a.png
"""

import json
import os
import subprocess
import sys
import time

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "doubao_worker.js")


class DoubaoBridge:
    """豆包常驻会话桥。start() 后保持运行，多次 ask 复用同一会话。"""

    def __init__(self, worker: str | None = None):
        self.worker = worker or WORKER
        self._proc: subprocess.Popen | None = None
        self._ready = False

    def start(self, timeout: int = 30) -> bool:
        """启动常驻 worker，等待 ready。"""
        if self._proc and self._proc.poll() is None:
            return True
        self._proc = subprocess.Popen(
            ["node", self.worker],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        # 等 ready 信号
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                break
            try:
                resp = json.loads(line)
                if resp.get("ready"):
                    self._ready = True
                    return True
            except json.JSONDecodeError:
                continue
        return False

    def _send(self, msg: dict, timeout: float = 60.0) -> dict:
        """发送命令并等待响应（stdin 一行，stdout 一行）。"""
        if not self._proc or self._proc.poll() is not None:
            return {"ok": False, "error": "worker not running"}
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()
        # 等待响应（带超时）
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                return {"ok": False, "error": "worker closed"}
            try:
                resp = json.loads(line)
                if resp.get("exit"):
                    self._proc = None
                return resp
            except json.JSONDecodeError:
                continue
        return {"ok": False, "error": f"timeout after {timeout}s"}

    # ── 公共接口 ──────────────────────────────

    def ask(self, question: str, images: list[str] | None = None,
            timeout: int = 45) -> str:
        """问豆包。可带本地图片。返回回答文本。"""
        resp = self._send({
            "action": "ask",
            "question": question,
            "images": images or [],
            "timeout": timeout * 1000,
        }, timeout=timeout + 10)
        if resp.get("ok"):
            ans = resp.get("answer", "(空回答)")
            # 清洗：去掉尾巴里夹带的用户问题原文（容忍 DOM 空格/换行差异）
            q_clean = question.strip()
            import re
            pat = re.compile(r"\s*".join(re.escape(c) for c in q_clean) + r"\s*")
            ans_clean = pat.sub("", ans, count=1) if q_clean else ans
            # 去掉常见前缀（问题重复/引导语）
            for pre in ["你刚才问", "你问的是", "问题：", "回答："]:
                if ans_clean.startswith(pre):
                    ans_clean = ans_clean[len(pre):]
                    break
            ans_clean = ans_clean.strip() or "(空回答)"
            # 去掉尾部豆包推荐问题（"如何…""给我推荐…"等追问句）
            import re as _re
            ans_clean = _re.split(r"\n\s*(?:如何|给我推荐|还有哪些|能不能|你能|你会|再生成|要不要)", ans_clean)[0]
            ans_clean = _re.sub(r"[\s]*$", "", ans_clean)
            # 去掉"1）…2）…"编号前缀（豆包习惯性分点，对情报展示更干净）
            ans_clean = _re.sub(r"^[0-9０-９]）\s*", "", ans_clean)
            return ans_clean or "(空回答)"
        return f"(豆包调用失败: {resp.get('error', '?')})"

    def new_chat(self) -> dict:
        """开新对话（可选，默认保持会话）。"""
        return self._send({"action": "new_chat"}, timeout=20)

    def status(self) -> dict:
        """当前会话状态。"""
        return self._send({"action": "status"}, timeout=10)

    def stop(self):
        """关闭 worker 和浏览器。"""
        if self._proc and self._proc.poll() is None:
            try:
                self._send({"action": "exit"}, timeout=8)
            except Exception:
                pass
            try:
                if self._proc and self._proc.poll() is None:
                    self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if self._proc:
                    self._proc.kill()
        self._proc = None
        self._ready = False

    # ── 上下文管理 ──────────────────────────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()


# ─── CLI ──────────────────────────────────────


def main():
    import argparse
    p = argparse.ArgumentParser(description="豆包常驻对话桥")
    p.add_argument("question", help="要问的问题")
    p.add_argument("--image", action="append", default=[], help="图片路径（可多个）")
    p.add_argument("--no-exit", action="store_true", help="问完不退出（保持会话）")
    args = p.parse_args()

    bridge = DoubaoBridge()
    if not bridge.start():
        print("❌ 启动失败（node/playwright 问题）")
        sys.exit(1)

    print(f"✅ 豆包已连接（会话保持模式）")
    answer = bridge.ask(args.question, args.image)
    print(f"\n💬 豆包:\n{answer}")

    if not args.no_exit:
        bridge.stop()
        print("\n👋 已关闭（下次调用将保持同一会话）" if False else "\n👋 已关闭")


if __name__ == "__main__":
    main()
