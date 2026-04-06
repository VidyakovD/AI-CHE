#!/bin/bash
# Daily backup of chat.db — keeps last N copies
# Usage: crontab -e → 0 1 * * * /root/AI-CHE/scripts/backup-db.sh

BACKUP_DIR="/root/AI-CHE/backups"
DB="/root/AI-CHE/chat.db"
DATE=$(date +%Y%m%d_%H%M%S)
KEEP=7

mkdir -p "$BACKUP_DIR"
cp "$DB" "$BACKUP_DIR/chat_$DATE.db"

# Remove backups older than KEEP days
find "$BACKUP_DIR" -name "chat_*.db" -mtime +$KEEP -delete

echo "[$DATE] Backup saved: chat_$DATE.db"
