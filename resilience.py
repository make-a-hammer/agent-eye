#!/usr/bin/env python3
"""
resilience.py — agent-eye 抗脆弱访问层（合规版）

源自架构报告 §3「合规的访问抗脆弱性」：
    - 目标不是"绕过防护"，而是让采集稳定、低负载、可恢复
    - 429/503 时降速而非换 IP；验证码出现时停止而非绕过
    - 连续失败后暂停域名，而不是无限重试

三个组件：
    ① TokenBucket    — 按域名令牌桶限速（自适应：出错降速）
    ② CircuitBreaker — 断路器（连续失败 N 次 → 暂停域名 T 秒）
    ③ SourceFallback — 来源替代图（主源挂了 → 切备源）

用法:
    from resilience import ResilienceManager
    rm = ResilienceManager()
    if rm.allow("api.openalex.org"):
        try:
            data = fetch(...)
            rm.record_success("api.openalex.org")
        except RateLimitError:
            rm.record_failure("api.openalex.org", reason="429")
            # 下次自动降速/暂停

合规说明（报告立场）：
    - 本模块不做 User-Agent 伪装、代理轮换、验证码绕过
    - 429 → 指数退避（降低请求速率）
    - 验证码/登录墙 → 停止自动化，交回人工
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "sources_registry.json")
STATE_PATH = os.path.expanduser("~/.agent-eye_resilience.json")


# ─── ① 令牌桶限速（自适应）─────────────────────


class TokenBucket:
    """
    按域名的令牌桶。出错时自动降低速率（合规降速，非绕过）。

    默认 1 rps，容量 burst。每次 take() 消耗 1 令牌，不足则等待。
    """

    def __init__(self, rate: float = 1.0, capacity: float = 3.0):
        self.rate = rate                # 每秒补充令牌
        self.base_rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.time()
        self._lock = threading.Lock()

    def _refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now

    def take(self, block: bool = True, timeout: float = 30.0) -> bool:
        """取一个令牌。block=True 时等待直到可用。"""
        deadline = time.time() + timeout
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
            if not block:
                return False
            if time.time() > deadline:
                return False
            time.sleep(0.1)

    def throttle(self, factor: float = 0.5):
        """降速（出错时调用）：速率乘以 factor，下限 0.1 rps。"""
        with self._lock:
            self.rate = max(0.1, self.rate * factor)

    def recover(self, factor: float = 1.2):
        """恢复速率（成功时调用）：逐步回到基准速率。"""
        with self._lock:
            self.rate = min(self.base_rate, self.rate * factor)


# ─── ② 断路器 ──────────────────────────────


@dataclass
class BreakerState:
    failures: int = 0
    opened_at: float | None = None
    last_reason: str = ""
    half_open: bool = False


class CircuitBreaker:
    """
    断路器：连续失败达阈值 → 打开（暂停该域名）→ 冷却后半开试探。

    状态机：closed → (N次失败) → open → (冷却T秒) → half_open → 成功 → closed
    """

    def __init__(self, threshold: int = 3, cooldown_s: float = 60.0):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.states: dict[str, BreakerState] = {}
        self._lock = threading.Lock()

    def allow(self, domain: str) -> bool:
        """是否允许请求该域名。"""
        with self._lock:
            st = self.states.get(domain)
            if st is None or st.opened_at is None:
                return True
            # 冷却中？
            if time.time() - st.opened_at < self.cooldown_s:
                return False
            # 冷却结束 → 半开试探（放行一次）
            st.half_open = True
            return True

    def record_success(self, domain: str):
        with self._lock:
            self.states[domain] = BreakerState()      # 重置
            self._save()

    def record_failure(self, domain: str, reason: str = ""):
        with self._lock:
            st = self.states.setdefault(domain, BreakerState())
            if st.half_open:
                # 半开试探失败 → 重新打开，冷却翻倍
                st.opened_at = time.time()
                st.cooldown_s = getattr(st, "cooldown_s", self.cooldown_s) * 2
                st.half_open = False
                st.last_reason = f"{reason} (half-open failed)"
            else:
                st.failures += 1
                st.last_reason = reason
                if st.failures >= self.threshold:
                    # 指数冷却：每轮打开时长翻倍
                    opened = st.opened_at is not None
                    cooldown = self.cooldown_s * (2 if opened else 1)
                    st.opened_at = time.time()
                    st.cooldown_s = cooldown
            self._save()

    def status(self) -> dict:
        return {d: {"failures": s.failures, "open": s.opened_at is not None,
                    "reason": s.last_reason}
                for d, s in self.states.items()}

    def _save(self):
        try:
            data = {d: {"failures": s.failures, "opened_at": s.opened_at,
                        "last_reason": s.last_reason}
                    for d, s in self.states.items()}
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass


# ─── ③ 来源替代图 ─────────────────────────────


class SourceFallback:
    """
    来源替代链：主源失败 → 按 registry 里配置的 failure_fallback 切换。

    例：openalex 挂了 → crossref → arxiv
    """

    def __init__(self, registry_path: str = REGISTRY_PATH):
        self.registry = {}
        try:
            with open(registry_path, encoding="utf-8") as f:
                self.registry = json.load(f).get("sources", {})
        except Exception:
            pass
        self._failed: set[str] = set()

    def mark_failed(self, source_id: str):
        self._failed.add(source_id)

    def mark_ok(self, source_id: str):
        self._failed.discard(source_id)

    def chain(self, source_id: str) -> list[str]:
        """返回该源的替代链（含自身，按优先级）。"""
        cfg = self.registry.get(source_id, {})
        return [source_id] + list(cfg.get("failure_fallback", []))

    def next_available(self, source_id: str) -> str | None:
        """返回替代链中第一个未失败的源。"""
        for s in self.chain(source_id):
            if s not in self._failed:
                return s
        return None

    def config(self, source_id: str) -> dict:
        return self.registry.get(source_id, {})


# ─── 统一管理器 ────────────────────────────────


class ResilienceManager:
    """组合限速 + 断路器 + 替代图，供各数据源统一调用。"""

    def __init__(self, registry_path: str = REGISTRY_PATH):
        self.buckets: dict[str, TokenBucket] = {}
        self.breaker = CircuitBreaker()
        self.fallback = SourceFallback(registry_path)
        self._lock = threading.Lock()

    def _domain(self, url_or_domain: str) -> str:
        if "://" in url_or_domain:
            return urlparse(url_or_domain).netloc
        return url_or_domain

    def _bucket_for(self, domain: str) -> TokenBucket:
        with self._lock:
            if domain not in self.buckets:
                # 查 registry 配置的速率
                rate = 1.0
                for sid, cfg in self.fallback.registry.items():
                    base = cfg.get("base_url", "")
                    if base and self._domain(base) == domain:
                        rate = float(cfg.get("rate_limit", {}).get("rps", 1.0))
                        break
                self.buckets[domain] = TokenBucket(rate=rate, capacity=max(1.0, rate))
            return self.buckets[domain]

    def allow(self, url_or_domain: str) -> bool:
        """该域名当前是否允许请求（断路器 + 限速）。"""
        domain = self._domain(url_or_domain)
        if not self.breaker.allow(domain):
            return False
        return self._bucket_for(domain).take(block=True, timeout=10.0)

    def record_success(self, url_or_domain: str):
        domain = self._domain(url_or_domain)
        self.breaker.record_success(domain)
        self._bucket_for(domain).recover()

    def record_failure(self, url_or_domain: str, reason: str = ""):
        domain = self._domain(url_or_domain)
        self.breaker.record_failure(domain, reason)
        self._bucket_for(domain).throttle()   # 合规降速，非换 IP

    def status(self) -> dict:
        return {
            "breakers": self.breaker.status(),
            "buckets": {d: round(b.rate, 3) for d, b in self.buckets.items()},
        }


# ─── 便捷装饰器 ────────────────────────────────

_default_rm: ResilienceManager | None = None


def get_manager() -> ResilienceManager:
    global _default_rm
    if _default_rm is None:
        _default_rm = ResilienceManager()
    return _default_rm


def guarded(source_id: str, max_retries: int = 3):
    """
    装饰器：给数据源函数加限速 + 退避重试 + 断路器。

    用法:
        @guarded("openalex")
        def fetch_papers(query): ...
    """
    def deco(fn):
        def wrapper(*args, **kwargs):
            rm = get_manager()
            cfg = rm.fallback.config(source_id)
            domain = rm._domain(cfg.get("base_url", source_id))
            last_err = None
            for attempt in range(max_retries):
                if not rm.allow(domain):
                    raise RuntimeError(f"断路器开启，{domain} 已暂停（连续失败）")
                try:
                    result = fn(*args, **kwargs)
                    rm.record_success(domain)
                    rm.fallback.mark_ok(source_id)
                    return result
                except Exception as e:
                    last_err = e
                    rm.record_failure(domain, reason=type(e).__name__)
                    # 指数退避 + 抖动（报告 §3：避免多 worker 同时重试）
                    import random
                    delay = (2 ** attempt) + random.uniform(0, 0.5)
                    time.sleep(min(delay, 8))
            raise last_err
        return wrapper
    return deco


# ─── CLI ─────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="agent-eye 抗脆弱层")
    p.add_argument("--status", action="store_true", help="查看状态")
    p.add_argument("--test", action="store_true", help="自测")
    args = p.parse_args()

    if args.test:
        rm = ResilienceManager()
        print("=== 抗脆弱自测 ===")
        # 限速
        t0 = time.time()
        for _ in range(3):
            rm.allow("api.test.com")
        print(f"① 令牌桶: 3 次请求耗时 {time.time()-t0:.2f}s（有速率限制）")
        # 断路器
        for i in range(3):
            rm.record_failure("bad.test.com", "429")
        allowed = rm.allow("bad.test.com")
        print(f"② 断路器: 连续3次失败后 allow={allowed}（应为 False）")
        # 降速
        rate_before = rm._bucket_for("bad.test.com").rate
        print(f"③ 降速: 基准速率已降到 {rate_before:.2f} rps")
        # 来源替代
        chain = rm.fallback.chain("openalex")
        print(f"④ 来源替代链: openalex → {chain[1:]}")
        rm.fallback.mark_failed("openalex")
        nxt = rm.fallback.next_available("openalex")
        print(f"⑤ openalex 失败后切换到: {nxt}")
    else:
        print(json.dumps(get_manager().status(), ensure_ascii=False, indent=2))
