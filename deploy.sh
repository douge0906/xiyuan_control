#!/usr/bin/env bash
# 掌上锡院 · 控制台部署（Ubuntu / Debian）
#
# 用法：上传整个目录后在目录内执行：
#   chmod +x deploy.sh && bash deploy.sh
#
# 说明：
# - 配置与密钥在 data/.env，首次启动自动生成（ADMIN_KEY / TOKEN_SECRET）
# - 数据库 data/control.db（SQLite WAL）
# - 监听 127.0.0.1:8100，对外由 Nginx 终止 TLS
set -euo pipefail

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

CURRENT_USER="$(whoami)"
APP_DIR="/opt/xiyuan-control"
SERVICE="xiyuan-control"
PORT=8100

echo "==> [1/5] 系统依赖"
$SUDO apt-get update -y
$SUDO apt-get install -y python3 python3-venv python3-pip

echo "==> [2/5] 同步代码到 $APP_DIR"
$SUDO mkdir -p "$APP_DIR"
# 保留服务器上的 data/（数据库与 .env 是命根子）
if [ -d "$APP_DIR/data" ]; then
  $SUDO cp -r "$APP_DIR/data" /tmp/xiyuan-control-data
fi
$SUDO rsync -a --delete --exclude data --exclude venv --exclude __pycache__ ./ "$APP_DIR/"
if [ -d /tmp/xiyuan-control-data ]; then
  $SUDO rm -rf "$APP_DIR/data" && $SUDO mv /tmp/xiyuan-control-data "$APP_DIR/data"
fi
$SUDO chown -R "$CURRENT_USER:$CURRENT_USER" "$APP_DIR"

echo "==> [3/5] 虚拟环境与依赖"
cd "$APP_DIR"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -q -r requirements.txt
./venv/bin/python -c "import fastapi, uvicorn, cryptography; print('deps ok')"

echo "==> [4/5] 配置"
mkdir -p "$APP_DIR/data"
if [ ! -f "$APP_DIR/data/.env" ]; then
  # 首次启动 app 会自己生成 ADMIN_KEY / TOKEN_SECRET；这里先占位
  touch "$APP_DIR/data/.env"
  chmod 600 "$APP_DIR/data/.env"
  echo "    首次启动将自动生成管理密钥"
else
  echo "    沿用现有 data/.env"
fi

echo "==> [5/5] systemd"
$SUDO tee /etc/systemd/system/$SERVICE.service >/dev/null <<EOF
[Unit]
Description=XiYuan Control Plane
After=network.target

[Service]
User=$CURRENT_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT --workers 1
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now $SERVICE
sleep 2
$SUDO systemctl --no-pager --lines=0 status $SERVICE || true
echo
echo "管理密钥：$(grep '^ADMIN_KEY=' "$APP_DIR/data/.env" 2>/dev/null | cut -d= -f2 || echo '（启动后生成）')"
echo "健康检查：curl -s http://127.0.0.1:$PORT/healthz"
