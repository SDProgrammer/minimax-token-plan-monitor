#!/bin/bash
# aliyun_setup.sh - 一键部署 MiniMax Token Plan Monitor 到 Linux 服务器
# 用法:
#   ssh root@<server-ip>
#   dnf install -y git
#   git clone <repo-url> /opt/minimax-token-plan-monitor
#   bash /opt/minimax-token-plan-monitor/deploy/aliyun_setup.sh
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/minimax-token-plan-monitor}"
BIN_DIR="$HOME/bin"
VENV_DIR="$HOME/venvs/minimax-token-plan"
DATA_DIR="$HOME/Documents/MiniMax/token-plan"
LOG_DIR="${DATA_DIR}/reports"
SECRET_DIR="$HOME/.config/minimax"
SECRET_FILE="${SECRET_DIR}/secret"

if [ ! -d "$REPO_DIR" ]; then
  echo "ERROR: 仓库目录不存在 $REPO_DIR(先 git clone <repo-url> $REPO_DIR)"
  exit 1
fi

# 以 root 跑时建议自行建专用用户;这里默认用当前登录用户的 HOME
HOME_BIN="$HOME/bin"
mkdir -p "$HOME_BIN"

echo "=== 1. 装 PostgreSQL + Python 3.11 + git ==="
dnf install -y postgresql-server postgresql-contrib python3.11 python3.11-pip git

echo ""
echo "=== 2. 初始化 PostgreSQL + 启动 ==="
if [ ! -f /var/lib/pgsql/data/PG_VERSION ]; then
  postgresql-setup --initdb
fi
systemctl enable postgresql
systemctl start postgresql
sleep 2
systemctl is-active postgresql

echo ""
echo "=== 3. 配置 pg_hba.conf(trust 模式,仅本机) ==="
cp /var/lib/pgsql/data/pg_hba.conf /var/lib/pgsql/data/pg_hba.conf.bak
cat > /var/lib/pgsql/data/pg_hba.conf <<'EOF'
local   all             all                                     trust
host    all             all             127.0.0.1/32            trust
host    all             all             ::1/128                 trust
local   replication     all                                     trust
host    replication     all             127.0.0.1/32            trust
host    replication     all             ::1/128                 trust
EOF
systemctl reload postgresql

echo ""
echo "=== 4. 建 db + schema ==="
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='minimax_token_plan'" \
  | grep -q 1 || sudo -u postgres psql -c "CREATE DATABASE minimax_token_plan"
psql -h 127.0.0.1 -U postgres -d minimax_token_plan -f "$REPO_DIR/schema.sql"

echo ""
echo "=== 5. 拷脚本到 $BIN_DIR ==="
mkdir -p "$BIN_DIR"
cp "$REPO_DIR"/fetch_token_plan.py      "$BIN_DIR/"
cp "$REPO_DIR"/fetch_token_plan_daemon.py "$BIN_DIR/"
cp "$REPO_DIR"/report_token_plan.py    "$BIN_DIR/"
cp "$REPO_DIR"/token-plan-weekly.sh    "$BIN_DIR/"
chmod +x "$BIN_DIR"/*.py "$BIN_DIR"/*.sh

echo ""
echo "=== 6. 建 venv + 装依赖 ==="
mkdir -p "$HOME/venvs"
if [ ! -d "$VENV_DIR" ]; then
  python3.11 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt" --quiet

echo ""
echo "=== 7. 创建数据目录 ==="
mkdir -p "$LOG_DIR" "$SECRET_DIR"
chmod 700 "$SECRET_DIR"

echo ""
echo "=== 8. 写入 MiniMax Key 到 secret 文件 ==="
echo "请输入你的 MiniMax Token Plan 订阅 Key"
read -r -s -p "Key: " KEY
echo
if [ -z "$KEY" ]; then
  echo "ERROR: Key 不能为空"
  exit 1
fi
printf '%s' "$KEY" > "$SECRET_FILE"
chmod 600 "$SECRET_FILE"
unset KEY
echo "  ✅ Key 已写入 $SECRET_FILE(chmod 600)"

echo ""
echo "=== 9. 注册 systemd service ==="
cp "$REPO_DIR/deploy/token-plan-daemon.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable token-plan-daemon
systemctl start token-plan-daemon
sleep 2
systemctl status token-plan-daemon --no-pager | head -10

echo ""
echo "=== 10. 加 crontab(每周一 10:00 跑 wrapper) ==="
LINE="0 10 * * 1 TZ=Asia/Shanghai $HOME/bin/token-plan-weekly.sh"
( crontab -l 2>/dev/null | grep -v 'token-plan-weekly' ) > /tmp/new_cron
echo "$LINE" >> /tmp/new_cron
crontab /tmp/new_cron
echo "--- crontab ---"
crontab -l

echo ""
echo "=== 11. 立刻跑一次 wrapper 验证 ==="
$BIN_DIR/token-plan-weekly.sh
echo ""
echo "--- cron.log ---"
tail -20 "$DATA_DIR/cron.log"

echo ""
echo "=========================================="
echo "✅ 部署完成"
echo "数据目录: $DATA_DIR"
echo "报告目录: $LOG_DIR"
echo "=========================================="
