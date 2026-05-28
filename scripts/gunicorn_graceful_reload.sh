#!/bin/bash
# True zero-downtime reload через gunicorn USR2 upgrade.
#
# HUP не подходит — gunicorn убивает всех старых workers одновременно,
# у нас старт нового worker занимает ~9 сек (тяжёлые импорты:
# 26 модулей AGENT_REGISTRY + ai.py + chatbot_engine). В окне 9 сек
# kernel accept queue переполняется → connection refused.
#
# USR2 правильнее: fork нового master с новыми workers, **оба слушают
# один сокет** через SO_REUSEPORT (gunicorn.conf.py: reuse_port=True).
# Когда новый master Ready — старому шлём WINCH (graceful shutdown
# workers) → QUIT (завершение master).
#
# Используется в /etc/systemd/system/ai-che.service:
#   ExecReload=/root/AI-CHE/scripts/gunicorn_graceful_reload.sh
set -eu

PID_FILE=/run/ai-che.pid
WAIT_NEW_MASTER=20    # сек на старт нового master + первого worker
WAIT_OLD_DRAIN=45     # сек дать старым workers доработать запросы

if [ ! -f "$PID_FILE" ]; then
    echo "[reload] PID file not found ($PID_FILE), falling back to HUP"
    pkill -HUP -f "gunicorn main:app" || true
    exit 0
fi

OLD_PID=$(cat "$PID_FILE")

if ! kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[reload] old master $OLD_PID dead, nothing to reload"
    exit 1
fi

echo "[reload] old master PID=$OLD_PID, sending USR2 (graceful upgrade)"
kill -USR2 "$OLD_PID"

# gunicorn USR2 поведение: текущий master переименовывает свой pidfile в
# pidfile.oldbin и fork'ит ВТОРОГО master с новой версией кода. Тот пишет
# свой PID в обычный pidfile. Старый master + workers продолжают слушать
# до явного WINCH/TERM.
echo "[reload] waiting ${WAIT_NEW_MASTER}s for new master to boot…"
SECONDS_WAITED=0
NEW_PID=""
while [ $SECONDS_WAITED -lt $WAIT_NEW_MASTER ]; do
    sleep 1
    SECONDS_WAITED=$((SECONDS_WAITED + 1))
    if [ -f "$PID_FILE" ]; then
        CUR_PID=$(cat "$PID_FILE")
        if [ "$CUR_PID" != "$OLD_PID" ] && kill -0 "$CUR_PID" 2>/dev/null; then
            NEW_PID="$CUR_PID"
            break
        fi
    fi
done

if [ -z "$NEW_PID" ]; then
    echo "[reload] ⚠ new master did not boot in ${WAIT_NEW_MASTER}s"
    echo "[reload] restoring old: kill -HUP $OLD_PID"
    kill -HUP "$OLD_PID" || true
    exit 1
fi

echo "[reload] ✓ new master PID=$NEW_PID booted"
echo "[reload] sending WINCH to old master (graceful workers shutdown)"
kill -WINCH "$OLD_PID" || true

# Дать старым workers доработать активные запросы (не accept'ить новые).
# Новый master с новыми workers уже слушает на том же сокете —
# новые connections идут к ним.
echo "[reload] waiting ${WAIT_OLD_DRAIN}s for old workers to drain…"
sleep "$WAIT_OLD_DRAIN"

echo "[reload] sending QUIT to old master $OLD_PID (final shutdown)"
kill -QUIT "$OLD_PID" 2>/dev/null || true

# Дать systemd новый main PID. Тут НЕЛЬЗЯ менять MainPID в PIDFile
# напрямую — gunicorn уже сделал это. systemd подхватит через
# PIDFile= после рестарта systemd-daemon, но для текущей сессии mainpid
# остаётся старым → systemctl stop потом убивает /run/ai-che.pid =
# уже новый PID. Это OK.
echo "[reload] ✓ done. new master PID=$NEW_PID is active"
exit 0
