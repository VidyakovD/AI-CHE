#!/bin/bash
# Blue/green zero-downtime deploy для AI Студии Че.
#
# Архитектура:
#   nginx → upstream ai_che_backend → [127.0.0.1:8000 primary, 127.0.0.1:8001 backup]
#   ai-che.service        = port 8000 (green)
#   ai-che-blue.service   = port 8001 (blue)
#   nginx proxy_next_upstream → если 8000 не отвечает, мгновенно failover на 8001
#
# Стратегия деплоя:
#   1. Pull код + установить deps
#   2. Restart BLUE (8001) с новым кодом → проверить smoke
#   3. Restart GREEN (8000) с новым кодом → nginx failover на BLUE пока green
#      рестартует → проверить smoke
#   4. Оба активны, версии синхронизированы
#
# Запуск:
#   /root/AI-CHE/scripts/deploy.sh
#
# Откат при ошибке: git revert + повторный запуск deploy.sh.

set -eu

REPO=/root/AI-CHE
GREEN_URL=http://127.0.0.1:8000/
BLUE_URL=http://127.0.0.1:8001/
# Публичный smoke через HTTPS на localhost — обходим публичный DNS/CDN,
# но nginx upstream-failover виден. -k т.к. сертификат на aiche.ru, не localhost.
PUBLIC_URL=https://aiche.ru/
PUBLIC_CURL_ARGS="--resolve aiche.ru:443:127.0.0.1 -k"
SMOKE_TIMEOUT=15

log() {
    echo "[deploy $(date +%T)] $*"
}

smoke_test() {
    local url=$1
    local label=$2
    local extra_args="${3:-}"
    local tries=20
    while [ $tries -gt 0 ]; do
        code=$(curl -sL -o /dev/null -w "%{http_code}" --max-time 5 $extra_args "$url" 2>/dev/null || echo 000)
        if [ "$code" = "200" ]; then
            log "✓ $label SMOKE OK (HTTP 200)"
            return 0
        fi
        sleep 1
        tries=$((tries - 1))
    done
    log "✗ $label SMOKE FAIL after ${SMOKE_TIMEOUT}s (last code: $code)"
    return 1
}

cd "$REPO"

log "── Шаг 1/5: подтягиваем код"
# Если есть локальные изменения tracked файлов — сбрасываем (это сервер,
# не разрабатываем тут). untracked файлы (.env.* бэкапы, .vapid) не трогаем.
if ! git diff --quiet HEAD 2>/dev/null; then
    log "  WARN: tracked файлы изменены — сбрасываю до HEAD"
    git checkout -- .
fi
git fetch origin main 2>&1 | tail -1
git reset --hard origin/main 2>&1 | tail -1

log "── Шаг 2/5: обновляем зависимости"
./venv/bin/pip install -r requirements.txt --upgrade --quiet

log "── Шаг 3/5: рестарт BLUE (port 8001)"
# Если blue unit ещё не установлен — установить
if [ ! -f /etc/systemd/system/ai-che-blue.service ]; then
    log "  установка ai-che-blue.service в systemd..."
    cp deploy/ai-che-blue.service /etc/systemd/system/ai-che-blue.service
    systemctl daemon-reload
    systemctl enable ai-che-blue
fi
# Обновим green unit тоже на случай изменений в deploy/ai-che.service
if ! cmp -s deploy/ai-che.service /etc/systemd/system/ai-che.service; then
    log "  обновление ai-che.service в systemd..."
    cp deploy/ai-che.service /etc/systemd/system/ai-che.service
    systemctl daemon-reload
fi

systemctl restart ai-che-blue
sleep 3
smoke_test "$BLUE_URL" "BLUE" || {
    log "BLUE упал, прерываю деплой. GREEN ещё на старом коде но активен."
    exit 1
}

# Прогрев blue 2 запросами через nginx — чтобы failover при падении green
# был мгновенным (nginx уже знает что blue здоров).
log "  прогрев blue через nginx upstream..."
for _ in 1 2 3; do
    curl -sL -o /dev/null --max-time 5 $PUBLIC_CURL_ARGS "$PUBLIC_URL" || true
    sleep 0.5
done

log "── Шаг 4/5: рестарт GREEN (port 8000) — nginx failover на BLUE"
systemctl restart ai-che
# Пока green в рестарте — публичные запросы должны идти через blue.
# Дадим nginx 2 сек чтобы failover отработал
sleep 2
smoke_test "$GREEN_URL" "GREEN" || {
    log "GREEN не поднялся! BLUE активен (через nginx failover). Разберись."
    exit 1
}

log "── Шаг 5/5: проверяем nginx-маршрут (публичный URL должен отвечать)"
smoke_test "$PUBLIC_URL" "PUBLIC (через nginx)" "$PUBLIC_CURL_ARGS" || {
    log "nginx что-то странное — проверь конфиг."
    exit 1
}

log ""
log "✓✓✓ DEPLOY OK ✓✓✓"
log "GREEN (8000) primary + BLUE (8001) backup, оба на новом коде."
log "git log -1: $(git log -1 --oneline)"
