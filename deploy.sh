#!/bin/bash
# 一键部署：rsync 本地代码到服务器 → 安装依赖 → 重启 web + daemon → 健康检查
# 用法：./deploy.sh
# 只同步代码，不覆盖服务器上的 .env / accounts.enc / settings.yaml / data/ / logs/

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
  --exclude='config/settings.yaml' \
  --exclude='miniprogram/config.js' \
  --exclude='data/' \
  --exclude='logs/' \
  --exclude='.git/' \
  --exclude='deploy.sh' \
  "$LOCAL/" "$SERVER:$REMOTE/"

echo "▶ 安装/更新依赖 ..."
ssh "$SERVER" "cd $REMOTE && .venv/bin/pip install -q -r requirements.txt"

echo "▶ 导入冒烟测试（失败则不重启，老服务继续跑）..."
ssh "$SERVER" "cd $REMOTE && .venv/bin/python -c 'import sys; sys.path.insert(0, \".\"); import web.app; from src.main import run_all' " \
  || { echo "✗ 冒烟测试失败，已中止（服务器仍在运行旧版本）"; exit 1; }

echo "▶ 重启 zjip-daemon + zjip-web ..."
ssh "$SERVER" "systemctl restart zjip-daemon && systemctl restart zjip-web"

echo "▶ 健康检查 ..."
ssh "$SERVER" "
  sleep 3
  systemctl is-active --quiet zjip-web    || { echo '✗ zjip-web 未运行'; exit 1; }
  systemctl is-active --quiet zjip-daemon || { echo '✗ zjip-daemon 未运行'; exit 1; }
  code=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/login)
  [ \"\$code\" = \"200\" ] || { echo \"✗ Web 登录页返回 \$code\"; exit 1; }
  echo '  zjip-web: active, 登录页 200'
  echo '  zjip-daemon: active'
"

echo "✓ 部署完成"
