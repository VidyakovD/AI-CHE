"""
Scheduler для воркфлоу с триггером `trigger_schedule`.
Запускается при старте main.py, проверяет каждую минуту — пришло ли время.
"""
import asyncio, logging, json, os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from server.db import SessionLocal
from server.models import ChatBot
from server.chatbot_engine import _execute_workflow

log = logging.getLogger("scheduler")

# Запоминаем последний запуск каждой (bot_id, node_id) пары
_last_fired: dict[tuple[int, str], datetime] = {}


def _should_fire(cfg: dict, now_local: datetime, last_fired: datetime | None) -> bool:
    """Нужно ли запускать расписание СЕЙЧАС (в локальном времени бота)."""
    mode = cfg.get("mode", "daily")

    if mode == "interval":
        minutes = int(cfg.get("interval_min", 15) or 15)
        if last_fired is None: return True
        return (now_local - last_fired).total_seconds() >= minutes * 60

    if mode == "hourly":
        # Срабатывает в начале каждого часа
        if last_fired is None: return now_local.minute == 0
        return now_local.hour != last_fired.hour and now_local.minute == 0

    if mode == "custom":
        # Простой парсер cron "M H D M W"
        cron = (cfg.get("cron") or "").strip()
        parts = cron.split()
        if len(parts) != 5: return False
        m, h, d, mo, w = parts
        def match(val: int, spec: str) -> bool:
            if spec == "*": return True
            if "," in spec: return any(match(val, s) for s in spec.split(","))
            if "/" in spec:
                base, step = spec.split("/", 1)
                if base == "*": return val % int(step) == 0
                return val >= int(base) and (val - int(base)) % int(step) == 0
            if "-" in spec:
                a, b = spec.split("-", 1)
                return int(a) <= val <= int(b)
            return val == int(spec)
        fired_this_minute = (last_fired and (now_local - last_fired).total_seconds() < 60)
        if fired_this_minute: return False
        return (match(now_local.minute, m) and match(now_local.hour, h)
                and match(now_local.day, d) and match(now_local.month, mo)
                and match(now_local.isoweekday() % 7, w))

    # daily / weekly — по HH:MM
    time_str = (cfg.get("time") or "09:00").strip()
    try:
        hh, mm = [int(x) for x in time_str.split(":", 1)]
    except Exception:
        return False

    if now_local.hour != hh or now_local.minute != mm:
        return False

    # Не стреляем дважды в ту же минуту
    if last_fired and (now_local - last_fired).total_seconds() < 60:
        return False

    if mode == "daily":
        return True

    if mode == "weekly":
        wd = now_local.isoweekday()  # 1-7 (Пн=1)
        allowed = {int(x.strip()) for x in (cfg.get("weekdays") or "1,2,3,4,5").split(",") if x.strip().isdigit()}
        return wd in allowed

    return False


async def _scheduler_tick():
    """Одна проверка — бежим по всем активным ботам и их расписаниям.

    Оптимизация: pre-фильтруем в SQL по подстроке "trigger_schedule" в
    workflow_json. На 1000 ботов отсекает 95%+ нерелевантных строк
    без полной загрузки JSON в Python.
    """
    db = SessionLocal()
    try:
        bots = (
            db.query(ChatBot)
            .filter_by(status="active")
            .filter(ChatBot.workflow_json.isnot(None))
            .filter(ChatBot.workflow_json.like('%trigger_schedule%'))
            .all()
        )
    finally:
        db.close()

    for bot in bots:
        if not bot.workflow_json:
            continue
        try:
            wf = json.loads(bot.workflow_json)
        except Exception:
            continue
        nodes = wf.get("wfc_nodes", [])
        schedule_nodes = [n for n in nodes if n.get("type") == "trigger_schedule"]
        if not schedule_nodes:
            continue

        for n in schedule_nodes:
            cfg = n.get("cfg", {})
            tz_name = cfg.get("tz") or "Asia/Yekaterinburg"
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = ZoneInfo("UTC")
            now_local = datetime.now(tz)
            key = (bot.id, n.get("id"))
            last = _last_fired.get(key)
            if last and last.tzinfo is None:
                last = last.replace(tzinfo=tz)
            if _should_fire(cfg, now_local, last):
                log.info(f"[Scheduler] firing bot={bot.id} node={n.get('id')} mode={cfg.get('mode')}")
                _last_fired[key] = now_local
                # Запустить граф с пустым входом
                try:
                    await _execute_workflow(
                        bot=bot,
                        chat_id=f"schedule_{n.get('id')}",
                        user_text="",
                        platform="schedule",
                        user_name="Scheduler",
                        workflow=wf,
                    )
                except Exception as e:
                    log.error(f"[Scheduler] bot={bot.id} node={n.get('id')} error: {e}")


async def scheduler_loop():
    """Главный цикл — проверка каждые 30 секунд.
    Advisory lock гарантирует что при нескольких workers tick выполнится один раз."""
    from server.worker_lock import worker_lock
    log.info("Scheduler started")
    while True:
        try:
            with worker_lock("scheduler_tick", ttl_sec=25) as acquired:
                if acquired:
                    await _scheduler_tick()
                # иначе другой worker уже выполняет — пропускаем
        except Exception as e:
            log.error(f"[Scheduler] tick error: {e}")
        await asyncio.sleep(30)


# ══ Auto-check API ключей раз в час + алерт админу при поломке ═══════════════
_last_apikey_check: datetime | None = None
_APIKEY_CHECK_INTERVAL = timedelta(hours=1)
_last_alerted_broken_ids: set[int] = set()


async def _apikey_check_tick():
    """Проверяет все API-ключи раз в час. Шлёт email админу если статус сломался."""
    global _last_apikey_check, _last_alerted_broken_ids
    now = datetime.utcnow()
    if _last_apikey_check and (now - _last_apikey_check) < _APIKEY_CHECK_INTERVAL:
        return
    _last_apikey_check = now

    from server.models import ApiKey
    from server.routes.admin import _test_key

    db = SessionLocal()
    try:
        keys = db.query(ApiKey).all()
    finally:
        db.close()

    # Проверяем в executor (OpenAI/Anthropic SDK синхронные)
    loop = asyncio.get_event_loop()
    broken = []
    for key in keys:
        try:
            status, error = await loop.run_in_executor(None, _test_key, key.provider, key.key_value)
        except Exception as e:
            status, error = "error", str(e)
        # Апдейт в БД в отдельной сессии
        db = SessionLocal()
        try:
            k = db.query(ApiKey).filter_by(id=key.id).first()
            if k:
                k.status = status
                k.last_error = error
                k.last_check = datetime.utcnow()
                db.commit()
        finally:
            db.close()
        if status == "error":
            broken.append((key, error))

    # Алерт админу если появились новые сломанные ключи
    new_broken = {k.id for k, _ in broken} - _last_alerted_broken_ids
    if new_broken:
        _alert_admin_broken_keys([b for b in broken if b[0].id in new_broken])
        _last_alerted_broken_ids = {k.id for k, _ in broken}
    else:
        # Если ключи починились — сбросим
        _last_alerted_broken_ids = {k.id for k, _ in broken}


def _alert_admin_broken_keys(broken: list):
    """Шлёт email админу со списком сломанных ключей."""
    import os
    admins = [e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]
    if not admins:
        return
    try:
        from server.email_service import _send, _base_template
        items = "".join(f"<li><b>{k.provider}</b>: {e or 'нет ответа'}</li>" for k, e in broken)
        body = f"""
        <p style="color:rgba(199,196,215,0.8);line-height:1.6">Автоматическая проверка обнаружила {len(broken)} сломанных API-ключа:</p>
        <ul style="color:rgba(199,196,215,0.8)">{items}</ul>
        <p style="color:rgba(199,196,215,0.7);font-size:13px">Юзеры получают «Сервис временно недоступен». Обновите ключ в /admin.html → API Ключи.</p>"""
        for admin_email in admins:
            _send(admin_email, "⚠️ API-ключи сломаны — AI Студия Че",
                  _base_template("Сломаны API-ключи", body))
    except Exception as e:
        log.error(f"[apikey alert] email failed: {e}")


async def apikey_check_loop():
    """Проверка API-ключей раз в час в фоне (с advisory lock для multi-worker).

    После рестарта сначала подгружаем уже-сломанные ключи из БД в
    `_last_alerted_broken_ids` — иначе следующая проверка считала бы их
    «новыми» и слала бы email повторно при каждом рестарте.
    """
    global _last_alerted_broken_ids
    from server.worker_lock import worker_lock
    from server.models import ApiKey
    log.info("API-key health-check started")

    # Hydrate state из БД: считаем что все уже-сломанные ключи мы уже
    # alerted'или ранее (либо при предыдущем запуске, либо админ их видел в UI).
    try:
        db = SessionLocal()
        try:
            already_broken = {k.id for k in db.query(ApiKey).filter_by(status="error").all()}
            _last_alerted_broken_ids = already_broken
            if already_broken:
                log.info(f"API-key health-check: {len(already_broken)} ключей "
                          "уже отмечены как сломанные в БД, повторно не алертим")
        finally:
            db.close()
    except Exception as e:
        log.warning(f"API-key health-check: hydrate state failed: {e}")

    # Первая проверка через 5 минут после старта (чтобы не тормозить холодный старт)
    await asyncio.sleep(300)
    while True:
        try:
            with worker_lock("apikey_check", ttl_sec=3500) as acquired:
                if acquired:
                    await _apikey_check_tick()
        except Exception as e:
            log.error(f"[apikey check] error: {e}")
        await asyncio.sleep(3600)


async def _cleanup_old_pdfs_tick():
    """Удаляет PDF-отчёты бизнес-решений старше 30 дней.
    Без этого /uploads/solutions/ растёт неограниченно — каждый run = новый PDF."""
    import os, time
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    folder = os.path.join(base, "uploads", "solutions")
    if not os.path.isdir(folder):
        return
    cutoff = time.time() - 30 * 86400
    removed = 0
    for name in os.listdir(folder):
        if not name.startswith("sol_") or not name.endswith(".pdf"):
            continue
        path = os.path.join(folder, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except Exception:
            pass
    if removed:
        log.info(f"[pdf-cleanup] removed {removed} PDFs older than 30 days")


async def pdf_cleanup_loop():
    """Раз в сутки чистит старые PDF (с lock — не дублируется на multi-worker)."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(600)  # подождать 10 мин после старта
    while True:
        try:
            with worker_lock("pdf_cleanup", ttl_sec=3600 * 23) as acquired:
                if acquired:
                    await _cleanup_old_pdfs_tick()
        except Exception as e:
            log.error(f"[pdf cleanup] error: {e}")
        await asyncio.sleep(86400)


# ── Auto-backup chat.db (раз в сутки, hot backup + retention 14 дней) ────────

def _get_backup_encryption_key() -> bytes | None:
    """Достаёт 256-битный ключ для AES-GCM шифрования бэкапов.

    Приоритет:
      1. ENV `BACKUP_ENCRYPTION_KEY` (hex или base64-encoded 32 байта)
      2. Файл `<project>/.backup_encryption_key` (генерится один раз)

    Если файла нет — генерим новый ключ и сохраняем с правами 0o400.
    Возвращает None если по какой-то причине ключ недоступен (тогда
    backup НЕ делается — иначе нарушение compliance).

    ВАЖНО: после потери ключа → backup'ы нечитаемы. Юзер должен
    скопировать содержимое ключа в безопасное место (1Password, Yandex Vault).
    """
    import os, base64, secrets as _secrets
    env_key = os.getenv("BACKUP_ENCRYPTION_KEY", "").strip()
    if env_key:
        try:
            # Поддержка hex (64 hex chars) и base64
            if len(env_key) == 64 and all(c in "0123456789abcdefABCDEF" for c in env_key):
                k = bytes.fromhex(env_key)
            else:
                k = base64.b64decode(env_key)
            if len(k) == 32:
                return k
        except Exception:
            pass
        log.error("[db-backup] BACKUP_ENCRYPTION_KEY задан, но имеет некорректный формат "
                   "(нужно 32 байта в hex или base64)")
        return None

    # Файловый ключ
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    key_path = os.path.join(base, ".backup_encryption_key")
    if os.path.exists(key_path):
        try:
            with open(key_path, "rb") as f:
                k = f.read().strip()
                if len(k) == 64:  # hex
                    return bytes.fromhex(k.decode("ascii"))
                if len(k) == 32:  # raw
                    return k
                # base64
                return base64.b64decode(k)
        except Exception as e:
            log.error(f"[db-backup] cannot read key file: {e}")
            return None

    # Генерим новый ключ. ОБЯЗАТЕЛЬНО предупреждаем юзера в логах.
    new_key = _secrets.token_bytes(32)
    try:
        with open(key_path, "wb") as f:
            f.write(new_key.hex().encode("ascii"))
        try:
            os.chmod(key_path, 0o400)
        except Exception:
            pass
        log.warning(
            "[db-backup] СГЕНЕРИРОВАН новый BACKUP_ENCRYPTION_KEY → %s. "
            "СКОПИРУЙ содержимое файла в безопасное место (1Password/Vault)! "
            "При потере ключа все будущие бэкапы будут нечитаемы.",
            key_path,
        )
        return new_key
    except Exception as e:
        log.error(f"[db-backup] cannot write key file: {e}")
        return None


def _encrypt_file(src_path: str, dst_path: str, key: bytes) -> None:
    """Зашифровать файл AES-256-GCM, записать в dst_path.

    Формат: 12-байтный nonce || ciphertext || 16-байтный auth tag.
    Auth tag добавляется автоматически AESGCM.encrypt(). Для чтения нужен
    тот же ключ + первые 12 байт как nonce.

    Решение читать файл целиком в RAM приемлемо для chat.db (≤ сотен МБ);
    при росте до ГБ — переключиться на streaming (chunked AES-CTR + HMAC).
    """
    import secrets as _secrets
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aesgcm = AESGCM(key)
    nonce = _secrets.token_bytes(12)
    with open(src_path, "rb") as f:
        plaintext = f.read()
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=b"aiche-db-backup-v1")
    with open(dst_path, "wb") as f:
        f.write(nonce + ciphertext)


def _upload_backup_to_yandex_disk(local_enc_path: str) -> str | None:
    """Заливает зашифрованный backup на Yandex.Disk через WebDAV.
    Возвращает строку результат или None если фича выключена.

    Включается через env:
      YANDEX_DISK_USER     — email от Yandex-аккаунта (например vidyakovd88@aiche.ru)
      YANDEX_DISK_PASSWORD — пароль приложения для типа «Файлы (WebDAV)»
      YANDEX_DISK_FOLDER   — папка на диске (default: aiche-backups)

    Файлы шифруются AES-256-GCM до записи на диск; на Yandex.Disk
    хранится зашифрованный blob, что делает диск безопасным офлайн-вместом
    хранения PDN-копий (152-ФЗ ст. 18 — копия в РФ-облаке).
    """
    import os
    user = os.getenv("YANDEX_DISK_USER", "").strip()
    pwd = os.getenv("YANDEX_DISK_PASSWORD", "").strip().replace(" ", "")
    folder = os.getenv("YANDEX_DISK_FOLDER", "aiche-backups").strip().strip("/")
    if not (user and pwd):
        return None  # фича выключена
    try:
        import httpx
    except ImportError:
        log.error("[yandex-disk] httpx не установлен")
        return "httpx missing"
    base = "https://webdav.yandex.ru/"
    fname = os.path.basename(local_enc_path)
    auth = httpx.BasicAuth(user, pwd)
    try:
        with httpx.Client(auth=auth, timeout=60.0) as c:
            # Создаём папку (если её ещё нет). MKCOL: 201=создан,
            # 405/409=уже существует — оба ок.
            r = c.request("MKCOL", base + folder + "/")
            if r.status_code not in (201, 405, 409, 200):
                log.warning(f"[yandex-disk] MKCOL {folder}: HTTP {r.status_code} {r.text[:200]}")
            # PUT файл (overwrite)
            with open(local_enc_path, "rb") as f:
                r = c.put(base + folder + "/" + fname, content=f.read())
            if 200 <= r.status_code < 300:
                size_mb = os.path.getsize(local_enc_path) / 1024 / 1024
                return f"yandex-disk:/{folder}/{fname} ({size_mb:.1f} MB)"
            return f"failed: HTTP {r.status_code} {r.text[:200]}"
    except Exception as e:
        log.error(f"[yandex-disk] upload failed: {type(e).__name__}: {str(e)[:300]}")
        return f"failed: {type(e).__name__}"


def _upload_backup_to_yc_s3(local_enc_path: str) -> str | None:
    """Загружает зашифрованный backup в Yandex Object Storage (S3-совместимое).
    Возвращает строку с результатом или None если фича выключена.

    Включается через env:
      YC_S3_ENDPOINT — обычно https://storage.yandexcloud.net (есть default)
      YC_S3_BUCKET — имя bucket'а (юзер создаёт в YC консоли)
      YC_S3_KEY_ID — статический ключ access_key_id
      YC_S3_SECRET — статический ключ secret_access_key

    Заливаем в `s3://<bucket>/db/<filename>`. Файл уже зашифрован AES-256-GCM,
    в облаке хранится в зашифрованном виде. Retention в облаке отдельный (можно
    политикой bucket'а сделать longer чем локальный 14 дней).
    """
    import os
    endpoint = os.getenv("YC_S3_ENDPOINT", "https://storage.yandexcloud.net").strip()
    bucket = os.getenv("YC_S3_BUCKET", "").strip()
    key_id = os.getenv("YC_S3_KEY_ID", "").strip()
    secret = os.getenv("YC_S3_SECRET", "").strip()
    if not (bucket and key_id and secret):
        return None  # фича выключена
    try:
        import boto3
        from botocore.config import Config as _BotoConfig
    except ImportError:
        log.error("[yc-s3] boto3 не установлен (pip install boto3)")
        return "boto3 missing"
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            region_name="ru-central1",  # Yandex Cloud только ru-central1
            config=_BotoConfig(connect_timeout=10, read_timeout=60, retries={"max_attempts": 2}),
        )
        s3_key = "db/" + os.path.basename(local_enc_path)
        s3.upload_file(local_enc_path, bucket, s3_key)
        size_mb = os.path.getsize(local_enc_path) / 1024 / 1024
        return f"s3://{bucket}/{s3_key} ({size_mb:.1f} MB)"
    except Exception as e:
        log.error(f"[yc-s3] upload failed: {type(e).__name__}: {str(e)[:300]}")
        return f"failed: {type(e).__name__}"


async def _db_backup_tick():
    """Делает hot-backup БД и шифрует AES-256-GCM (152-ФЗ для ПДн).
    Backend выбирается по DATABASE_URL:
      - SQLite → sqlite3.backup() (atomic, не блокирует writes)
      - PostgreSQL → pg_dump --format=custom (consistent snapshot, MVCC)

    Файлы: /backups/chat.db.YYYY-MM-DD.enc; retention 14 дней.
    Restore: scripts/restore_backup.py — расшифровка через тот же ключ.
    """
    import os, datetime, glob, subprocess
    from server.db import IS_SQLITE, IS_POSTGRES, DATABASE_URL
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    enc_key = _get_backup_encryption_key()
    if enc_key is None:
        log.error("[db-backup] нет encryption key — пропускаем backup, "
                   "иначе остался бы незашифрованный дамп на диске")
        return

    backup_dir = os.path.join(base, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    today = datetime.date.today().isoformat()
    dst_plain = os.path.join(backup_dir, f"chat.db.{today}.tmp")
    dst_enc = os.path.join(backup_dir, f"chat.db.{today}.enc")
    if os.path.exists(dst_enc):
        return  # уже сделали сегодня

    try:
        if IS_SQLITE:
            import sqlite3
            src = os.path.join(base, "chat.db")
            if not os.path.exists(src):
                return
            src_conn = sqlite3.connect(src)
            dst_conn = sqlite3.connect(dst_plain)
            try:
                with dst_conn:
                    src_conn.backup(dst_conn)
            finally:
                src_conn.close()
                dst_conn.close()
            check_conn = sqlite3.connect(dst_plain)
            try:
                cur = check_conn.execute("PRAGMA integrity_check")
                row = cur.fetchone()
                integrity_ok = bool(row and row[0] == "ok")
            finally:
                check_conn.close()
            if not integrity_ok:
                log.error(f"[db-backup] integrity_check FAILED for {dst_plain} — removing")
                try: os.remove(dst_plain)
                except Exception: pass
                return
        elif IS_POSTGRES:
            # pg_dump --format=custom: бинарный формат с compression, поддерживает
            # параллельный restore. Передаём DATABASE_URL прямо ему.
            # Перед запуском смотрим есть ли pg_dump в PATH.
            pg_dump = "pg_dump"
            try:
                res = subprocess.run(
                    [pg_dump, "--format=custom", "--no-owner", "--no-privileges",
                     "--file=" + dst_plain, DATABASE_URL],
                    check=True, capture_output=True, timeout=600,
                )
            except FileNotFoundError:
                log.error("[db-backup] pg_dump не установлен (apt install postgresql-client)")
                return
            except subprocess.CalledProcessError as e:
                log.error(f"[db-backup] pg_dump failed: {e.stderr.decode('utf-8', 'ignore')[:500]}")
                try: os.remove(dst_plain)
                except Exception: pass
                return
        else:
            log.error("[db-backup] неизвестный backend — пропускаем")
            return

        # Шифруем + удаляем plaintext
        _encrypt_file(dst_plain, dst_enc, enc_key)
        try:
            os.chmod(dst_enc, 0o400)
        except Exception:
            pass
        try:
            os.remove(dst_plain)
        except Exception as e:
            log.warning(f"[db-backup] cannot remove plaintext {dst_plain}: {e}")
        size_mb = os.path.getsize(dst_enc) / 1024 / 1024
        backend = "sqlite" if IS_SQLITE else "postgres"
        log.info(f"[db-backup] {dst_enc} ({size_mb:.1f} MB) backend={backend} encrypted=AES-256-GCM")

        # Отдельный шаг: офлайн-копия в Yandex Object Storage (если настроен).
        # Делается только после успешного шифрования. Если облако упало —
        # локальный backup всё равно есть, fail не блокирует tick.
        yc_result = _upload_backup_to_yc_s3(dst_enc)
        if yc_result is not None:
            if yc_result.startswith("s3://"):
                log.info(f"[db-backup] yandex-cloud: {yc_result}")
            else:
                log.warning(f"[db-backup] yandex-cloud: {yc_result}")
    except Exception as e:
        log.error(f"[db-backup] failed: {e}")
        for p in (dst_plain, dst_enc):
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass
        return

    # Retention: удаляем backup-ы старше 14 дней
    cutoff = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
    removed = 0
    for path in glob.glob(os.path.join(backup_dir, "chat.db.*")):
        try:
            name = os.path.basename(path)
            tag = name.replace("chat.db.", "").replace(".enc", "").replace(".tmp", "")
            if len(tag) == 10 and tag < cutoff:
                os.remove(path)
                removed += 1
        except Exception:
            pass
    if removed:
        log.info(f"[db-backup] retention: removed {removed} backups older than 14 days")


async def _cleanup_old_action_logs_tick():
    """Удаляет аудит-логи в три эшелона retention:
      - обычные info: 30 дней
      - auth.* / payment.* / record.* info: 365 дней (нужны для forensic
        и юридических вопросов: «когда я зарегистрировался?», «когда был платёж?»)
      - error/warn/critical: 90 дней (нужны для разбора инцидентов)
    """
    from datetime import datetime, timedelta
    from server.db import db_session
    from server.models import ActionLog
    now = datetime.utcnow()
    cutoff_info_short = now - timedelta(days=30)
    cutoff_info_long = now - timedelta(days=365)
    cutoff_err = now - timedelta(days=90)
    try:
        with db_session() as db:
            # info обычные (не auth/payment) — 30 дней
            n_info = (db.query(ActionLog)
                      .filter(ActionLog.ts < cutoff_info_short,
                              ActionLog.level == "info")
                      .filter(~ActionLog.action.like("auth.%"))
                      .filter(~ActionLog.action.like("payment.%"))
                      .filter(~ActionLog.action.like("record.%"))
                      .delete(synchronize_session=False))
            # auth/payment/record info — 1 год
            n_long = (db.query(ActionLog)
                      .filter(ActionLog.ts < cutoff_info_long,
                              ActionLog.level == "info")
                      .filter(
                          ActionLog.action.like("auth.%") |
                          ActionLog.action.like("payment.%") |
                          ActionLog.action.like("record.%"))
                      .delete(synchronize_session=False))
            # warn/error/critical — 90 дней
            n_err = (db.query(ActionLog)
                     .filter(ActionLog.ts < cutoff_err,
                             ActionLog.level != "info")
                     .delete(synchronize_session=False))
            db.commit()
            if n_info or n_long or n_err:
                log.info(f"[audit-cleanup] removed info={n_info} long={n_long} non-info={n_err}")
    except Exception as e:
        log.error(f"[audit-cleanup] failed: {e}")


async def audit_cleanup_loop():
    from server.worker_lock import worker_lock
    await asyncio.sleep(1200)  # 20 мин после старта
    while True:
        try:
            with worker_lock("audit_cleanup", ttl_sec=3600 * 23) as acquired:
                if acquired:
                    await _cleanup_old_action_logs_tick()
        except Exception as e:
            log.error(f"[audit-cleanup] tick: {e}")
        await asyncio.sleep(86400)


async def _cleanup_old_conversations_tick():
    """Удаляет тёрны диалогов старше 30 дней — иначе таблица растёт без границ.
    Каждый бот в день может писать сотни сообщений × 100k клиентов = миллионы строк."""
    from datetime import datetime, timedelta
    from server.db import db_session
    from server.models import BotConversationTurn
    cutoff = datetime.utcnow() - timedelta(days=30)
    try:
        with db_session() as db:
            n = (db.query(BotConversationTurn)
                 .filter(BotConversationTurn.created_at < cutoff)
                 .delete(synchronize_session=False))
            db.commit()
            if n:
                log.info(f"[conv-cleanup] removed {n} turns older than 30d")
    except Exception as e:
        log.error(f"[conv-cleanup] failed: {e}")


async def conv_cleanup_loop():
    """Раз в сутки чистит старые тёрны диалогов чат-ботов."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(900)  # 15 мин после старта
    while True:
        try:
            with worker_lock("conv_cleanup", ttl_sec=3600 * 23) as acquired:
                if acquired:
                    await _cleanup_old_conversations_tick()
        except Exception as e:
            log.error(f"[conv-cleanup] tick error: {e}")
        await asyncio.sleep(86400)


async def _storage_billing_tick():
    """
    Раз в сутки списывает плату за хранение файлов:
      - считаем total bytes у каждого юзера: active StoredAsset + KnowledgeFile
      - округляем вверх до 100 МБ блоков (один общий лимит на user)
      - умножаем на цену storage.per_100mb_month / 30 (дневная ставка)
      - списываем атомарно

    Логика просроченных оплат:
      - При успешном списании ставим last_billed_at=now на все asset'ы и
        KnowledgeFile юзера, попавшие в SUM этого тика.
      - Если баланса нет 7+ дней:
          StoredAsset → archive (is_active=False)
          KnowledgeFile → enabled=False (не участвует в RAG, но файл хранится)
      - Если archived/disabled 30+ дней (last_billed_at < now-37d):
          физическое удаление файла + строки в БД.

    Юзер видит "архивирован" в UI и может пополнить + восстановить (если ещё не удалили).
    """
    from datetime import datetime, timedelta
    from server.db import db_session
    from server.models import StoredAsset, KnowledgeFile, User, Transaction
    from server.billing import deduct_strict
    from server.pricing import get_price
    from sqlalchemy import func, update as sa_update

    rate_kop_month = get_price("storage.per_100mb_month", default=5000)
    daily_rate = max(1, rate_kop_month // 30)
    chunk = 100 * 1024 * 1024
    now = datetime.utcnow()
    # tick_start фиксируем ДО SELECT'а, чтобы при UPDATE last_billed_at не
    # затронуть файлы, загруженные параллельно с tick'ом (у них при upload
    # last_billed_at=now() >= tick_start). Иначе race: tick посчитал SUM без
    # свежего файла, но обновил его last_billed_at — на следующих сутках
    # файл попадает в SUM как «уже оплачен».
    tick_start = now
    try:
        with db_session() as db:
            # ── Сбор SUM bytes по юзерам из StoredAsset + KnowledgeFile ──
            asset_sum = dict(
                db.query(StoredAsset.user_id,
                          func.sum(StoredAsset.size_bytes))
                  .filter(StoredAsset.is_active == True)
                  .filter((StoredAsset.last_billed_at == None) |
                          (StoredAsset.last_billed_at < tick_start))
                  .group_by(StoredAsset.user_id).all()
            )
            kb_sum = dict(
                db.query(KnowledgeFile.user_id,
                          func.sum(KnowledgeFile.size))
                  .filter(KnowledgeFile.user_id != None)
                  .filter((KnowledgeFile.last_billed_at == None) |
                          (KnowledgeFile.last_billed_at < tick_start))
                  .group_by(KnowledgeFile.user_id).all()
            )
            user_ids = set(asset_sum) | set(kb_sum)
            charged = skipped = 0
            for user_id in user_ids:
                if not user_id:
                    continue
                total_bytes = int(asset_sum.get(user_id, 0) or 0) \
                              + int(kb_sum.get(user_id, 0) or 0)
                if total_bytes <= 0:
                    continue
                units = (total_bytes + chunk - 1) // chunk
                cost = units * daily_rate
                if deduct_strict(db, user_id, cost):
                    db.add(Transaction(
                        user_id=user_id, type="usage", tokens_delta=-cost,
                        description=f"Хранилище: {round(total_bytes/1024/1024, 1)} МБ ({cost/100:.2f} ₽/день)",
                    ))
                    # Помечаем «оплачено» ТОЛЬКО те asset'ы и KB-файлы, которые
                    # попали в SUM этого тика. Свежезагруженные не трогаем.
                    db.execute(
                        sa_update(StoredAsset)
                        .where(StoredAsset.user_id == user_id,
                               StoredAsset.is_active == True,
                               (StoredAsset.last_billed_at == None) |
                               (StoredAsset.last_billed_at < tick_start))
                        .values(last_billed_at=now)
                    )
                    db.execute(
                        sa_update(KnowledgeFile)
                        .where(KnowledgeFile.user_id == user_id,
                               (KnowledgeFile.last_billed_at == None) |
                               (KnowledgeFile.last_billed_at < tick_start))
                        .values(last_billed_at=now)
                    )
                    charged += 1
                else:
                    skipped += 1
            db.commit()
            if charged or skipped:
                log.info(f"[storage-billing] charged={charged} skipped(no balance)={skipped}")

            # ── Архивация просроченных StoredAsset (>7 дней без оплаты) ──
            cutoff_archive = now - timedelta(days=7)
            archived = (
                db.query(StoredAsset)
                .filter(StoredAsset.is_active == True)
                .filter(StoredAsset.last_billed_at != None)
                .filter(StoredAsset.last_billed_at < cutoff_archive)
                .filter(StoredAsset.created_at < cutoff_archive)
                .all()
            )
            archived_ids: list[int] = []
            for a in archived:
                a.is_active = False
                archived_ids.append(a.id)
            if archived_ids:
                db.commit()
                log.warning(f"[storage-billing] archived {len(archived_ids)} asset(s) — просрочка оплаты >7д")
                from server.audit_log import log_action
                by_user: dict[int, list[int]] = {}
                for a in archived:
                    by_user.setdefault(a.user_id, []).append(a.id)
                for uid, ids in by_user.items():
                    log_action("asset.archived", user_id=uid, target_type="asset",
                               level="warn", success=False,
                               details={"reason": "no_balance_7d", "asset_ids": ids[:50]})

            # ── Disable просроченных KnowledgeFile (>7 дней без оплаты) ──
            disabled_kb = (
                db.query(KnowledgeFile)
                .filter(KnowledgeFile.enabled == True)
                .filter(KnowledgeFile.last_billed_at != None)
                .filter(KnowledgeFile.last_billed_at < cutoff_archive)
                .filter(KnowledgeFile.created_at < cutoff_archive)
                .all()
            )
            disabled_ids: list[int] = []
            for kf in disabled_kb:
                kf.enabled = False
                disabled_ids.append(kf.id)
            if disabled_ids:
                db.commit()
                log.warning(f"[storage-billing] disabled {len(disabled_ids)} KB-файл(ов) — просрочка оплаты >7д")
                from server.audit_log import log_action
                by_user_kb: dict[int, list[int]] = {}
                for kf in disabled_kb:
                    if kf.user_id:
                        by_user_kb.setdefault(kf.user_id, []).append(kf.id)
                for uid, ids in by_user_kb.items():
                    log_action("knowledge.disabled", user_id=uid, target_type="kb",
                               level="warn", success=False,
                               details={"reason": "no_balance_7d", "file_ids": ids[:50]})

            # ── Физическое удаление просроченных StoredAsset (>37 дней) ──
            cutoff_delete = now - timedelta(days=37)
            from pathlib import Path as _P
            _proj_root = _P(__file__).resolve().parent.parent
            _uploads_root = (_proj_root / "uploads").resolve()

            stale = (
                db.query(StoredAsset)
                .filter(StoredAsset.is_active == False)
                .filter(StoredAsset.last_billed_at != None)
                .filter(StoredAsset.last_billed_at < cutoff_delete)
                .all()
            )
            deleted = 0
            for a in stale:
                try:
                    p = (_proj_root / a.path.lstrip("/")).resolve()
                    p.relative_to(_uploads_root)
                    if p.exists() and p.is_file():
                        p.unlink()
                except (ValueError, OSError) as ex:
                    log.warning(f"[storage-billing] skip delete {a.path}: {type(ex).__name__}")
                except Exception as ex:
                    log.warning(f"[storage-billing] cannot delete file {a.path}: {type(ex).__name__}")
                db.delete(a)
                deleted += 1
            if deleted:
                db.commit()
                log.warning(f"[storage-billing] hard-deleted {deleted} asset(s) — просрочка >37д")

            # ── Физическое удаление просроченных KnowledgeFile (>37 дней) ──
            stale_kb = (
                db.query(KnowledgeFile)
                .filter(KnowledgeFile.enabled == False)
                .filter(KnowledgeFile.last_billed_at != None)
                .filter(KnowledgeFile.last_billed_at < cutoff_delete)
                .all()
            )
            deleted_kb = 0
            for kf in stale_kb:
                try:
                    if kf.path:
                        p = (_proj_root / kf.path.lstrip("/")).resolve()
                        p.relative_to(_uploads_root)
                        if p.exists() and p.is_file():
                            p.unlink()
                except (ValueError, OSError) as ex:
                    log.warning(f"[storage-billing] skip delete KB {kf.path}: {type(ex).__name__}")
                except Exception as ex:
                    log.warning(f"[storage-billing] cannot delete KB file {kf.path}: {type(ex).__name__}")
                db.delete(kf)  # каскадно удалит KnowledgeChunk-и
                deleted_kb += 1
            if deleted_kb:
                db.commit()
                log.warning(f"[storage-billing] hard-deleted {deleted_kb} KB-файл(ов) — просрочка >37д")
    except Exception as e:
        log.error(f"[storage-billing] failed: {e}")


async def storage_billing_loop():
    """Раз в сутки списывает дневную плату за хранение файлов юзеров."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(1800)  # 30 мин после старта (после миграций)
    while True:
        try:
            with worker_lock("storage_billing", ttl_sec=3600 * 23) as acquired:
                if acquired:
                    await _storage_billing_tick()
        except Exception as e:
            log.error(f"[storage-billing] tick error: {e}")
        await asyncio.sleep(86400)


async def db_backup_loop():
    """Раз в 24ч hot-backup БД (с advisory lock — не дублируется)."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(120)  # подождать 2 мин после старта (миграции должны успеть)
    while True:
        try:
            with worker_lock("db_backup", ttl_sec=3600 * 23) as acquired:
                if acquired:
                    await _db_backup_tick()
        except Exception as e:
            log.error(f"[db-backup] tick error: {e}")
        await asyncio.sleep(86400)


# ── Orchestra schedules: автоматический запуск пользовательских расписаний ──

async def _orchestra_schedules_tick():
    """Каждую минуту проверяем какие OrchestraSchedule созрели для запуска
    (next_run_at <= now, is_active=True). Запускаем orchestra в фоне +
    обновляем last_run_at + next_run_at."""
    from server.db import db_session
    from server.models import OrchestraSchedule, SolutionRun, Solution, User
    from server.routes.schedules import _calc_next_run
    from server.solutions_orchestra import run_orchestra
    from server.billing import get_balance
    import json as _json
    import uuid as _uuid

    now = datetime.utcnow()
    fired: list[int] = []
    try:
        with db_session() as db:
            due = (db.query(OrchestraSchedule)
                     .filter(OrchestraSchedule.is_active == True,  # noqa: E712
                             OrchestraSchedule.next_run_at <= now,
                             OrchestraSchedule.next_run_at != None)  # noqa: E711
                     .limit(20).all())
            for s in due:
                # Базовая проверка баланса — минимум 200 коп = 2 ₽
                bal = get_balance(db, s.user_id)
                if bal < 200:
                    # Сдвигаем next_run на завтра — не отключаем (юзер может пополнить)
                    s.next_run_at = _calc_next_run(s.frequency, now)
                    log.warning(f"[schedule {s.id}] skip — недостаточно баланса")
                    continue
                # Проверяем что у юзера is_active и solution существует
                user = db.query(User).filter_by(id=s.user_id, is_active=True).first()
                solution = db.query(Solution).filter_by(
                    id=s.solution_id, is_active=True).first()
                if not user or not solution or not solution.orchestra_json:
                    s.is_active = False
                    log.warning(f"[schedule {s.id}] disabled — user/solution gone")
                    continue
                # Создаём SolutionRun (как обычный orchestra-старт)
                attachments = []
                if s.attachments_json:
                    try:
                        attachments = _json.loads(s.attachments_json)
                    except Exception:
                        attachments = []
                run = SolutionRun(
                    user_id=s.user_id, solution_id=s.solution_id,
                    chat_id=f"sched_{s.id}_" + _uuid.uuid4().hex[:8],
                    status="running",
                    user_input=s.user_input,
                    attachments_json=(_json.dumps(attachments, ensure_ascii=False)
                                       if attachments else None),
                    context=_json.dumps({"_scheduled_from": s.id},
                                         ensure_ascii=False),
                )
                db.add(run); db.flush()
                fired.append(run.id)
                # Обновляем расписание
                s.last_run_at = now
                s.last_run_id = run.id
                s.total_runs = (s.total_runs or 0) + 1
                s.next_run_at = _calc_next_run(s.frequency, now)
                # Audit
                try:
                    from server.audit_log import log_action
                    log_action("orchestra_schedule.fired", user_id=s.user_id,
                               target_type="schedule", target_id=str(s.id),
                               details={"run_id": run.id})
                except Exception:
                    pass
            db.commit()
    except Exception as e:
        log.error(f"[schedules] tick error: {type(e).__name__}: {e}")
        return

    # Запускаем orchestra в фоне (после commit)
    for run_id in fired:
        try:
            asyncio.create_task(run_orchestra(run_id))
        except Exception as e:
            log.error(f"[schedules] failed to launch run {run_id}: {e}")


async def orchestra_schedules_loop():
    """Раз в минуту проверяет созревшие расписания."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(60)  # ждём минуту после старта (миграции)
    while True:
        try:
            with worker_lock("orchestra_schedules", ttl_sec=55) as acquired:
                if acquired:
                    await _orchestra_schedules_tick()
        except Exception as e:
            log.error(f"[schedules] loop error: {e}")
        await asyncio.sleep(60)


async def _idempotency_cleanup_tick():
    """Удаляем idempotency-записи старше 5 минут (TTL).
    Без cleanup таблица будет расти вечно — при 60 RPS это +5M записей/день."""
    from server.db import db_session
    from server.models import IdempotencyRecord
    cutoff = datetime.utcnow() - timedelta(seconds=300)
    try:
        with db_session() as db:
            n = (db.query(IdempotencyRecord)
                   .filter(IdempotencyRecord.created_at < cutoff)
                   .delete(synchronize_session=False))
            db.commit()
            if n > 0:
                log.info(f"[idempotency] cleaned {n} stale records")
    except Exception as e:
        log.error(f"[idempotency] cleanup error: {type(e).__name__}: {e}")


async def idempotency_cleanup_loop():
    """Раз в минуту чистит expired idempotency-записи (5 мин TTL)."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(120)  # 2 мин после старта
    while True:
        try:
            with worker_lock("idempotency_cleanup", ttl_sec=55) as acquired:
                if acquired:
                    await _idempotency_cleanup_tick()
        except Exception as e:
            log.error(f"[idempotency] loop error: {e}")
        await asyncio.sleep(60)


# ══════════════════════════════════════════════════════════════════════════════
#  152-ФЗ Data Retention — анонимизация старых данных неактивных юзеров
# ══════════════════════════════════════════════════════════════════════════════
# Адаптация модуля datapolicy/datapolicycron.class.php от Dolibarr.
#
# Закон 152-ФЗ ст. 5 ч. 7: «Хранение персональных данных должно осуществляться
# в форме, позволяющей определить субъекта персональных данных, не дольше, чем
# этого требуют цели обработки». Если юзер N лет не заходит — мы не имеем
# права хранить его email/телефон.
#
# Решение: ночная задача проходит по old records и анонимизирует/удаляет:
#   - User с last_login_at > N месяцев назад (default 24) → email/phone → hash,
#     name → «Удалённый пользователь», marketing_consent → False
#   - ProposalProject старше N лет (default 3) → generated_html/generated_pdf
#     обнуляются (контент удалён, метаданные остаются для biller'а)
#   - BotConversationTurn — уже чистится conv_cleanup_loop (30 дней)
#
# Поведение настраивается через env:
#   DATA_RETENTION_USER_INACTIVE_MONTHS=24   (0 = отключить)
#   DATA_RETENTION_PROPOSAL_YEARS=3          (0 = отключить)
#   DATA_RETENTION_DRY_RUN=true              (логировать без UPDATE)
#
# Audit: каждое действие пишется в action_logs (data_retention.user_anonymized,
# data_retention.proposal_purged). Для compliance-аудита РКН.

async def _data_retention_tick():
    """Один тик — пройти по юзерам/КП и анонимизировать старые записи."""
    from datetime import datetime, timedelta
    from server.db import db_session
    from server.models import User, ProposalProject
    from server.audit_log import log_action
    import hashlib

    user_months = int(os.getenv("DATA_RETENTION_USER_INACTIVE_MONTHS", "24") or 0)
    prop_years = int(os.getenv("DATA_RETENTION_PROPOSAL_YEARS", "3") or 0)
    dry_run = os.getenv("DATA_RETENTION_DRY_RUN", "false").lower() in ("1", "true", "yes")

    if user_months <= 0 and prop_years <= 0:
        return  # обе политики отключены

    log.info(f"[data-retention] tick (user>{user_months}mo / prop>{prop_years}y / dry_run={dry_run})")

    # ── 1. User: анонимизация ──────────────────────────────────────────────
    if user_months > 0:
        cutoff_user = datetime.utcnow() - timedelta(days=user_months * 30)
        anon_count = 0
        try:
            with db_session() as db:
                # Не трогаем юзеров без last_login_at (= не успели залогиниться,
                # но создан недавно). И не трогаем уже анонимизированных
                # (по префиксу email).
                stale_users = (
                    db.query(User)
                    .filter(User.last_login_at.isnot(None))
                    .filter(User.last_login_at < cutoff_user)
                    .filter(~User.email.like("anon_%"))
                    .limit(100)  # batch-лимит на тик, чтоб не блокировать БД
                    .all()
                )
                for u in stale_users:
                    if dry_run:
                        log.info(f"[data-retention] DRY: would anonymize user={u.id} email={u.email}")
                        anon_count += 1
                        continue
                    # Хеш-токен от оригинального email для бухгалтерии (платежи
                    # должны оставаться trace'емыми по «какому-то ID», но без PII)
                    h = hashlib.sha256((u.email or "").encode("utf-8")).hexdigest()[:16]
                    anon_email = f"anon_{u.id}_{h}@deleted.local"
                    log_action(
                        "data_retention.user_anonymized",
                        user_id=u.id, target_type="user", target_id=str(u.id),
                        level="info", success=True,
                        details={"reason": f"inactive>{user_months}mo",
                                 "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None},
                    )
                    u.email = anon_email
                    u.name = "Удалённый пользователь"
                    u.tg_username = None
                    u.marketing_consent = False
                    anon_count += 1
                db.commit()
                if anon_count:
                    log.info(f"[data-retention] anonymized {anon_count} stale user(s) "
                             f"(inactive > {user_months}mo){' [DRY]' if dry_run else ''}")
        except Exception as e:
            log.error(f"[data-retention] user-anonymize failed: {type(e).__name__}: {e}")

    # ── 2. ProposalProject: purge старого контента ─────────────────────────
    if prop_years > 0:
        cutoff_prop = datetime.utcnow() - timedelta(days=prop_years * 365)
        purged = 0
        try:
            with db_session() as db:
                old_props = (
                    db.query(ProposalProject)
                    .filter(ProposalProject.created_at < cutoff_prop)
                    .filter(ProposalProject.generated_html.isnot(None))
                    .limit(100)
                    .all()
                )
                for p in old_props:
                    if dry_run:
                        log.info(f"[data-retention] DRY: would purge proposal={p.id}")
                        purged += 1
                        continue
                    p.generated_html = None
                    p.generated_pdf = None
                    p.client_email = None
                    p.client_request = None
                    log_action(
                        "data_retention.proposal_purged",
                        user_id=p.user_id, target_type="proposal_project", target_id=str(p.id),
                        level="info", success=True,
                        details={"reason": f"older>{prop_years}y",
                                 "created_at": p.created_at.isoformat() if p.created_at else None},
                    )
                    purged += 1
                db.commit()
                if purged:
                    log.info(f"[data-retention] purged content of {purged} old proposal(s) "
                             f"(older > {prop_years}y){' [DRY]' if dry_run else ''}")
        except Exception as e:
            log.error(f"[data-retention] proposal-purge failed: {type(e).__name__}: {e}")


async def data_retention_loop():
    """Раз в сутки прогоняет _data_retention_tick — анонимизирует старые
    данные неактивных юзеров (152-ФЗ ст. 5)."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(1800)  # 30 мин после старта — чтобы не нагружать первый run
    while True:
        try:
            with worker_lock("data_retention", ttl_sec=3600 * 23) as acquired:
                if acquired:
                    await _data_retention_tick()
        except Exception as e:
            log.error(f"[data-retention] loop tick error: {type(e).__name__}: {e}")
        await asyncio.sleep(86400)  # каждые 24 часа


def start_scheduler():
    """Фоновые задачи: scheduler / health / cleanup PDF / backup / conv / audit."""
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(apikey_check_loop())
    asyncio.create_task(pdf_cleanup_loop())
    asyncio.create_task(db_backup_loop())
    asyncio.create_task(conv_cleanup_loop())
    asyncio.create_task(audit_cleanup_loop())
    asyncio.create_task(storage_billing_loop())
    asyncio.create_task(orchestra_schedules_loop())
    asyncio.create_task(idempotency_cleanup_loop())
    asyncio.create_task(data_retention_loop())
