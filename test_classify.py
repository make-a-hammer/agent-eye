#!/usr/bin/env python3
"""
test_classify.py — 查询分类器回归测试

分类器是路由入口，判错会导致整个检索走错源。
本文件固化用例，防止后续改动导致退化。

运行: python test_classify.py
     或 pytest test_classify.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sources import classify_query, QUERY_TYPES

# (查询, 期望类型, 说明)
CASES = [
    # ── research: 学术主题 ──
    ("固态电池 最新进展", "research", "学科进展"),
    ("Transformer 注意力机制", "research", "学术机制"),
    ("量子计算 应用", "research", "复合学科名词（不能误判 computational）"),
    ("云计算 趋势", "research", "复合学科名词"),
    ("大模型 微调 技术综述", "research", "技术主题"),

    # ── computational: 真的要做计算 ──
    ("计算 这组数据的增长率", "computational", "明确计算意图"),
    ("统计 平均值", "computational", "统计意图"),
    ("这批数据的总和是多少", "computational", "求和意图"),

    # ── factual: 事实查询（疑问词）──
    ("2026年诺贝尔物理学奖得主是谁", "factual", "事实疑问（含学科词但问的是事实）"),
    ("深圳的邮编是多少", "factual", "事实疑问（'多少'不能触发 computational）"),

    # ── comparative ──
    ("iPhone 17 vs 华为 Mate", "comparative", "对比"),

    # ── operational ──
    ("如何安装 n8n", "operational", "操作类"),
    ("下载 微信安装包", "operational", "下载类"),

    # ── technical: 代码/工具 ──
    ("pdf 解析开源库", "technical", "开源库"),
    ("找 GitHub 上的 pdf 工具", "technical", "GitHub 工具"),
    ("npm 安装 playwright", "technical", "npm 包"),

    # ── high_risk: 安全第一 ──
    ("这个药 剂量 多少", "high_risk", "医疗高风险"),
    ("股票 投资 建议", "high_risk", "投资高风险"),
]


def run() -> tuple[int, int]:
    passed, failed = 0, []
    print("=" * 70)
    print(f"查询分类器回归测试 — {len(CASES)} 用例")
    print("=" * 70)
    for q, expect, note in CASES:
        got = classify_query(q)["type"]
        if got == expect:
            passed += 1
        else:
            failed.append((q, expect, got, note))
    if failed:
        print("\n失败用例:")
        for q, expect, got, note in failed:
            print(f"  ❌ {q}")
            print(f"     期望 {expect} / 实际 {got}  ({note})")
    print(f"\n通过 {passed}/{len(CASES)}")
    return passed, len(CASES)


def test_classify_all():
    """pytest 入口。"""
    passed, total = run()
    assert passed == total, f"{total - passed} 个用例失败"


def test_types_have_sources():
    """每个类型都必须有可用源，且源名合法（防止 'code' 这类幽灵源）。"""
    import intel
    for t, cfg in QUERY_TYPES.items():
        assert cfg["sources"], f"{t} 无源"
        for s in cfg["sources"]:
            assert s in intel.SOURCES, f"{t} 引用了不存在的源: {s}"


if __name__ == "__main__":
    p, t = run()
    raise SystemExit(0 if p == t else 1)
