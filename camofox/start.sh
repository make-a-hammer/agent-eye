#!/usr/bin/env bash
# 启动 camofox-browser —— agent-eye 的浏览器后端之一
#
# 用法:  bash camofox/start.sh
# 停:    Ctrl+C （或 bash camofox/stop.sh）
#
# 合规说明（与 agent-eye 的 ethics.py 红线一致）:
#   - 关闭遥测上报：CAMOFOX_CRASH_REPORT_ENABLED=false（隐私最小化）
#   - 不配置代理轮换/住宅代理 → 不启用它的反检测网络栈
#   - 只用它的「能力层」：可访问性快照 + 元素引用(e1/e2) + 结构化抽取

cd "$(dirname "$0")" || exit 1

export CAMOFOX_PORT="${CAMOFOX_PORT:-9377}"
export CAMOFOX_CRASH_REPORT_ENABLED=false
export CAMOFOX_LOCALE="${CAMOFOX_LOCALE:-zh-CN}"
export CAMOFOX_TIMEZONE="${CAMOFOX_TIMEZONE:-Asia/Shanghai}"

echo "════════════════════════════════════════════"
echo " camofox-browser → http://127.0.0.1:${CAMOFOX_PORT}"
echo " 遥测: 关   代理轮换: 未启用"
echo " 停: Ctrl+C"
echo "════════════════════════════════════════════"
echo

exec node node_modules/@askjo/camofox-browser/server.js
