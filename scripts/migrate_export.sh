#!/bin/bash
# Экспорт данных со старого сервера для миграции на новый.
# Запускать НА СТАРОМ сервере (root@194.104.9.219).
#
# Что упаковывается:
#   - chat.db (SQLite БД — все юзеры, КП, боты, балансы)
#   - uploads/ (PDF КП, презентации, картинки, RAG-файлы)
#   - .env (API-ключи, secrets — ОСТОРОЖНО, секретно!)
#   - .backup_encryption_key (для расшифровки старых бэкапов)
#   - .vapid_private.pem (Web Push)
#
# НЕ упаковывается:
#   - venv/ — установится заново на новом
#   - .git/ — лучше git clone заново
#   - logs/ — историю не переносим
#   - backups/*.enc — занимают место, можно пересоздать

set -euo pipefail

PROJ_DIR="${PROJ_DIR:-/root/AI-CHE}"
OUT_DIR="${OUT_DIR:-/tmp}"
TS=$(date +%Y%m%d-%H%M%S)
ARCHIVE="$OUT_DIR/aiche-migrate-$TS.tar.gz"

if [ ! -d "$PROJ_DIR" ]; then
    echo "❌ $PROJ_DIR не найден"
    exit 1
fi

cd "$PROJ_DIR"

echo "📦 Экспорт данных из $PROJ_DIR..."

# Stop service чтобы chat.db был в consistent-состоянии (WAL не фигачился)
if systemctl is-active --quiet ai-che 2>/dev/null; then
    echo "⏸  Останавливаю ai-che для консистентного бэкапа БД..."
    systemctl stop ai-che
    SERVICE_WAS_RUNNING=1
else
    SERVICE_WAS_RUNNING=0
fi

# Чек-листы перед бэкапом
echo "📊 Размеры:"
[ -f "chat.db" ] && echo "   chat.db: $(du -h chat.db | cut -f1)"
[ -d "uploads" ] && echo "   uploads/: $(du -sh uploads | cut -f1) ($(find uploads -type f | wc -l) файлов)"

# Список файлов для упаковки
FILES=()
[ -f "chat.db" ] && FILES+=("chat.db")
[ -d "uploads" ] && FILES+=("uploads")
[ -f ".env" ] && FILES+=(".env")
[ -f ".backup_encryption_key" ] && FILES+=(".backup_encryption_key")
[ -f ".vapid_private.pem" ] && FILES+=(".vapid_private.pem")

if [ ${#FILES[@]} -eq 0 ]; then
    echo "❌ Нет файлов для бэкапа в $PROJ_DIR"
    exit 1
fi

# Упаковываем
echo "🗜  Архивирую в $ARCHIVE..."
tar czf "$ARCHIVE" "${FILES[@]}" 2>&1 | tail -3

# Контрольная сумма
SHA=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
SIZE=$(du -h "$ARCHIVE" | cut -f1)

echo ""
echo "✅ Готово!"
echo "   Файл:  $ARCHIVE"
echo "   Размер: $SIZE"
echo "   SHA256: $SHA"
echo ""

# Возобновляем сервис если был запущен
if [ "$SERVICE_WAS_RUNNING" = "1" ]; then
    echo "▶️  Возобновляю ai-che..."
    systemctl start ai-che
fi

echo ""
echo "📤 Перенесите файл на новый сервер:"
echo "   scp $ARCHIVE root@<NEW_IP>:/root/"
echo ""
echo "Или через ssh с локальной машины:"
echo "   ssh root@194.104.9.219 'cat $ARCHIVE' > aiche-migrate.tar.gz"
echo "   scp aiche-migrate.tar.gz root@<NEW_IP>:/root/"
