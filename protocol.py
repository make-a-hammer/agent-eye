#!/usr/bin/env python3
"""
protocol.py — agent-eye 调试协议引擎

从 OpenGame debug-skill 吸收的「活的失败协议」：
    (signature, rootCause, fix) 三元组持久化，
    失败→签名匹配→已知修复，验证成功后记录，重复 3 次→泛化为预检规则。

数据结构与 OpenGame types.ts 对应：
    DebugProtocol  ↔  protocol.json 顶层
    DebugEntry     ↔  entries[] (reactive=诊断用 / proactive=预检用)
    FailureSignature ↔ signature (stage, errorCode, messagePattern)
    ProtocolRule   ↔  rules[] (泛化出的预检规则)

零外部依赖，纯 Python 标准库。
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ─── 路径 ──────────────────────────────────────

PROTOCOL_DIR = Path(__file__).parent / "protocol"
SEED_DIR = Path(__file__).parent / "seed-protocol"
LIVE_PROTOCOL_PATH = PROTOCOL_DIR / "protocol.json"

# 泛化阈值：同一 errorCode 出现 N 次后提升为规则
GENERALIZE_THRESHOLD = 3

# ─── 数据结构 ──────────────────────────────────


@dataclass
class FixAction:
    """一个修复动作。action 是行为指令，detail 是补充说明。"""
    action: str
    detail: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "FixAction":
        return cls(action=d.get("action", ""), detail=d.get("detail", ""))


@dataclass
class DebugEntry:
    """协议原子单元：(signature, rootCause, fix) 三元组。"""
    id: str
    kind: str  # reactive | proactive
    signature: dict
    rootCause: str
    tags: list[str]
    fix: dict
    occurrences: int = 0
    contributingProjects: list[str] = field(default_factory=list)
    createdAt: str = ""
    lastMatchedAt: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "DebugEntry":
        return cls(
            id=d["id"],
            kind=d.get("kind", "reactive"),
            signature=d.get("signature", {}),
            rootCause=d.get("rootCause", ""),
            tags=d.get("tags", []),
            fix=d.get("fix", {}),
            occurrences=d.get("occurrences", 0),
            contributingProjects=d.get("contributingProjects", []),
            createdAt=d.get("createdAt", ""),
            lastMatchedAt=d.get("lastMatchedAt", ""),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "signature": self.signature,
            "rootCause": self.rootCause,
            "tags": self.tags,
            "fix": self.fix,
            "occurrences": self.occurrences,
            "contributingProjects": self.contributingProjects,
            "createdAt": self.createdAt,
            "lastMatchedAt": self.lastMatchedAt,
        }


@dataclass
class ProtocolRule:
    """泛化出的规则：validator 可执行的预检检查。"""
    id: str
    errorCode: str
    description: str
    sourceEntries: list[str] = field(default_factory=list)
    prevented: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "ProtocolRule":
        return cls(
            id=d["id"],
            errorCode=d.get("errorCode", ""),
            description=d.get("description", ""),
            sourceEntries=d.get("sourceEntries", []),
            prevented=d.get("prevented", 0),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "errorCode": self.errorCode,
            "description": self.description,
            "sourceEntries": self.sourceEntries,
            "prevented": self.prevented,
        }


class DebugProtocol:
    """顶层持久化状态：entries + rules + evolutionLog。"""

    def __init__(self, path: Path = LIVE_PROTOCOL_PATH):
        self.path = path
        self.version = 0
        self.createdAt = _now()
        self.updatedAt = _now()
        self.entries: list[DebugEntry] = []
        self.rules: list[ProtocolRule] = []
        self.evolutionLog: list[dict] = []
        self._seed_protocol: dict | None = None

    # ── 加载 ──

    @classmethod
    def load(cls, path: Path = LIVE_PROTOCOL_PATH) -> "DebugProtocol":
        """从磁盘加载；不存在则从 seed 初始化。"""
        p = cls(path)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            p.version = data.get("version", 0)
            p.createdAt = data.get("createdAt", p.createdAt)
            p.updatedAt = data.get("updatedAt", p.updatedAt)
            p.entries = [DebugEntry.from_dict(e) for e in data.get("entries", [])]
            p.rules = [ProtocolRule.from_dict(r) for r in data.get("rules", [])]
            p.evolutionLog = data.get("evolutionLog", [])
        else:
            p._init_from_seed()
            p.save()
        return p

    def _init_from_seed(self):
        """从 seed-protocol/protocol.json 初始化。"""
        seed_path = SEED_DIR / "protocol.json"
        if not seed_path.exists():
            return
        data = json.loads(seed_path.read_text(encoding="utf-8"))
        self._seed_protocol = data
        self.entries = [DebugEntry.from_dict(e) for e in data.get("entries", [])]
        self.evolutionLog.append({
            "ts": _now(),
            "event": "init_from_seed",
            "detail": f"从 {seed_path.name} 初始化 {len(self.entries)} 条条目",
        })

    # ── 保存 ──

    def save(self):
        self.updatedAt = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": self.version,
            "createdAt": self.createdAt,
            "updatedAt": self.updatedAt,
            "seedProtocolPath": "seed-protocol",
            "entries": [e.to_dict() for e in self.entries],
            "rules": [r.to_dict() for r in self.rules],
            "evolutionLog": self.evolutionLog,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ── 查询 ──

    def reactive_entries(self) -> list[DebugEntry]:
        return [e for e in self.entries if e.kind == "reactive"]

    def proactive_entries(self) -> list[DebugEntry]:
        return [e for e in self.entries if e.kind == "proactive"]

    def find_by_id(self, entry_id: str) -> DebugEntry | None:
        for e in self.entries:
            if e.id == entry_id:
                return e
        return None

    def find_by_error_code(self, code: str) -> list[DebugEntry]:
        return [e for e in self.entries if e.signature.get("errorCode") == code]

    def find_by_tag(self, tag: str) -> list[DebugEntry]:
        return [e for e in self.entries if tag in e.tags]

    # ── 匹配 ──

    def match(self, stage: str, error_text: str) -> DebugEntry | None:
        """
        核心签名匹配：用 errorCode 所在的 stage + messagePattern 正则匹配错误文本。
        返回最佳命中的 reactive 条目（含 verified fix），未命中返回 None。

        OpenGame diagnoser 的「signature matching」Python 版。
        """
        best: DebugEntry | None = None
        best_score = 0.0
        for e in self.reactive_entries():
            sig = e.signature
            # stage 匹配：any 通配，否则必须相等
            if sig.get("stage") != "any" and sig.get("stage") != stage:
                continue
            pattern = sig.get("messagePattern", "")
            if not pattern:
                continue
            try:
                m = re.search(pattern, error_text, re.IGNORECASE)
            except re.error:
                continue
            if m:
                # 匹配长度占比作为得分：越长的正则匹配越具体
                score = len(m.group(0)) / max(len(error_text), 1) + 0.5
                if score > best_score:
                    best = e
                    best_score = score
        return best

    # ── 记录与演化 ──

    def record_match(self, entry: DebugEntry, project: str = "agent-eye"):
        """Recorder：命中已有条目 → 计数 +1，更新贡献项目。"""
        entry.occurrences += 1
        entry.lastMatchedAt = _now()
        if project not in entry.contributingProjects:
            entry.contributingProjects.append(project)
        self.evolutionLog.append({
            "ts": _now(),
            "event": "match",
            "detail": f"{entry.id} 命中，occurrences={entry.occurrences}",
        })

    def add_novel_entry(self, entry: DebugEntry, project: str = "agent-eye"):
        """Recorder：新模式 → 追加条目（仅在修复被验证成功后调用）。"""
        entry.contributingProjects = [project]
        self.entries.append(entry)
        self.evolutionLog.append({
            "ts": _now(),
            "event": "novel_entry",
            "detail": f"新增 {entry.id} ({entry.signature.get('errorCode')})",
        })

    def generalize(self) -> list[ProtocolRule]:
        """
        Generalizer：同一 errorCode 的 reactive 条目 ≥ 阈值 → 泛化为规则。
        规则是「下次执行前主动检查」的预检项，validator 会消费它们。
        """
        new_rules = []
        from collections import defaultdict
        groups: dict[str, list[DebugEntry]] = defaultdict(list)
        for e in self.reactive_entries():
            groups[e.signature.get("errorCode", "")].append(e)

        existing_codes = {r.errorCode for r in self.rules}
        for code, group in groups.items():
            if not code:
                continue
            total = sum(e.occurrences for e in group)
            if total >= GENERALIZE_THRESHOLD and code not in existing_codes:
                rule = ProtocolRule(
                    id=f"rule-{code.lower()}",
                    errorCode=code,
                    description=f"预防 {code}：{'；'.join(e.rootCause for e in group[:2])}",
                    sourceEntries=[e.id for e in group],
                )
                self.rules.append(rule)
                new_rules.append(rule)
                self.evolutionLog.append({
                    "ts": _now(),
                    "event": "generalize",
                    "detail": f"{code} 累计 {total} 次 → 泛化为规则 {rule.id}",
                })
        if new_rules:
            self.version += 1
            self.save()
        return new_rules

    # ── 状态报告 ──

    def stats(self) -> dict:
        return {
            "version": self.version,
            "entries": len(self.entries),
            "reactive": len(self.reactive_entries()),
            "proactive": len(self.proactive_entries()),
            "rules": len(self.rules),
            "evolution_events": len(self.evolutionLog),
            "path": str(self.path),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ─── 便捷入口 ──────────────────────────────────


def load_protocol() -> DebugProtocol:
    """加载协议（不存在则从 seed 初始化）。"""
    return DebugProtocol.load()


if __name__ == "__main__":
    import sys
    p = load_protocol()
    print(json.dumps(p.stats(), ensure_ascii=False, indent=2))
    print("\n── 匹配测试 ──")
    tests = [
        ("navigate", "Navigation timeout of 20000 ms exceeded"),
        ("navigate", "net::ERR_NAME_NOT_RESOLVED"),
        ("click", "waiting for selector \"a.link\" to be visible"),
        ("click", "no such element: Unable to locate element"),
        ("navigate", "captcha: please verify you are human"),
        ("navigate", "HTTP 403 Forbidden"),
        ("navigate", "429 Too Many Requests"),
        ("observe", "页面正文为空，SPA 未渲染"),
        ("click", "Execution context was destroyed"),
        ("any", "JSONDecodeError: Expecting value: line 1 column 1"),
        ("navigate", "some unknown error that never matches"),
    ]
    for stage, text in tests:
        hit = p.match(stage, text)
        if hit:
            print(f"  ✅ [{stage}] {text[:45]:45s} → {hit.id} ({hit.signature.get('errorCode')})")
        else:
            print(f"  ❌ [{stage}] {text[:45]:45s} → 未命中")
