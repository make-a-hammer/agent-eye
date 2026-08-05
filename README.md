# agent-eye

Playwright persistent Chrome profile for AI agents. A Windows-native, under-200-line alternative to Ego-Lite.

Inherits your real Chrome login state to bypass anti-bot detection. Used as the visual fallback channel in the AI OS search pipeline.

## v2.1 调试协议（debug protocol）

从 [OpenGame debug-skill](https://github.com/leigest519/OpenGame) 吸收的「活的失败协议」：把踩过的坑变成下次直接绕开的结构化资产。

```
失败发生 → protocol.match(签名匹配) → 命中? 执行已知修复 : 未命中交还LLM
        → 修复后重试原操作 → 验证成功才记录 → 同一错误3次 → 泛化为预检规则
```

### 文件

| 文件 | 职责 |
|---|---|
| `seed-protocol/protocol.json` | 领域种子协议：7 条反应式 + 2 条主动式 |
| `protocol.py` | 协议引擎：加载/保存/签名匹配/记录/泛化（零依赖纯标准库） |
| `repair.py` | 修复执行器：按 fix.actions 序列执行行为修复 |
| `loop.py` | 主循环增强：失败时进「诊断→修复→重试」子循环 |

### 协议条目两种类型

- **reactive**（诊断用）：错误出现后签名匹配 errorCode+正则 → 找到根因和已验证修复
- **proactive**（预检用）：执行前主动检查（如「目标为海外域名先探代理」）

### 合规边界

验证码/风控类条目的修复动作只含 **等待/换源/放弃**，不包含破解、绕过。与 `ethics.py` 防火墙一致。

### 测试

```bash
python test_protocol_loop.py   # 4条链路: 失败→修复→重试 / 验证码合规 / 未知错误降级 / 经验计数
```

### 演化机制

- 命中条目 `occurrences += 1`（经验强化）
- 同一 errorCode 累计 ≥ 3 次 → `generalize()` 自动提升为预检规则
- 协议持久化在 `protocol/protocol.json`（首次运行从 seed 自动初始化）
