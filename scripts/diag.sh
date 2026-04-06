#!/bin/bash
# Быстрая диагностика после деплоя
echo "=== systemctl status ai-che ==="
systemctl status ai-che --no-pager 2>/dev/null || echo "service not found"
echo ""
echo "=== journalctl -n 50 --no-pager ==="
journalctl -u ai-che --no-pager -n 50 2>/dev/null || journalctl -n 50 2>/dev/null
echo ""
echo "=== pm2 list ==="
pm2 list 2>/dev/null || echo "pm2 not found"
echo ""
echo "=== python3 -c 'import main' ==="
cd /root/AI-CHE && python3 -c "import main" 2>&1 | head -30
