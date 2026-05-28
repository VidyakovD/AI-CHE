"""Gunicorn config для AI Студии Че — zero-downtime rolling reload.

Запуск:
  /root/AI-CHE/venv/bin/gunicorn main:app -c gunicorn.conf.py

Hot reload (без downtime):
  systemctl reload ai-che
  # Эквивалент: kill -HUP $(cat /run/ai-che.pid)
  #
  # Gunicorn master ловит SIGHUP → стартует новый набор workers с обновлённым
  # кодом → старые workers получают SIGTERM → дорабатывают активные запросы
  # за `graceful_timeout` секунд → корректно завершаются. Порт 8000 всё
  # время отвечает, nginx ничего не замечает, юзеры не получают 502.

Ограничения hot-reload:
  - Изменения в pyproject/requirements.txt — нужен полный restart
  - Изменения в gunicorn.conf.py — нужен полный restart
  - Новые env-переменные — нужен полный restart
  - Изменения в Python-коде / шаблонах — HOT-reload работает
"""
import multiprocessing
import os

# ── Socket ───────────────────────────────────────────────────────────────────
# bind через env-переменную: blue/green развёрнут на двух портах.
# - ai-che.service       → AI_CHE_PORT=8000 (зелёный, основной)
# - ai-che-blue.service  → AI_CHE_PORT=8001 (синий, backup)
# Nginx upstream даёт failover между ними при деплое.
bind = f"127.0.0.1:{os.getenv('AI_CHE_PORT', '8000')}"
# Backlog: сколько pending TCP-соединений ждёт accept. nginx буферит запросы
# при reload — берём с запасом чтобы pending не отбрасывались.
backlog = 2048
# SO_REUSEPORT: позволяет ДВУМ master-процессам слушать тот же порт. Это
# критично для USR2 zero-downtime upgrade: новый master стартует параллельно
# со старым на 127.0.0.1:8000, оба accept'ят connections, потом старый
# graceful shuts down.
reuse_port = True

# ── Workers ──────────────────────────────────────────────────────────────────
# 4 worker'а как было в uvicorn unit'е. На 2-core машине = 2*core+1 не нужен,
# т.к. большая часть запросов async-bound (LLM-вызовы, БД).
workers = int(os.getenv("GUNICORN_WORKERS", 4))
worker_class = "uvicorn.workers.UvicornWorker"

# Threads на worker (не используется с UvicornWorker — async обрабатывает всё)
# threads = 1

# Каждый worker перезапускается после N запросов (защита от memory leaks
# в long-running процессах). jitter рандомизирует чтобы worker'ы не
# перезапускались одновременно (иначе короткий dip в capacity).
max_requests = 10_000
max_requests_jitter = 1000

# Долгие LLM-вызовы (Claude/GPT/Veo) могут идти 60-120 секунд. timeout=120
# даёт буфер; если worker зависнет на дольше — gunicorn убьёт принудительно.
timeout = 180
# Graceful shutdown: 30 сек worker'у на завершение текущих запросов после
# получения SIGTERM. После этого SIGKILL. Достаточно для большинства
# LLM-вызовов (cache + retry без долгих переподключений).
graceful_timeout = 60
# keep-alive: переиспользовать TCP-соединение от nginx 5 секунд. Снижает
# overhead handshake'ов при burst-нагрузках.
keepalive = 5

# ── Preload ──────────────────────────────────────────────────────────────────
# preload_app=False (default) — каждый worker отдельно импортирует main.
# Зачем не True:
#   - Scheduler/cron loops стартуют в startup_event каждого worker'а.
#     С preload они бы стартанули один раз в master + дублировались бы в
#     каждом worker'е через fork. У нас есть worker_lock (SQLite advisory),
#     который защищает от дублирования — но это лишний шум.
#   - Hot-reload через HUP при preload=True требует gunicorn перезапуска
#     master (т.к. master уже импортировал старый код). Хотим простой HUP.
# preload_app = False

# ── PID и лог ────────────────────────────────────────────────────────────────
# pidfile тоже параметризован — у blue свой PID, иначе они перетирали бы друг друга.
pidfile = os.getenv("AI_CHE_PIDFILE", "/run/ai-che.pid")
# Логи идут в stderr → systemd journal. JSON-формат можно настроить отдельно
# если понадобится structured logging для observability.
accesslog = "-"   # stdout
errorlog = "-"    # stderr
loglevel = "info"
# Access log format: убрали user-agent (PII в логах), оставили базовые поля
access_log_format = (
    '%(h)s "%(r)s" %(s)s %(b)s %(D)sus "%(f)s"'
)

# ── Прокси-заголовки ─────────────────────────────────────────────────────────
# nginx отдаёт реальный IP клиента через X-Forwarded-For. По умолчанию
# гуниkорн доверяет proxy-headers только от 127.0.0.1 — это и наш nginx.
proxy_protocol = False
proxy_allow_ips = "127.0.0.1"
forwarded_allow_ips = "127.0.0.1"

# ── Worker memory limits ─────────────────────────────────────────────────────
# Не выставляем worker_tmp_dir в /dev/shm — у нас обычный диск SSD достаточно
# быстрый, /dev/shm часто маленький (~64МБ) и переполняется при больших
# multipart-загрузках.

# ── Hooks для observability ──────────────────────────────────────────────────
def on_starting(server):
    """master startup — один раз перед fork worker'ов."""
    server.log.info("[gunicorn] master starting, workers=%d", workers)


def on_reload(server):
    """Triggered by SIGHUP — перед fork'ом новых worker'ов."""
    server.log.info("[gunicorn] HUP received, rolling reload of workers")


def worker_int(worker):
    """SIGINT/SIGQUIT в worker'е — обычно при graceful shutdown."""
    worker.log.info("[gunicorn] worker %d shutting down gracefully", worker.pid)


def worker_abort(worker):
    """Worker превысил timeout — gunicorn принудительно убивает."""
    worker.log.warning("[gunicorn] worker %d aborted (timeout)", worker.pid)


def post_fork(server, worker):
    """После fork — каждый worker может инициализировать своё (e.g. БД пул)."""
    server.log.debug("[gunicorn] worker %d forked", worker.pid)
