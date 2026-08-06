#!/usr/bin/env python3
"""
ethics.py - agent-eye compliance firewall (ADR + reverse-skill inspired)

Four-layer protection:
  1. Throttle: human-rhythm delays between all actions
  2. ResearchLog: JSONL audit trail proving academic intent
  3. ProxyConfig: unified proxy detection and isolation
  4. AnomalyDetector: auto-pause on consecutive failures (ADR mode)

Usage:
    from ethics import throttle, log, ProxyConfig, AnomalyDetector
"""

import time
import random
import json
import os
import socket
from datetime import datetime
from pathlib import Path


# --- 1. Throttle ---

class Throttle:
    """Delay controller - simulates human browsing rhythm."""

    def __init__(self, base_delay: float = 2.0, jitter: float = 1.0):
        self.base = base_delay
        self.jitter = jitter
        self._last_action = 0.0

    def wait(self):
        elapsed = time.time() - self._last_action
        needed = self.base + random.uniform(-self.jitter, self.jitter)
        if elapsed < needed:
            time.sleep(needed - elapsed)
        self._last_action = time.time()

    def touch(self):
        self._last_action = time.time()


throttle = Throttle(base_delay=2.0, jitter=1.0)
pause = throttle.wait


# --- 2. ResearchLog ---

class ResearchLog:
    """JSONL audit log - proves academic research intent."""

    def __init__(self, path: str = "research_log.jsonl"):
        self.path = Path(path)
        self._ensure()

    def _ensure(self):
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def log(self, agent: str, action: str, target: str = "",
            result: str = "", note: str = ""):
        entry = {
            "ts": datetime.now().isoformat(),
            "agent": agent,
            "action": action,
            "target": target or "",
            "result": result or "",
            "note": note or "personal academic research",
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def tail(self, n: int = 10) -> str:
        lines = []
        if self.path.exists():
            lines = self.path.read_text(encoding="utf-8").strip().split("\n")[-n:]
        return "\n".join(lines)


log = ResearchLog()


# --- 3. ProxyConfig ---

class ProxyConfig:
    """Detect and manage proxy state."""

    DEFAULT_PROXY = "socks5://127.0.0.1:10808"

    @staticmethod
    def detect(host: str = "127.0.0.1", port: int = 10808) -> str | None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        alive = s.connect_ex((host, port)) == 0
        s.close()
        return f"socks5://{host}:{port}" if alive else None

    @staticmethod
    def status() -> dict:
        proxy = ProxyConfig.detect()
        return {
            "proxy_available": proxy is not None,
            "proxy_address": proxy or "none",
            "mode": "proxy" if proxy else "direct (domestic only)",
        }


# --- 4. AnomalyDetector (ADR mode) ---

class AnomalyDetector:
    """Auto-pause on consecutive failures. Inspired by Uber ADR."""

    def __init__(self, max_failures: int = 3):
        self.max = max_failures
        self.count = 0
        self.paused = False
        self.reason = ""

    def record(self, action: str, ok: bool, note: str = "") -> bool:
        """Return True = should pause."""
        if ok:
            self.count = 0
            return False
        self.count += 1
        if action in ("wait", "done", "captcha") and self.count >= self.max:
            self.paused = True
            self.reason = f"{self.count}x {action}: {note}"
            log.log("ethics", "anomaly_pause", action,
                    f"{self.count} failures", self.reason)
            return True
        return False

    def reset(self):
        self.count = 0
        self.paused = False
        self.reason = ""

    def status(self) -> dict:
        return {"paused": self.paused, "failures": self.count, "reason": self.reason}


detector = AnomalyDetector(max_failures=3)


# --- Compliance check ---

def compliance_check() -> dict:
    return {
        "throttle": f"{throttle.base}s +/- {throttle.jitter}s",
        "logging": f"{log.path} ({'exists' if log.path.exists() else 'missing'})",
        "proxy": ProxyConfig.status(),
        "anomaly_detector": detector.status(),
        "timestamp": datetime.now().isoformat(),
    }


if __name__ == "__main__":
    print(json.dumps(compliance_check(), ensure_ascii=False, indent=2))
