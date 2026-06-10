#!/bin/bash
# 一键部署：rsync 本地代码到服务器，重启 Web 服务
# 用法：./deploy.sh
# 只同步代码，不覆盖服务器上的 .env / accounts.enc / data/ / logs/

set -e

SERVER="root@104.249.172.32"
REMOTE="/opt/zjip-monitor"
LOCAL="$(cd "$(dirname "$0")" && pwd)"

echo "▶ 同步代码到 $SERVER:$REMOTE ..."

rsync -az --progress \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='config/accounts.enc' \
  --exclude='config/accounts.yaml' \
  --exclude='data/' \
  --exclude='logs/' \
  --exclude='.git/' \
  --exclude='deploy.sh' \
  "$LOCAL/" "$SERVER:$REMOTE/"

echo "▶ 重启 zjip-web ..."
ssh "$SERVER" "systemctl restart zjip-web"

echo "✓ 部署完成"
