#!/bin/bash
set -e

BACKUP_DIR="/root/AI-CHE/backups"
DB="/root/AI-CHE/chat.db"
DATE=$(date +%Y%m%d_%H%M%S)
KEEP=7
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
YAD_TOKEN=$(grep '^YAD_TOKEN=' "$SCRIPT_DIR/../.env" 2>/dev/null | cut -d'=' -f2 | tr -d ' "'"'"'"

mkdir -p "$BACKUP_DIR"
cp "$DB" "$BACKUP_DIR/chat_$DATE.db"

# Local cleanup
find "$BACKUP_DIR" -name "chat_*.db" -mtime +$KEEP -delete

# Upload to Yandex.Disk via OAuth
if [ -n "$YAD_TOKEN" ]; then
    UPLOAD_URL=$(curl -s -G \
        -H "Authorization: OAuth $YAD_TOKEN" \
        --data-urlencode "path=ai-che-backup/chat_$DATE.db" \
        https://cloud-api.yandex.net/v1/disk/resources/upload 2>/dev/null | \
        python3 -c "import sys,json; print(json.load(sys.stdin)['href'])" 2>/dev/null)

    if [ -n "$UPLOAD_URL" ]; then
        curl -s -T "$BACKUP_DIR/chat_$DATE.db" "$UPLOAD_URL" && \
            echo "[$DATE] Upload to Yandex.Disk: OK" || \
            echo "[$DATE] Upload to Yandex.Disk: FAILED"
    else
        echo "[$DATE] Failed to get upload URL from Yandex.Disk API"
    fi
else
    echo "[$DATE] YAD_TOKEN not set — keeping backup locally only"
fi

echo "[$DATE] Backup: chat_$DATE.db"
