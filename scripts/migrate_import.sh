#!/bin/bash
# Распаковка миграционного архива на новом сервере.
# Запускать НА НОВОМ сервере после migrate_setup.sh.
#
# Usage:
#   ./scripts/migrate_import.sh /root/aiche-migrate-20260505-091200.tar.gz

set -euo pipefail

ARCHIVE="${1:-}"
PROJ_DIR="${PROJ_DIR:-/root/AI-CHE}"

if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
    echo "Usage: $0 <path-to-archive.tar.gz>"
    echo "Пример: $0 /root/aiche-migrate-20260505-091200.tar.gz"
    exit 1
fi

if [ ! -d "$PROJ_DIR" ]; then
    echo "❌ $PROJ_DIR не найден. Сначала запустите migrate_setup.sh"
    exit 1
fi

cd "$PROJ_DIR"

# Останавливаем сервис если запущен
if systemctl is-active --quiet ai-che 2>/dev/null; then
    echo "⏸  Останавливаю ai-che перед импортом..."
    systemctl stop ai-che
fi

# Бэкап текущих файлов на случай отката
TS=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/tmp/aiche-rollback-$TS"
mkdir -p "$BACKUP_DIR"

for f in chat.db .env .backup_encryption_key .vapid_private.pem; do
    [ -e "$PROJ_DIR/$f" ] && cp -r "$PROJ_DIR/$f" "$BACKUP_DIR/" && echo "   Бэкап: $f → $BACKUP_DIR/"
done
[ -d "$PROJ_DIR/uploads" ] && [ -n "$(ls -A $PROJ_DIR/uploads 2>/dev/null)" ] && cp -r "$PROJ_DIR/uploads" "$BACKUP_DIR/" && echo "   Бэкап: uploads/ → $BACKUP_DIR/"

# Распаковка
echo ""
echo "📦 Распаковываю $ARCHIVE..."
tar xzf "$ARCHIVE" -C "$PROJ_DIR" 2>&1 | tail -5

# Проверка целостности
echo ""
echo "✓ Проверка результата:"
[ -f "$PROJ_DIR/chat.db" ] && echo "   chat.db: $(du -h $PROJ_DIR/chat.db | cut -f1) ✅" || echo "   chat.db: ❌"
[ -d "$PROJ_DIR/uploads" ] && echo "   uploads: $(du -sh $PROJ_DIR/uploads | cut -f1) ($(find $PROJ_DIR/uploads -type f | wc -l) файлов) ✅" || echo "   uploads: ❌"
[ -f "$PROJ_DIR/.env" ] && echo "   .env: ✅" || echo "   .env: ❌"
[ -f "$PROJ_DIR/.backup_encryption_key" ] && echo "   .backup_encryption_key: ✅"
[ -f "$PROJ_DIR/.vapid_private.pem" ] && echo "   .vapid_private.pem: ✅"

# SQLite integrity check
if [ -f "$PROJ_DIR/chat.db" ]; then
    INTEGRITY=$(sqlite3 "$PROJ_DIR/chat.db" "PRAGMA integrity_check;" 2>&1 | head -1)
    if [ "$INTEGRITY" = "ok" ]; then
        echo "   SQLite integrity: ok ✅"
    else
        echo "   ⚠️  SQLite integrity: $INTEGRITY"
    fi
    USERS=$(sqlite3 "$PROJ_DIR/chat.db" "SELECT COUNT(*) FROM users" 2>/dev/null)
    BOTS=$(sqlite3 "$PROJ_DIR/chat.db" "SELECT COUNT(*) FROM chatbots" 2>/dev/null)
    echo "   Юзеров: $USERS | Ботов: $BOTS"
fi

# Применяем миграции
echo ""
echo "🔄 Применяю lightweight-миграции..."
cd "$PROJ_DIR"
DEV_MODE=false APP_ENV=production ./venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from server.db import Base, engine, apply_lightweight_migrations
from server import models
Base.metadata.create_all(bind=engine)
apply_lightweight_migrations()
print('OK')
" 2>&1 | tail -5

# Права на файлы
chmod 600 "$PROJ_DIR/.env" 2>/dev/null || true
chmod 400 "$PROJ_DIR/.backup_encryption_key" 2>/dev/null || true
chmod 400 "$PROJ_DIR/.vapid_private.pem" 2>/dev/null || true

echo ""
echo "✅ Импорт завершён!"
echo ""
echo "Откат (если что-то сломалось):"
echo "   systemctl stop ai-che"
echo "   cp $BACKUP_DIR/* $PROJ_DIR/"
echo "   systemctl start ai-che"
echo ""
echo "Запустить сервис:"
echo "   systemctl start ai-che"
echo "   systemctl status ai-che"
echo "   journalctl -u ai-che -f   # live-логи"
echo ""
echo "Проверить:"
echo "   curl http://127.0.0.1:8000/healthz   # должно быть {\"status\":\"ok\"}"
