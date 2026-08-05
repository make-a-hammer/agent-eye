#!/usr/bin/env python3
"""
test_protocol_loop.py — agent-eye 调试协议链路合成测试

不依赖真实浏览器：用 FakeHand 模拟 hand 接口，验证
    失败 → 诊断 → 修复 → 重试 全链路。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from protocol import load_protocol, DebugProtocol
from repair import Repairer
from loop import diagnose_and_repair


class FakeHand:
    """模拟 BrowserSession 的原子操作。"""

    def __init__(self, fail_times: int = 1):
        self.fail_times = fail_times  # 前 N 次 navigate 失败
        self.navigate_calls = 0
        self.wait_ms = []
        self.reload_calls = 0
        self.scroll_calls = 0
        self.click_calls = 0

    async def navigate(self, url: str) -> tuple[bool, str]:
        self.navigate_calls += 1
        if self.navigate_calls <= self.fail_times:
            return False, "Navigation timeout of 20000 ms exceeded"
        return True, f"ok: {url}"

    async def wait(self, ms: int = 2000) -> tuple[bool, str]:
        self.wait_ms.append(ms)
        return True, f"waited {ms}ms"

    async def scroll(self, amount: int = 500) -> tuple[bool, str]:
        self.scroll_calls += 1
        return True, f"scrolled {amount}px"

    async def click(self, selector: str) -> tuple[bool, str]:
        self.click_calls += 1
        return True, f"clicked {selector}"

    async def reload(self, **kwargs):
        self.reload_calls += 1
        return True


async def test_diagnose_repair_retry():
    """核心链路：NAV_TIMEOUT → 命中协议 → wait_longer 修复 → 重试成功"""
    protocol = load_protocol()
    hand = FakeHand(fail_times=1)
    repairer = Repairer(hand)

    # 第一次导航失败
    ok, detail = await hand.navigate("https://example.com")
    assert not ok, "第一次应该失败"

    # 诊断 + 修复
    handled, rdetail, entry = await diagnose_and_repair(
        protocol, repairer, "navigate", detail, context={"url": "https://example.com"})
    print(f"诊断结果: handled={handled} | {rdetail}")
    assert handled, "NAV_TIMEOUT 应该命中协议并修复"
    assert entry is not None and entry.signature["errorCode"] == "NAV_TIMEOUT"

    # 重试
    ok2, detail2 = await hand.navigate("https://example.com")
    assert ok2, "修复后重试应该成功"
    print(f"重试成功: {detail2}")
    print(f"✅ 链路1通过: 失败→诊断→修复→重试")


async def test_antibot_compliance():
    """合规边界：验证码 → 不破解，走等待/放弃"""
    protocol = load_protocol()
    hand = FakeHand(fail_times=0)
    repairer = Repairer(hand)

    handled, rdetail, entry = await diagnose_and_repair(
        protocol, repairer, "navigate", "captcha: please verify you are human",
        context={"url": "https://x.com"})
    print(f"验证码诊断: handled={handled} | {rdetail}")
    assert entry is not None and entry.signature["errorCode"] == "ANTIBOT_DETECTED"
    # 修复动作必须是合规的（等待/换源/放弃），绝不能是"破解"
    fix_actions = [a["action"] for a in entry.fix["actions"]]
    forbidden = {"crack_captcha", "bypass", "solve_captcha"}
    assert not (forbidden & set(fix_actions)), f"禁止的修复动作: {fix_actions}"
    print(f"✅ 链路2通过: 验证码走合规路径，动作={fix_actions}")


async def test_unmatched_error():
    """未知错误 → 不命中 → 不 panic"""
    protocol = load_protocol()
    hand = FakeHand(fail_times=0)
    repairer = Repairer(hand)

    handled, rdetail, entry = await diagnose_and_repair(
        protocol, repairer, "navigate", "some totally unknown error xx#@!",
        context={"url": "https://example.com"})
    print(f"未知错误诊断: handled={handled} | {rdetail}")
    assert not handled and entry is None
    print("✅ 链路3通过: 未知错误安全降级")


async def test_occurrence_growth():
    """Recorder：命中后 occurrences 应该增长"""
    protocol = load_protocol()
    hand = FakeHand(fail_times=1)
    repairer = Repairer(hand)

    before = {e.id: e.occurrences for e in protocol.entries}
    for _ in range(2):
        ok, detail = await hand.navigate("https://example.com")
        if not ok:
            await diagnose_and_repair(protocol, repairer, "navigate", detail,
                                      context={"url": "https://example.com"})
    after = {e.id: e.occurrences for e in protocol.entries}

    grown = [eid for eid in before if after.get(eid, 0) > before.get(eid, 0)]
    print(f"occurrences 增长: {grown}")
    assert grown, "命中条目 occurrences 应该增长"
    print("✅ 链路4通过: 经验计数累积")


async def main():
    await test_diagnose_repair_retry()
    print()
    await test_antibot_compliance()
    print()
    await test_unmatched_error()
    print()
    await test_occurrence_growth()
    print("\n🎉 全部测试通过")


if __name__ == "__main__":
    asyncio.run(main())
