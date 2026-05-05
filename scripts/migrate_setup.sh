#!/bin/bash
# Первичная настройка нового сервера для AI Студии Че.
# Запускать НА НОВОМ сервере (root@193.187.92.147) после `apt update`.
#
# Что делает:
#   1. apt: python3.12, nginx, certbot, ufw, fail2ban, sqlite3
#   2. Установка python-зависимостей в venv
#   3. UFW: разрешить 22/80/443, остальное закрыть
#   4. fail2ban: защита SSH от брутфорса
#   5. Создание systemd unit ai-che.service
#   6. Создание nginx config (заглушка под aiche.ru)
#   7. Шрифты для PDF (DejaVu, Liberation, Noto)
#
# Прокси для AI и certbot SSL — отдельно (после основной установки).

set -euo pipefail

REPO="${REPO:-https://github.com/VidyakovD/AI-CHE.git}"
PROJ_DIR="${PROJ_DIR:-/root/AI-CHE}"
DOMAIN="${DOMAIN:-aiche.ru}"

echo "🚀 Установка AI Студии Че на $(hostname) ($(curl -s ifconfig.me 2>/dev/null || echo unknown))"
echo "   Репозиторий: $REPO"
echo "   Куда:        $PROJ_DIR"
echo "   Домен:       $DOMAIN"
echo ""

# ── 1. Системные пакеты ──
echo "📦 [1/7] Установка пакетов..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    nginx certbot python3-certbot-nginx \
    ufw fail2ban \
    sqlite3 \
    git curl wget \
    fonts-liberation fonts-dejavu fonts-noto-core \
    build-essential libpq-dev \
    >/dev/null

PY=$(command -v python3.12 || command -v python3)
echo "   Python: $($PY --version)"

# ── 2. Клонирование репо ──
echo "📥 [2/7] Клонирование репозитория..."
if [ ! -d "$PROJ_DIR/.git" ]; then
    git clone "$REPO" "$PROJ_DIR"
else
    cd "$PROJ_DIR" && git pull --rebase
fi

# ── 3. Виртуальное окружение + зависимости ──
echo "🐍 [3/7] Установка Python-зависимостей..."
cd "$PROJ_DIR"
if [ ! -d "venv" ]; then
    $PY -m venv venv
fi
./venv/bin/pip install --upgrade pip --quiet
./venv/bin/pip install -r requirements.txt --quiet
echo "   Установлено: $(./venv/bin/pip list 2>/dev/null | wc -l) пакетов"

# ── 4. UFW firewall ──
echo "🛡  [4/7] Настройка UFW..."
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow 80/tcp comment 'HTTP'
ufw allow 443/tcp comment 'HTTPS'
ufw --force enable
ufw status numbered | head -8

# ── 5. fail2ban ──
echo "🔒 [5/7] fail2ban на SSH..."
cat > /etc/fail2ban/jail.d/aiche-sshd.local <<'EOF'
[sshd]
enabled = true
port = ssh
maxretry = 5
findtime = 600
bantime = 3600
EOF
systemctl enable --now fail2ban >/dev/null
systemctl restart fail2ban

# ── 6. systemd unit ──
echo "⚙️  [6/7] systemd-юнит ai-che.service..."
if [ -f "$PROJ_DIR/deploy/ai-che.service" ]; then
    cp "$PROJ_DIR/deploy/ai-che.service" /etc/systemd/system/
else
    # Inline-fallback, если deploy/ нет
    cat > /etc/systemd/system/ai-che.service <<'UNIT'
[Unit]
Description=AI Studio Che — FastAPI app
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/AI-CHE
EnvironmentFile=/root/AI-CHE/.env
ExecStart=/root/AI-CHE/venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 4
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT
fi
systemctl daemon-reload
systemctl enable ai-che >/dev/null
echo "   ai-che.service установлен (НЕ запущен — сначала перенесите .env)"

# ── 7. nginx config ──
echo "🌐 [7/7] nginx-конфиг для $DOMAIN..."
if [ -f "$PROJ_DIR/deploy/nginx.conf" ]; then
    sed "s|__DOMAIN__|$DOMAIN|g" "$PROJ_DIR/deploy/nginx.conf" > "/etc/nginx/sites-available/$DOMAIN"
else
    # Inline-fallback
    cat > "/etc/nginx/sites-available/$DOMAIN" <<EOF
server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;

    client_max_body_size 50M;
    server_tokens off;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        # SSE для orchestra-stream
        proxy_buffering off;
        proxy_cache off;
    }
}
EOF
fi
ln -sf "/etc/nginx/sites-available/$DOMAIN" "/etc/nginx/sites-enabled/$DOMAIN"
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
echo "   nginx настроен на 80 → 127.0.0.1:8000"

echo ""
echo "✅ Базовая установка завершена!"
echo ""
echo "Следующие шаги:"
echo "  1. Перенесите данные:  ./scripts/migrate_import.sh /root/aiche-migrate.tar.gz"
echo "  2. Запустите сервис:   systemctl start ai-che"
echo "  3. Получите SSL:       certbot --nginx -d $DOMAIN -d www.$DOMAIN"
echo "  4. Поднимите AI-прокси (см. MIGRATION.md → раздел «AI-прокси»)"
echo "  5. DNS: переключите A-запись $DOMAIN на $(curl -s ifconfig.me 2>/dev/null || echo 'этот сервер')"
