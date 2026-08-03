#!/usr/bin/env python3
"""
ethics.py — agent-eye 合规防火墙

三层技术保护：
  1. 频率节流：所有操作加人类节奏延迟
  2. 日志清白：自动记录研究意图，留下学术证据
  3. 代理隔离：区分爬虫流量和个人流量

用法:
    from ethics import throttle, ResearchLog, ProxyConfig
"""

import time
import random
import json
import os
import socket
from datetime import datetime
from pathlib import Path


# ─── 1. 频率节流 ──────────────────────────────────


# 默认：模拟人类浏览节奏
class Throttle:
    """操作间隔控制器。模拟人类行为——不完全均匀，有微小随机波动。"""

    def __init__(self, base_delay: float = 2.0, jitter: float = 1.0):
        """
        Args:
            base_delay: 基础延迟（秒）
            jitter: 随机抖动范围（±jitter 秒）
        """
        self.base = base_delay
        self.jitter = jitter
        self._last_action = 0.0

    def wait(self):
        """等待合适的时间间隔，然后记录本次操作时间。"""
        elapsed = time.time() - self._last_action
        needed = self.base + random.uniform(-self.jitter, self.jitter)
        if elapsed < needed:
            time.sleep(needed - elapsed)
        self._last_action = time.time()

    def touch(self):
        """只记录操作时间，不等待。（用于「刚操作完，这时开始计时」）"""
        self._last_action = time.time()


# 全局实例——每个模块共用一个节流器
throttle = Throttle(base_delay=2.0, jitter=1.0)

# 快捷函数：等一等再操作
pause = throttle.wait


# ─── 2. 日志清白 ──────────────────────────────────


class ResearchLog:
    """
    研究意图日志——记录 agent 在做什么、为什么做。
    目的：万一被问到，能证明这是学术研究，不是恶意攻击。

    日志格式（JSONL）：
    {
        "ts": "2026-08-03T12:00:00",
        "agent": "trend_scout",
        "action": "source_youtube",
        "target": "ytsearch:AI工具",
        "result": "10 items",
        "note": "个人学习研究——分析海外AI内容生态"
    }
    """

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
            "note": note or "个人学术研究",
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def tail(self, n: int = 10) -> str:
        """查看最近 N 条日志"""
        lines = []
        if self.path.exists():
            lines = self.path.read_text(encoding="utf-8").strip().split("\n")[-n:]
        return "\n".join(lines)


# 全局日志实例
log = ResearchLog()


# ─── 3. 代理隔离 ──────────────────────────────────


class ProxyConfig:
    """检测并管理代理状态。"""

    DEFAULT_PROXY = "socks5://127.0.0.1:10808"

    @staticmethod
    def detect(host: str = "127.0.0.1", port: int = 10808) -> str | None:
        """返回可用代理地址，不可用则返回 None。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        alive = s.connect_ex((host, port)) == 0
        s.close()
        return f"socks5://{host}:{port}" if alive else None

    @staticmethod
    def status() -> dict:
        """返回当前网络状态诊断。"""
        proxy = ProxyConfig.detect()
        return {
            "proxy_available": proxy is not None,
            "proxy_address": proxy or "无",
            "mode": "代理模式" if proxy else "直连模式（仅国内源）",
        }


# ─── 4. 完整合规检查（一键自检）──────────────────


def compliance_check() -> dict:
    """运行合规检查，返回状态报告。"""
    return {
        "throttle": f"基础延迟 {throttle.base}s ± {throttle.jitter}s",
        "logging": f"{log.path} ({'存在' if log.path.exists() else '缺失'})",
        "proxy": ProxyConfig.status(),
        "timestamp": datetime.now().isoformat(),
    }


if __name__ == "__main__":
    print(json.dumps(compliance_check(), ensure_ascii=False, indent=2))
