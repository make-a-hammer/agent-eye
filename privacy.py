#!/usr/bin/env python3
"""
privacy.py — 隐私与最小足迹（合规版）

源自架构报告 §7（报告明确立场）：
    "如果'隐秘性'指隐藏爬虫身份、轮换代理/IP、伪造指纹、绕过 bot 检测，
     我不能提供相关设计。真实目标应是减少不必要请求、保护用户隐私、
     降低数据暴露、避免给站点造成负担。"

本模块做的是后一种（合规强化）：
    ① PII 扫描     — 识别内容中的邮箱/手机/身份证/银行卡
    ② PII 最小化   — 掩码或剔除（防止 PII 流入索引/LLM 上下文）
    ③ 数据删除     — 按范围清理缓存/下载记录（支持被遗忘权）
    ④ 访问审计     — 记录访问了什么、为什么（可解释 + 可追责）

用法:
    from privacy import scan_pii, minimize, purge, audit
    hits = scan_pii("联系邮箱 a@b.com 手机 13800138000")
    clean = minimize(text, mode="mask")
    purge(scope="cache", older_than_days=30)
"""

import json
import os
import re
import sqlite3
import time

HOME_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_LOG = os.path.join(HOME_DIR, ".cache", "audit.jsonl")
CACHE_DB = os.path.join(HOME_DIR, ".cache", "index.db")
DL_DB = os.path.join(HOME_DIR, ".cache", "downloads.db")

# ─── ① PII 模式 ────────────────────────────────

PII_PATTERNS = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b"),
    "phone_cn": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "phone_intl": re.compile(r"\+\d{1,3}[-\s]?\d{6,14}\b"),
    "id_card_cn": re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
                             r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"),
    "bank_card": re.compile(r"(?<!\d)(?:\d{4}[-\s]?){3}\d{4}(?!\d)"),
    "ipv4": re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
    "wechat_id": re.compile(r"(?:微信号|wechat|wx)[:：\s]*([a-zA-Z][\w-]{5,19})", re.I),
}

# 排除的 IP（公开 DNS/常见服务，不算 PII）
BENIGN_IPS = {"8.8.8.8", "1.1.1.1", "127.0.0.1", "0.0.0.0", "114.114.114.114"}


def scan_pii(text: str) -> dict:
    """
    扫描文本中的 PII。

    Returns:
        {"email": ["a@b.com"], "phone_cn": [...], "count": 3}
    """
    if not text:
        return {"count": 0}
    hits: dict[str, list[str]] = {}
    for kind, pat in PII_PATTERNS.items():
        found = pat.findall(text)
        if kind == "wechat_id":
            found = [m if isinstance(m, str) else m[0] for m in found]
        if kind == "ipv4":
            found = [ip for ip in found if ip not in BENIGN_IPS]
        if found:
            hits[kind] = list(dict.fromkeys(found))[:20]
    hits["count"] = sum(len(v) for k, v in hits.items() if k != "count")
    return hits


# ─── ② PII 最小化 ──────────────────────────────


def _mask(value: str, show: int = 3) -> str:
    """掩码：保留头尾少量字符。"""
    if len(value) <= show * 2:
        return "*" * len(value)
    return value[:show] + "*" * (len(value) - show * 2) + value[-show:]


def minimize(text: str, mode: str = "mask") -> str:
    """
    PII 最小化。用于防止 PII 流入索引 / embedding / LLM 上下文。

    Args:
        mode: "mask"（掩码保留结构）| "redact"（替换为标记）| "strip"（删除）

    Returns:
        处理后的文本
    """
    if not text:
        return text

    def replace(kind: str):
        def fn(m):
            v = m.group(0)
            if mode == "strip":
                return ""
            if mode == "redact":
                return f"[{kind.upper()}_REDACTED]"
            return _mask(v)
        return fn

    out = text
    for kind, pat in PII_PATTERNS.items():
        if kind == "ipv4":
            def ip_fn(m):
                if m.group(0) in BENIGN_IPS:
                    return m.group(0)
                return _mask(m.group(0)) if mode == "mask" else (
                    "" if mode == "strip" else "[IP_REDACTED]")
            out = pat.sub(ip_fn, out)
        else:
            out = pat.sub(replace(kind), out)
    return out


def minimize_item(item: dict, mode: str = "mask") -> dict:
    """对 intel 结果条目做 PII 最小化（title/snippet）。"""
    out = dict(item)
    for field in ("title", "snippet", "body"):
        if out.get(field):
            out[field] = minimize(out[field], mode=mode)
    return out


# ─── ③ 数据删除（被遗忘权）─────────────────────


def purge(scope: str = "all", older_than_days: float | None = None,
          url_pattern: str | None = None) -> dict:
    """
    数据删除。

    Args:
        scope: "cache"（内容缓存）| "downloads"（下载记录）| "audit"（审计日志）| "all"
        older_than_days: 只删除早于 N 天的
        url_pattern: 只删除匹配的 URL（子串匹配）

    Returns:
        {"cache_entries": n, "download_records": n, "audit_lines": n}
    """
    result = {"cache_entries": 0, "download_records": 0, "audit_lines": 0}
    cutoff = (time.time() - older_than_days * 86400) if older_than_days else None

    # 缓存
    if scope in ("cache", "all") and os.path.exists(CACHE_DB):
        try:
            con = sqlite3.connect(CACHE_DB)
            sql, params = "DELETE FROM entries WHERE 1=1", []
            if cutoff:
                sql += " AND fetched_at < ?"
                params.append(cutoff)
            if url_pattern:
                sql += " AND url LIKE ?"
                params.append(f"%{url_pattern}%")
            cur = con.execute(sql, params)
            result["cache_entries"] = cur.rowcount
            con.commit()
            con.close()
        except Exception:
            pass

    # 下载记录（同时删文件）
    if scope in ("downloads", "all") and os.path.exists(DL_DB):
        try:
            con = sqlite3.connect(DL_DB)
            con.row_factory = sqlite3.Row
            sql, params = "SELECT sha256, path FROM downloads WHERE 1=1", []
            if cutoff:
                sql += " AND downloaded_at < ?"
                params.append(cutoff)
            if url_pattern:
                sql += " AND url LIKE ?"
                params.append(f"%{url_pattern}%")
            rows = con.execute(sql, params).fetchall()
            for r in rows:
                try:
                    if r["path"] and os.path.exists(r["path"]):
                        os.remove(r["path"])
                except OSError:
                    pass
            cur = con.execute(
                "DELETE FROM downloads WHERE sha256 IN ({})".format(
                    ",".join("?" * len(rows))), [r["sha256"] for r in rows]
            ) if rows else None
            result["download_records"] = len(rows)
            con.commit()
            con.close()
        except Exception:
            pass

    # 审计日志
    if scope in ("audit", "all") and os.path.exists(AUDIT_LOG):
        try:
            with open(AUDIT_LOG, encoding="utf-8") as f:
                lines = f.readlines()
            if cutoff or url_pattern:
                keep, removed = [], 0
                for ln in lines:
                    try:
                        rec = json.loads(ln)
                        ts = rec.get("ts", 0)
                        u = rec.get("target", "")
                        drop = ((cutoff and ts < cutoff) or
                                (url_pattern and url_pattern in u))
                        if drop:
                            removed += 1
                        else:
                            keep.append(ln)
                    except json.JSONDecodeError:
                        keep.append(ln)
                with open(AUDIT_LOG, "w", encoding="utf-8") as f:
                    f.writelines(keep)
                result["audit_lines"] = removed
            else:
                result["audit_lines"] = len(lines)
                with open(AUDIT_LOG, "w", encoding="utf-8") as f:
                    f.write("")
        except Exception:
            pass

    return result


# ─── ④ 访问审计 ────────────────────────────────


def audit(action: str, target: str, purpose: str = "",
          source_id: str = "", detail: str = "", contains_pii: bool = False):
    """
    记录一次访问/操作（可解释 + 可追责）。

    报告 §7：「提供抓取日志、站点管理方联系渠道和 opt-out 流程」
    """
    os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
    rec = {
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "target": target[:300],
        "purpose": purpose,
        "source_id": source_id,
        "detail": detail[:200],
        "pii_detected": contains_pii,
        "identity": "agent-eye/3.0 (personal research; contact via repo)",
    }
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


def audit_summary(limit: int = 50) -> dict:
    """审计摘要：按来源/动作统计。"""
    if not os.path.exists(AUDIT_LOG):
        return {"total": 0, "by_action": {}, "by_source": {}, "pii_flagged": 0}
    by_action: dict[str, int] = {}
    by_source: dict[str, int] = {}
    pii = 0
    total = 0
    try:
        with open(AUDIT_LOG, encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                total += 1
                by_action[r.get("action", "?")] = by_action.get(r.get("action", "?"), 0) + 1
                by_source[r.get("source_id", "?")] = by_source.get(r.get("source_id", "?"), 0) + 1
                if r.get("pii_detected"):
                    pii += 1
    except Exception:
        pass
    return {"total": total, "by_action": by_action,
            "by_source": by_source, "pii_flagged": pii}


# ─── CLI ─────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="隐私与最小足迹")
    p.add_argument("--scan", help="扫描文本中的 PII")
    p.add_argument("--minimize", help="最小化文本中的 PII")
    p.add_argument("--mode", default="mask", choices=["mask", "redact", "strip"])
    p.add_argument("--purge", choices=["cache", "downloads", "audit", "all"])
    p.add_argument("--days", type=float, help="只删除早于 N 天的")
    p.add_argument("--pattern", help="只删除 URL 匹配的")
    p.add_argument("--audit-summary", action="store_true")
    args = p.parse_args()

    if args.scan:
        print(json.dumps(scan_pii(args.scan), ensure_ascii=False, indent=2))
    elif args.minimize:
        print(minimize(args.minimize, mode=args.mode))
    elif args.purge:
        print(json.dumps(purge(scope=args.purge, older_than_days=args.days,
                               url_pattern=args.pattern),
                         ensure_ascii=False, indent=2))
    elif args.audit_summary:
        print(json.dumps(audit_summary(), ensure_ascii=False, indent=2))
    else:
        p.print_help()
