#!/bin/bash
# token-plan-weekly.sh - 每周 cron 触发(fetch + report)
# 部署路径可通过环境变量配置(默认是 Linux 服务器的 /root 路径)
set -euo pipefail

LOG="${TOKEN_PLAN_LOG:-$HOME/Documents/MiniMax/token-plan/cron.log}"
REPORT_DIR="${TOKEN_PLAN_REPORT_DIR:-$HOME/Documents/MiniMax/token-plan/reports}"
PY="${TOKEN_PLAN_PY:-$HOME/venvs/minimax-token-plan/bin/python}"
FETCH="${TOKEN_PLAN_FETCH:-$HOME/bin/fetch_token_plan.py}"
REPORT="${TOKEN_PLAN_REPORT:-$HOME/bin/report_token_plan.py}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

echo "==== token-plan-weekly $(ts) ====" >> "$LOG"

# 1) fetch
if ! "$PY" "$FETCH" >> "$LOG" 2>&1; then
  echo "  [$(ts)] fetch FAILED" >> "$LOG"
  exit 1
fi
echo "  [$(ts)] fetch OK" >> "$LOG"

# 2) report
"$PY" "$REPORT" --stdout-only >> "$LOG" 2>&1
LATEST=$(ls -t "$REPORT_DIR"/report_*.md 2>/dev/null | head -1 || true)
echo "  [$(ts)] report: ${LATEST:-<none>}" >> "$LOG"
