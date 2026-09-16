#!/usr/bin/env python3
"""
bench.py — agent-eye 评测基线

没有基线的优化都是自我安慰。本模块用固定查询集量化当前能力，
支持跑前/跑后对比（回答"这次改动到底提升了吗"）。

指标设计（对应架构报告的价值公式）：
    覆盖率   = 有结果的源 / 尝试的源        （权威来源覆盖）
    证据绑定率 = 有 evidence 的 claim / 总 claim  （证据可追溯性）
    置信度分布 = 🟢/🟡/🔴 占比
    新鲜度    = 有 published_days 的条目占比
    缓存命中率 = 命中 / 总请求
    耗时      = 总耗时 + 各源耗时
    失败率    = 失败源 / 尝试源

用法:
    python bench.py                    # 跑完整基准（含 LLM）
    python bench.py --fast             # 只测检索层（无 LLM，快）
    python bench.py --compare baseline.json   # 与历史基线对比
    python bench.py --list-baselines   # 列出历史基线
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASELINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bench")

# ─── 固定查询集（覆盖六类查询）─────────────────

BENCH_QUERIES = [
    {"q": "固态电池 最新进展",           "expect_type": "research"},
    {"q": "iPhone 17 vs 华为 Mate",     "expect_type": "comparative"},
    {"q": "如何安装 n8n",               "expect_type": "operational"},
    {"q": "Transformer 注意力机制",      "expect_type": "research"},
    {"q": "量子计算 应用",               "expect_type": "research"},
]

# 每个查询的期望（用于判断覆盖是否合理）
EXPECTED_MIN_SOURCES = 2      # 至少 2 个源有结果
EXPECTED_MIN_ITEMS = 5        # 至少 5 条结果


# ─── 指标计算 ─────────────────────────────────


def _run_one(query: str, max_per: int, use_llm: bool) -> dict:
    """跑单个查询，返回指标。"""
    from sources import classify_query, score_and_dedup
    import intel

    t0 = time.time()
    qtype = classify_query(query)

    # 选源（复用 intel 逻辑）
    source_list = [s for s in qtype["sources"] if s in intel.SOURCES] or ["web"]

    # 逐源并行跑（与 intel.collect 一致的执行路径）
    from concurrent.futures import ThreadPoolExecutor, as_completed

    all_items, per_source = [], {}
    t_parallel = time.time()

    def _call(name: str):
        ts = time.time()
        try:
            items = intel.SOURCES[name](query, max_per)
            return name, items, int((time.time()-ts)*1000), None
        except Exception as e:
            return name, [], int((time.time()-ts)*1000), str(e)[:60]

    with ThreadPoolExecutor(max_workers=max(1, min(len(source_list), 6))) as ex:
        for fut in as_completed([ex.submit(_call, n) for n in source_list]):
            name, items, ms, err = fut.result()
            per_source[name] = {"items": len(items), "ms": ms, "ok": err is None}
            if err:
                per_source[name]["error"] = err
            all_items.extend(items)

    # 打分排序
    if all_items:
        all_items = score_and_dedup(all_items, query)

    retrieval_ms = int((time.time() - t0) * 1000)
    parallel_wall_ms = int((time.time() - t_parallel) * 1000)

    # 缓存统计
    cache_hits = 0
    try:
        from cache import get_cache
        cs = get_cache().stats()
        cache_hits = cs.get("total_hits", 0)
    except Exception:
        pass

    result = {
        "query": query,
        "detected_type": qtype["type"],
        "type_correct": qtype["type"] == BENCH_QUERIES[
            next(i for i, b in enumerate(BENCH_QUERIES) if b["q"] == query)]["expect_type"]
            if any(b["q"] == query for b in BENCH_QUERIES) else None,
        "sources": source_list,
        "per_source": per_source,
        "sources_ok": sum(1 for v in per_source.values() if v["ok"]),
        "sources_failed": sum(1 for v in per_source.values() if not v["ok"]),
        "total_items": len(all_items),
        "retrieval_ms": retrieval_ms,
        "parallel_wall_ms": parallel_wall_ms,
        "cache_hits": cache_hits,
    }

    # LLM 层指标（证据绑定）
    if use_llm and all_items:
        try:
            from llm_client import create_llm
            llm = create_llm(provider="deepseek")
            tl = time.time()
            report = intel.analyze(query, all_items, llm)
            result["llm_ms"] = int((time.time() - tl) * 1000)

            claims = []
            for g in report.get("groups", []):
                claims.extend(g.get("claims", []))
            total_claims = len(claims)
            bound = sum(1 for c in claims if c.get("evidence"))
            conf = {"high": 0, "medium": 0, "low": 0}
            for c in claims:
                k = str(c.get("confidence", "")).lower()
                if k in conf:
                    conf[k] += 1

            result.update({
                "claims": total_claims,
                "claims_with_evidence": bound,
                "evidence_binding_rate": round(bound / total_claims, 3) if total_claims else None,
                "confidence_high": conf["high"],
                "confidence_medium": conf["medium"],
                "confidence_low": conf["low"],
                "gaps_reported": len(report.get("gaps", [])),
                "groups": len(report.get("groups", [])),
            })
        except Exception as e:
            result["llm_error"] = str(e)[:100]

    result["total_ms"] = int((time.time() - t0) * 1000)
    return result


def _aggregate(runs: list[dict]) -> dict:
    """聚合多个查询的指标。"""
    n = len(runs)
    if not n:
        return {}

    def avg(key, default=0):
        vals = [r.get(key) for r in runs if r.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else default

    total_items = sum(r.get("total_items", 0) for r in runs)
    total_sources = sum(len(r.get("sources", [])) for r in runs)
    ok_sources = sum(r.get("sources_ok", 0) for r in runs)
    failed_sources = sum(r.get("sources_failed", 0) for r in runs)

    claims = sum(r.get("claims", 0) for r in runs)
    bound = sum(r.get("claims_with_evidence", 0) for r in runs)

    type_correct = [r.get("type_correct") for r in runs if r.get("type_correct") is not None]

    agg = {
        "queries": n,
        "coverage_rate": round(ok_sources / total_sources, 3) if total_sources else None,
        "sources_ok": ok_sources,
        "sources_failed": failed_sources,
        "total_items": total_items,
        "avg_items_per_query": round(total_items / n, 1),
        "avg_retrieval_ms": avg("retrieval_ms"),
        "avg_total_ms": avg("total_ms"),
        "type_accuracy": (round(sum(1 for t in type_correct if t) / len(type_correct), 3)
                          if type_correct else None),
    }
    if claims:
        agg.update({
            "total_claims": claims,
            "evidence_binding_rate": round(bound / claims, 3),
            "avg_gaps_per_query": avg("gaps_reported"),
            "avg_groups_per_query": avg("groups"),
        })
    return agg


# ─── 主流程 ───────────────────────────────────


def run_bench(max_per: int = 3, use_llm: bool = True,
              queries: list[dict] | None = None) -> dict:
    """跑完整基准，返回报告。"""
    qs = queries or BENCH_QUERIES
    print(f"🏁 基准测试: {len(qs)} 个查询 | LLM={'开' if use_llm else '关'}\n")

    runs = []
    for i, item in enumerate(qs, 1):
        q = item["q"]
        print(f"[{i}/{len(qs)}] {q}")
        r = _run_one(q, max_per, use_llm)
        flag = "✅" if r.get("sources_ok", 0) >= EXPECTED_MIN_SOURCES else "⚠️"
        print(f"   {flag} 源: {r['sources_ok']}/{len(r['sources'])} | "
              f"条数: {r['total_items']} | 检索: {r['retrieval_ms']}ms"
              + (f" | claims: {r.get('claims',0)} "
                 f"(绑定率 {r.get('evidence_binding_rate')})" if use_llm else ""))
        runs.append(r)

    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {"max_per": max_per, "use_llm": use_llm},
        "aggregate": _aggregate(runs),
        "runs": runs,
    }
    return report


def save_baseline(report: dict, name: str | None = None) -> str:
    """保存基线。"""
    os.makedirs(BASELINE_DIR, exist_ok=True)
    name = name or time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BASELINE_DIR, f"baseline_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path


def compare(baseline_path: str, current: dict) -> dict:
    """对比基线与当前。"""
    with open(baseline_path, encoding="utf-8") as f:
        base = json.load(f)

    b, c = base.get("aggregate", {}), current.get("aggregate", {})
    keys = ["coverage_rate", "total_items", "avg_items_per_query",
            "avg_retrieval_ms", "avg_total_ms", "evidence_binding_rate",
            "total_claims", "avg_gaps_per_query", "type_accuracy"]

    diff = {}
    for k in keys:
        bv, cv = b.get(k), c.get(k)
        if bv is None or cv is None:
            continue
        delta = round(cv - bv, 3)
        pct = round((delta / bv * 100), 1) if bv else None
        # 耗时类指标是"越低越好"
        better = None
        if k.endswith("_ms"):
            better = delta < 0
        elif k in ("coverage_rate", "total_items", "avg_items_per_query",
                   "evidence_binding_rate", "type_accuracy", "total_claims"):
            better = delta > 0 if delta != 0 else None   # 无变化 = 中性
        diff[k] = {"baseline": bv, "current": cv, "delta": delta,
                   "pct": pct, "better": better}
    return diff


def print_comparison(diff: dict):
    """格式化输出对比结果。"""
    print(f"\n{'指标':<24} {'基线':>10} {'当前':>10} {'变化':>10}  ")
    print("-" * 62)
    for k, v in diff.items():
        mark = "⚪"     # 中性（无变化或未分类指标）
        if v["better"] is True:
            mark = "✅"
        elif v["better"] is False:
            mark = "❌"
        pct = f"{v['pct']:+.1f}%" if v.get("pct") is not None else ""
        print(f"{k:<24} {v['baseline']:>10} {v['current']:>10} "
              f"{v['delta']:>+10}  {mark} {pct}")


# ─── CLI ─────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="agent-eye 评测基线")
    p.add_argument("--fast", action="store_true", help="只测检索层（无 LLM）")
    p.add_argument("--max", type=int, default=3, help="每源条数")
    p.add_argument("--save", nargs="?", const="auto", help="保存为本基线")
    p.add_argument("--compare", help="与指定基线文件对比")
    p.add_argument("--list-baselines", action="store_true", help="列出历史基线")
    args = p.parse_args()

    if args.list_baselines:
        if not os.path.exists(BASELINE_DIR):
            print("暂无基线")
            return
        for f in sorted(os.listdir(BASELINE_DIR)):
            p_ = os.path.join(BASELINE_DIR, f)
            with open(p_, encoding="utf-8") as fh:
                d = json.load(fh)
            agg = d.get("aggregate", {})
            print(f"{f}  |  {d.get('timestamp')}  |  "
                  f"条数={agg.get('total_items')} 覆盖={agg.get('coverage_rate')} "
                  f"耗时={agg.get('avg_total_ms')}ms")
        return

    report = run_bench(max_per=args.max, use_llm=not args.fast)
    agg = report["aggregate"]

    print(f"\n{'='*62}")
    print("📊 聚合指标")
    print(f"{'='*62}")
    for k, v in agg.items():
        print(f"  {k:<26} {v}")

    if args.compare:
        if os.path.exists(args.compare):
            diff = compare(args.compare, report)
            print_comparison(diff)
        else:
            print(f"⚠️ 基线文件不存在: {args.compare}")

    if args.save is not None:
        name = None if args.save == "auto" else args.save
        path = save_baseline(report, name)
        print(f"\n💾 基线已保存: {path}")


if __name__ == "__main__":
    main()
