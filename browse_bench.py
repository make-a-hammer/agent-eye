#!/usr/bin/env python3
"""
browse_bench.py — agent-eye 浏览层基线（"像人一样浏览"能力评测）

对标 bench.py（检索层基线），对 loop.run_agent 的 vision→think→act 循环
做可复现评测：本地 HTML 夹具 + 固定任务集 + 指标 + 历史对比。

为什么用本地夹具而不是真站：
  - 可复现：真实网站随时改版，基线会漂移
  - 零风险：不触发任何反爬/风控（合规优先）
  - 快：无网络延迟、无代理依赖
真实网络的鲁棒性另行专门测试。

用法:
    python3 browse_bench.py --fast --save 浏览层_v1
    python3 browse_bench.py --compare .bench/baseline_浏览层_v1.json
    python3 browse_bench.py --live          # 附加真实站点探针（需代理）

指标（9 项）:
    success_rate     任务成功率            —— 能不能完成任务
    avg_steps        平均步数              —— 效率（人不会盲目点 N 次）
    step_efficiency  步数效率 = 成功数/总步数
    graceful_rate    优雅停止率            —— 不崩、不死循环
    stall_rate       停滞检测触发率        —— 边际收益递减感知
    obs_completeness observation 字段完整率 —— vision 质量
    wall_cv          任务耗时变异系数      —— 节奏波动「粗代理」
                     ⚠️ 真·步级拟人度需多步任务；当前主集多为单步，此项测不准
    total_wall_ms    总耗时
    avg_wall_ms      平均耗时
"""

import argparse
import asyncio
import functools
import http.server
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop import run_agent  # noqa: E402

BENCH_DIR = Path(__file__).resolve().parent / ".bench"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """只读静态服务，静默日志（camofox 拒绝 file://，夹具必须走 http）。"""

    def log_message(self, *args):  # noqa: D102
        pass


def serve_dir(directory: Path) -> tuple:
    """在随机端口起本地静态服务器，返回 (base_url, httpd)。"""
    handler = functools.partial(_QuietHandler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd

# ── 任务集：9 个，覆盖 观察 / 判断 / 鲁棒 / 边界 / 正文提取 ──────────
# human_steps = **人类最优路径步数**（专家基准）。agent 步数 / human_steps = human_ratio，
# >1 表示 agent 绕了路（这正是 OSWorld-Human 那类「人类参照」要暴露的东西）。
TASKS = [
    {
        "id": "t1_title_hit",
        "desc": "标题命中 —— vision 拿到 title，thinker 应利用它",
        "html": ("<html><head><meta charset=\"utf-8\"><title>Alpha Report 2026</title></head>"
                 "<body><p>nothing relevant in the body</p></body></html>"),
        "query": "Alpha Report 2026",
        "expect": "success",
        "human_steps": 1,
        "max_steps": 2,
    },
    {
        "id": "t2_body_hit",
        "desc": "正文命中 —— fallback 基本盘",
        "html": ("<html><head><meta charset=\"utf-8\"><title>Doc</title></head>"
                 "<body><p>quantum battery breakthrough 2026</p></body></html>"),
        "query": "quantum battery",
        "expect": "success",
        "human_steps": 1,
        "max_steps": 2,
    },
    {
        "id": "t3_no_match",
        "desc": "无匹配 —— 应优雅结束，不死循环",
        "html": ("<html><head><meta charset=\"utf-8\"><title>Lorem</title></head>"
                 "<body><p>lorem ipsum dolor sit amet</p></body></html>"),
        "query": "zzz-nonexistent-keyword",
        "expect": "graceful",
        "human_steps": 1,
        "max_steps": 5,
    },
    {
        "id": "t4_empty_page",
        "desc": "空页 —— 应停滞检测或立即停止",
        "html": "<html><head><meta charset=\"utf-8\"><title></title></head><body></body></html>",
        "query": "anything",
        "expect": "graceful",
        "human_steps": 1,
        "max_steps": 5,
    },
    {
        "id": "t5_bad_url",
        "desc": "文件不存在 —— 应优雅失败，不抛异常",
        "html": None,
        "query": "x",
        "expect": "graceful",
        "human_steps": 1,
        "max_steps": 3,
    },
    {
        "id": "t6_noisy_page",
        "desc": "导航栏噪音 + 正文在后 —— 测正文提取（百度那个坑的抽象）",
        "html": ("<html><head><meta charset=\"utf-8\"><title>Article</title></head><body>"
                 "<nav><a href='/'>Home</a><a href='/about'>About</a>"
                 "<a href='/login'>Login</a><a href='/signup'>Sign up</a></nav>"
                 "<aside>Related: nothing here worth reading at all</aside>"
                 "<main><h1>Solid Battery Report</h1>"
                 "<p>固态电池用固体电解质替代液态电解液，安全性更高，"
                 "能量密度理论突破 500Wh/kg。</p></main>"
                 "</body></html>"),
        "query": "solid battery",
        "expect": "success",
        "human_steps": 1,
        "max_steps": 2,
    },
    {
        "id": "t7_table_page",
        "desc": "表格页 —— 结构化数据提取",
        "html": ("<html><head><meta charset=\"utf-8\"><title>Price Table</title></head><body>"
                 "<h1>Prices</h1><table>"
                 "<tr><th>Item</th><th>Price</th></tr>"
                 "<tr><td>Alpha</td><td>100</td></tr>"
                 "<tr><td>Beta</td><td>250</td></tr>"
                 "</table><p>All prices in CNY.</p></body></html>"),
        "query": "Alpha",
        "expect": "success",
        "human_steps": 1,
        "max_steps": 2,
    },
    {
        "id": "t8_deep_keyword",
        "desc": "长页面深处关键词 —— 测截断是否切掉目标",
        "html": ("<html><head><meta charset=\"utf-8\"><title>Long Doc</title></head><body>"
                 + "<p>" + ("filler sentence about nothing much. " * 120) + "</p>"
                 + "<p>target marker: DEEPKEYWORD2026 found here.</p>"
                 "</body></html>"),
        "query": "DEEPKEYWORD2026",
        "expect": "success",
        "human_steps": 1,
        "max_steps": 3,
    },
    {
        "id": "t9_multi_lang",
        "desc": "中英混合页 —— 关键词在中文段落",
        "html": ("<html><head><meta charset=\"utf-8\"><title>Mixed</title></head><body>"
                 "<h1>Report</h1><p>English summary here.</p>"
                 "<p>中文结论：固态电池将在 2027 年小批量装车。</p>"
                 "</body></html>"),
        "query": "2027 年小批量装车",
        "expect": "success",
        "human_steps": 1,
        "max_steps": 2,
    },
]

LIVE_PROBES = [
    # 真实站探针（--live 时附加）；需要代理，故与主集分离
    {"id": "live_example", "url": "https://example.com", "query": "Example Domain",
     "expect": "graceful", "max_steps": 2},
]


async def run_one(task: dict, url: str, llm, headless: bool,
                  engine: str = "playwright") -> dict:
    """跑单个任务，返回结构化结果。"""
    t0 = time.time()
    steps: list[float] = []
    exception = ""
    try:
        r = await run_agent(
            url, task["query"], llm=llm,
            max_steps=task.get("max_steps", 3), headless=headless,
            engine=engine,
        )
    except Exception as e:  # noqa: BLE001 —— 基线必须记录崩溃而非被它中断
        r = {"success": False, "steps_taken": 0, "history": [],
             "result": f"EXCEPTION: {type(e).__name__}: {e}"}
        exception = f"{type(e).__name__}: {e}"
    wall_ms = int((time.time() - t0) * 1000)

    history = r.get("history", []) or []
    # obs 质量：history 里每步都该有 url
    obs_ok = sum(1 for h in history if h.get("url"))
    obs_completeness = (obs_ok / len(history)) if history else 0.0

    # 步间隔（拟人度）：用 history 无时间戳，故此处仅记录步数节奏代理
    for _ in history:
        steps.append(1.0)

    success = bool(r.get("success"))
    stop_reason = r.get("stop_reason", "")
    graceful = (not exception)  # 不崩即为优雅（含正常 done/stalled/max_steps）

    return {
        "id": task["id"],
        "desc": task["desc"],
        "expect": task["expect"],
        "human_steps": task.get("human_steps", 1),
        "success": success,
        "steps": r.get("steps_taken", 0),
        "stop_reason": stop_reason,
        "exception": exception,
        "graceful": graceful,
        "obs_completeness": round(obs_completeness, 3),
        "wall_ms": wall_ms,
        "result": (r.get("result") or "")[:120].replace("\n", " "),
    }


# ── 交互任务集：2 个，测「元素引用 vs CSS selector」能否完成真实交互 ──
# 这两个任务**不走 run_agent**（fallback 不会主动点击），直接测 session 接口。
# 这是能证明 camofox 元素引用价值的最小实验：同一意图，两种定位策略。
INTERACT_TASKS = [
    {
        "id": "t6_click_link",
        "desc": "点击链接跳转",
        "pages": {
            "t6_click_link.html": (
                '<html><head><meta charset="utf-8"><title>Start</title></head><body>'
                '<h1>Start Page</h1>'
                '<a href="t6_target.html">Go to target page</a>'
                '</body></html>'),
            "t6_target.html": (
                '<html><head><meta charset="utf-8"><title>Target</title></head><body>'
                '<h1>Target Page</h1><p>you arrived</p></body></html>'),
        },
        "entry": "t6_click_link.html",
        "assert_url_contains": "t6_target",
        "human_steps": 2,          # 点一下 + 看结果
    },
    {
        "id": "t7_fill_form",
        "desc": "填表并提交",
        "pages": {
            "t7_fill_form.html": (
                '<html><head><meta charset="utf-8"><title>Form</title></head><body>'
                '<h1>Search</h1>'
                '<form action="t7_result.html" method="get">'
                '<input type="text" name="q" placeholder="keyword">'
                '<button type="submit">Go</button>'
                '</form></body></html>'),
            "t7_result.html": (
                '<html><head><meta charset="utf-8"><title>Result</title></head><body>'
                '<h1>Result Page</h1><p>search done</p></body></html>'),
        },
        "entry": "t7_fill_form.html",
        "assert_url_contains": "t7_result",
        "human_steps": 3,          # 输入 + 提交 + 看结果
    },
    {
        "id": "t8_pick_by_name",
        "desc": "导航栏陷阱里精确点目标（语义定位 vs 默认选第一个 a）",
        "pages": {
            "t8_pick_by_name.html": (
                '<html><head><meta charset="utf-8"><title>Nav Trap</title></head><body>'
                '<nav><a href="/">Home</a><a href="/about">About</a></nav>'
                '<h1>Article</h1><p>content here</p>'
                '<a href="t8_target.html">Read the full article</a>'
                '</body></html>'),
            "t8_target.html": (
                '<html><head><meta charset="utf-8"><title>T8 Target</title></head><body>'
                '<h1>Arrived at target</h1></body></html>'),
        },
        "entry": "t8_pick_by_name.html",
        "assert_url_contains": "t8_target",
        "human_steps": 2,          # 找到目标链接 + 点击
    },
    {
        "id": "t9_scroll_find",
        "desc": "目标在首屏之外 —— 需要滚动才能看到",
        "pages": {
            "t9_scroll_find.html": (
                '<html><head><meta charset="utf-8"><title>Scroll</title></head><body>'
                '<h1>Top</h1>'
                + '<p>' + ('spacer content here. ' * 50) + '</p>'
                + '<a href="t9_target.html">Deep link target</a>'
                '</body></html>'),
            "t9_target.html": (
                '<html><head><meta charset="utf-8"><title>T9 Target</title></head><body>'
                '<h1>arrived at deep</h1></body></html>'),
        },
        "entry": "t9_scroll_find.html",
        "assert_url_contains": "t9_target",
        "scroll_first": True,
        "clicks": ["Deep link target"],
        "human_steps": 2,          # 滚动 + 点击
    },
    {
        "id": "t10_multi_field",
        "desc": "多个输入框 + 提交",
        "pages": {
            "t10_multi_field.html": (
                '<html><head><meta charset="utf-8"><title>Multi</title></head><body>'
                '<h1>Form</h1>'
                '<form action="t10_result.html" method="get">'
                '<input type="text" name="a" placeholder="first field">'
                '<input type="text" name="b" placeholder="second field">'
                '<button type="submit">Send</button>'
                '</form></body></html>'),
            "t10_result.html": (
                '<html><head><meta charset="utf-8"><title>T10 Result</title></head><body>'
                '<h1>submitted</h1></body></html>'),
        },
        "entry": "t10_multi_field.html",
        "assert_url_contains": "t10_result",
        "type_first_textbox": "value1",
        "clicks": ["Send"],
        "human_steps": 3,          # 填 + 提交 + 看
    },
    {
        "id": "t11_two_level",
        "desc": "两级导航（列表 → 详情 → 全文）",
        "pages": {
            "t11_two_level.html": (
                '<html><head><meta charset="utf-8"><title>List</title></head><body>'
                '<h1>Items</h1><a href="t11_mid.html">Open item detail</a>'
                '</body></html>'),
            "t11_mid.html": (
                '<html><head><meta charset="utf-8"><title>Mid</title></head><body>'
                '<h1>Detail</h1><a href="t11_target.html">Full text</a></body></html>'),
            "t11_target.html": (
                '<html><head><meta charset="utf-8"><title>T11 Target</title></head><body>'
                '<h1>final page</h1></body></html>'),
        },
        "entry": "t11_two_level.html",
        "assert_url_contains": "t11_target",
        "clicks": ["Open item detail", "Full text"],
        "human_steps": 3,          # 点进详情 + 点全文 + 看
    },
    {
        "id": "t12_search_then_open",
        "desc": "搜索提交后从结果点进目标（百度任务的抽象）",
        "pages": {
            "t12_search_then_open.html": (
                '<html><head><meta charset="utf-8"><title>Search</title></head><body>'
                '<h1>Search</h1>'
                '<form action="t12_results.html" method="get">'
                '<input type="text" name="q" placeholder="keyword">'
                '<button type="submit">Go</button></form>'
                '</body></html>'),
            "t12_results.html": (
                '<html><head><meta charset="utf-8"><title>Results</title></head><body>'
                '<h1>Results</h1>'
                '<a href="t12_target.html">Search result item</a>'
                '</body></html>'),
            "t12_target.html": (
                '<html><head><meta charset="utf-8"><title>T12 Target</title></head><body>'
                '<h1>article body here</h1></body></html>'),
        },
        "entry": "t12_search_then_open.html",
        "assert_url_contains": "t12_target",
        "type_first_textbox": "query",
        "submit_after_type": True,   # 不提交就不会到结果页（曾漏了这条，白点一路）
        "clicks": ["Search result item"],
        "human_steps": 4,          # 输入 + 提交 + 点结果 + 看正文
    },
]


def _cur_url(hand) -> str:
    """交互后取真实当前 URL：走一次 extract（两个后端都返回 url）。"""
    try:
        r = hand._send({"action": "extract"})
        return (r or {}).get("url") or getattr(hand, "url", "") or ""
    except Exception:  # noqa: BLE001
        return getattr(hand, "url", "") or ""


async def run_interact(task: dict, base_url: str, engine: str) -> dict:
    """
    交互任务：直接测 session 接口。
    camofox 走**元素引用**（refs() → e1/e2），playwright 走 CSS selector ——
    比的是「同一意图，两种定位策略谁更靠得住」。
    """
    from hand import BrowserSession
    if engine == "camofox":
        from hand import CamofoxSession
        sess = CamofoxSession()
    else:
        sess = BrowserSession(headless=True)

    t0 = time.time()
    used = ""
    fail = ""
    final_url = ""

    def out(success: bool) -> dict:
        return {"id": task["id"], "desc": task["desc"], "engine": engine,
                "success": success, "wall_ms": int((time.time() - t0) * 1000),
                "used": used, "detail": fail or ("OK" if success else "未达成"),
                "human_steps": task.get("human_steps", 1),
                "final_url": final_url}

    try:
        async with sess as hand:
            ok, detail = await hand.navigate(f"{base_url}/{task['entry']}")
            if not ok:
                fail = f"导航失败: {detail}"
                return out(False)

            if task["id"] == "t6_click_link":
                if engine == "camofox":
                    link = next((r for r in hand.refs() if r["role"] == "link"), None)
                    if not link:
                        fail = "快照里没有 link 引用"
                        return out(False)
                    used = f"ref {link['ref']} ({link['name'][:22]})"
                    await hand.click(link["ref"])
                else:
                    used = "CSS 'a'"
                    await hand.click("a")

            elif task["id"] == "t7_fill_form":
                if engine == "camofox":
                    box = next((r for r in hand.refs() if r["role"] == "textbox"), None)
                    if not box:
                        fail = "快照里没有 textbox 引用"
                        return out(False)
                    used = f"ref {box['ref']} (textbox) + "
                    await hand.type_text(box["ref"], "test")
                    btn = next((r for r in hand.refs() if r["role"] == "button"), None)
                    if btn:
                        used += f"ref {btn['ref']} (button)"
                        await hand.click(btn["ref"])
                    else:
                        used += "submit=True"
                        await hand.type_text(box["ref"], "\n", submit=True)
                else:
                    used = "CSS input[name=q] + button"
                    await hand.type_text("input[name=q]", "test")
                    await hand.click("button")

            elif task["id"] == "t8_pick_by_name":
                if engine == "camofox":
                    # 语义定位：从快照引用表里按**名字**找目标
                    link = next((r for r in hand.refs()
                                 if "full article" in r["name"].lower()), None)
                    if not link:
                        names = [r["name"][:18] for r in hand.refs()]
                        fail = f"引用表里没有目标链接（现有: {names[:6]}）"
                        return out(False)
                    used = f"ref {link['ref']} ({link['name'][:24]})"
                    await hand.click(link["ref"])
                else:
                    # 结构定位：最自然的默认 —— 第一个 <a>（这里会命中导航栏 Home）
                    used = "CSS 'a'（默认选第一个）"
                    await hand.click("a")

            else:
                # ── 声明式通用执行：scroll_first / type_first_textbox / clicks[] ──
                if task.get("scroll_first"):
                    await hand.scroll(800)
                    used += "scroll; "

                if task.get("type_first_textbox"):
                    txt = task["type_first_textbox"]
                    do_submit = bool(task.get("submit_after_type"))
                    if engine == "camofox":
                        box = next((r for r in hand.refs() if r["role"] == "textbox"), None)
                        if not box:
                            fail = "引用表里没有 textbox"
                            return out(False)
                        used += (f"ref {box['ref']}(textbox)='{txt}'"
                                 f"{'+submit' if do_submit else ''}; ")
                        await hand.type_text(box["ref"], txt, submit=do_submit)
                    else:
                        used += f"CSS input='{txt}'; "
                        await hand.type_text("input", txt)
                    # 提交后页面已变，等它加载完并**刷新快照** ——
                    # 否则 refs 还是旧页面的（t12 曾因此点错；wait 分支本身不刷快照）
                    await hand.wait(900)
                    if engine == "camofox":
                        hand._refresh()

                for name in task.get("clicks", []):
                    if engine == "camofox":
                        link = next((r for r in hand.refs()
                                     if name.lower() in (r["name"] or "").lower()), None)
                        if not link:
                            # 回退：提交按钮常无可读 name，按 role 找 button
                            link = next((r for r in hand.refs() if r["role"] == "button"), None)
                        if not link:
                            fail = f"引用表里找不到 '{name}'"
                            return out(False)
                        used += f"ref {link['ref']}({(link['name'] or name)[:16]}); "
                        await hand.click(link["ref"])
                    else:
                        used += f"CSS text={name}; "
                        await hand.click(f"text={name}")

            final_url = _cur_url(hand)
            success = task["assert_url_contains"] in (final_url or "")
            if not success:
                fail = f"URL 未含 '{task['assert_url_contains']}'"
            return out(success)
    except Exception as e:  # noqa: BLE001
        fail = f"EXCEPTION {type(e).__name__}: {e}"
        return out(False)


async def run_interact_suite(args, tmpdir: Path, base_url: str) -> int:
    """交互层基线：对比两种定位策略。"""
    print(f"🖱  交互层基线：{len(INTERACT_TASKS)} 个任务（engine={args.engine}）\n")
    results = []
    for task in INTERACT_TASKS:
        for name, html in task["pages"].items():
            (tmpdir / name).write_text(html, encoding="utf-8")
        res = await run_interact(task, base_url, args.engine)
        results.append(res)
        flag = "✅" if res["success"] else "❌"
        print(f"  {flag} {res['id']:<15} {res['wall_ms']:>6}ms")
        print(f"       定位: {res['used']}")
        print(f"       结果: {res['detail'][:58]}")
        print(f"       URL : {res['final_url'][-46:]}")

    n_ok = sum(1 for r in results if r["success"])
    h_total = sum(r.get("human_steps", 1) for r in results)
    metrics = {
        "tasks": len(results),
        "interact_success_rate": round(n_ok / len(results), 3) if results else 0.0,
        "success_n": f"{n_ok}/{len(results)}",
        "human_steps_total": h_total,
        "avg_wall_ms": int(statistics.mean([r["wall_ms"] for r in results])) if results else 0,
    }
    print("\n" + "=" * 70)
    print(f"📊 交互成功率 {metrics['success_n']}   平均耗时 {metrics['avg_wall_ms']}ms"
          f"   人类步数合计 {h_total}")

    if args.save:
        BENCH_DIR.mkdir(exist_ok=True)
        out = BENCH_DIR / f"baseline_交互层_{args.save}.json"
        out.write_text(json.dumps(
            {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
             "engine": args.engine, "metrics": metrics, "results": results},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"💾 已存 {out}")
    return 0


def verdict(res: dict) -> str:
    """判定 PASS/FAIL。"""
    if res["expect"] == "success":
        return "PASS" if res["success"] else "FAIL"
    # graceful：不崩 + 有正常停止路径
    if not res["graceful"]:
        return "FAIL"
    return "PASS"


async def main_async(args) -> int:
    tmppath = tempfile.mkdtemp(prefix="browsebench_")
    tmpdir = Path(tmppath)
    base_url, httpd = serve_dir(tmpdir)   # camofox 拒绝 file://，夹具走本地 HTTP

    # ── 交互层模式（--interact）：不走 run_agent，直接测 session 接口 ──
    if args.interact:
        rc = await run_interact_suite(args, tmpdir, base_url)
        httpd.shutdown()
        return rc

    llm = None  # --fast / 默认：无 LLM fallback 路径
    if not args.fast:
        try:
            from llm_client import get_llm
            llm = get_llm()
            print("• 使用 LLM 决策")
        except Exception as e:  # noqa: BLE001
            print(f"• LLM 不可用（{type(e).__name__}）→ 回落 fallback 路径")

    tasks = list(TASKS)
    if args.live:
        for p in LIVE_PROBES:
            tasks.append({**p, "html": None, "_live_url": p["url"], "desc": f"[live] {p['id']}"})

    results = []
    print(f"🖥  浏览层基线：{len(tasks)} 个任务（engine={args.engine}）\n")
    for task in tasks:
        if task.get("_live_url"):
            url = task["_live_url"]
        elif task["html"] is None:
            url = f"{base_url}/does_not_exist.html"
        else:
            p = tmpdir / f"{task['id']}.html"
            p.write_text(task["html"], encoding="utf-8")
            url = f"{base_url}/{task['id']}.html"

        res = await run_one(task, url, llm, args.headless, engine=args.engine)
        results.append(res)
        v = verdict(res)
        flag = "✅" if v == "PASS" else "❌"
        print(f"  {flag} {res['id']:<16} steps={res['steps']} "
              f"stop={res['stop_reason'] or '-':<9} {res['wall_ms']:>6}ms  {res['result'][:44]}")

    # ── 指标聚合 ──────────────────────────────────────────────
    n = len(results)
    # success_rate 只统计「期望成功」的任务 —— graceful 任务的 success 语义不同
    # （404 页「成功打开」和「优雅失败」都是正确结果，不该混进成功率）
    succ_tasks = [r for r in results if r["expect"] == "success"]
    n_succ = sum(1 for r in succ_tasks if r["success"])
    n_graceful = sum(1 for r in results if r["graceful"])
    n_stall = sum(1 for r in results if r["stop_reason"] == "stalled")
    total_steps = sum(r["steps"] for r in results)
    obs_vals = [r["obs_completeness"] for r in results if r["steps"] > 0]
    walls = [r["wall_ms"] for r in results]

    # 人类参照：只算**成功**任务 —— 失败任务步数少会把比值拉低，那是假象
    ok_res = [r for r in results if r["success"]]
    h_steps = sum(r.get("human_steps", 1) for r in ok_res)
    a_steps = sum(r["steps"] for r in ok_res)
    human_ratio = round(a_steps / h_steps, 3) if h_steps else 0.0
    metrics = {
        "success_rate": round(n_succ / len(succ_tasks), 3) if succ_tasks else 0.0,
        "success_n": f"{n_succ}/{len(succ_tasks)}",
        "human_ratio": human_ratio,
        "avg_steps": round(total_steps / n, 2),
        "step_efficiency": round(n_succ / total_steps, 3) if total_steps else 0.0,
        "graceful_rate": round(n_graceful / n, 3),
        "stall_rate": round(n_stall / n, 3),
        "obs_completeness": round(statistics.mean(obs_vals), 3) if obs_vals else 0.0,
        "wall_cv": round(statistics.pstdev(walls) / statistics.mean(walls), 3)
        if len(walls) > 1 and statistics.mean(walls) else 0.0,
        "total_wall_ms": sum(walls),
        "avg_wall_ms": int(statistics.mean(walls)) if walls else 0,
        "tasks": n,
    }

    print("\n" + "=" * 70)
    print("📊 指标")
    print("-" * 70)
    for k, v in metrics.items():
        if k in ("tasks",):
            continue
        print(f"  {k:<20} {v}")

    # ── 存档 ─────────────────────────────────────────────────
    payload = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
               "metrics": metrics,
               "results": [{k: v for k, v in r.items()} for r in results]}

    if args.save:
        BENCH_DIR.mkdir(exist_ok=True)
        out = BENCH_DIR / f"baseline_浏览层_{args.save}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n💾 已存 {out}")

    if args.compare:
        cp = Path(args.compare)
        if not cp.exists():
            print(f"\n⚠️  对比基线不存在: {cp}")
        else:
            old = json.loads(cp.read_text(encoding="utf-8"))
            om = old["metrics"]
            print("\n" + "=" * 70)
            print(f"📈 对比 {cp.name}（{old['timestamp']}）")
            print("-" * 70)
            # 指标方向性：并非"越大越好"
            LOWER_BETTER = {"total_wall_ms", "avg_wall_ms", "avg_steps"}
            NEUTRAL = {"stall_rate", "wall_cv"}   # 触发率/波动率，无绝对好坏
            for k, v in metrics.items():
                if k in ("tasks",) or k not in om:
                    continue
                o = om[k]
                # 非数值指标（如 success_n="2/2"）不做减法
                if not isinstance(v, (int, float)) or not isinstance(o, (int, float)):
                    print(f"  {k:<20} {o} → {v}  ⚪ （非数值）")
                    continue
                delta = v - o
                if abs(delta) <= 1e-9:
                    print(f"  {k:<20} {o} → {v}  ⚪ 持平")
                elif k in NEUTRAL:
                    print(f"  {k:<20} {o} → {v}  ⚪ {delta:+.3f}（中性）")
                else:
                    better = (delta < 0) if k in LOWER_BETTER else (delta > 0)
                    mark = "✅" if better else "❌"
                    print(f"  {k:<20} {o} → {v}  {mark} {delta:+.3f}")

    httpd.shutdown()
    return 0 if all(verdict(r) == "PASS" for r in results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="agent-eye 浏览层基线")
    ap.add_argument("--fast", action="store_true", help="跳过 LLM，用 fallback 决策")
    ap.add_argument("--save", metavar="NAME", help="存档为 baseline_浏览层_NAME.json")
    ap.add_argument("--compare", metavar="PATH", help="与历史基线对比")
    ap.add_argument("--live", action="store_true", help="附加真实站点探针（需代理）")
    ap.add_argument("--headless", action="store_true", default=True, help="无头模式（默认开）")
    ap.add_argument("--show", dest="headless", action="store_false", help="显示浏览器窗口")
    ap.add_argument("--engine", default="playwright", choices=["playwright", "camofox"],
                    help="浏览器后端（camofox 需先 bash camofox/start.sh）")
    ap.add_argument("--interact", action="store_true",
                    help="交互层模式：测点击/填表（元素引用 vs CSS selector）")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
