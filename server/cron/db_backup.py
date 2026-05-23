"""DB backup cron — ежедневный hot-backup с AES-256-GCM шифрованием.

Вынесено из server/scheduler.py.

Backend выбирается по DATABASE_URL:
  - SQLite     → sqlite3.backup() atomic, не блокирует writes
  - PostgreSQL → pg_dump --format=custom (MVCC consistent snapshot)

Файлы: /backups/chat.db.YYYY-MM-DD.enc; retention 14 дней (старые удаляются).
Restore: scripts/restore_backup.py — расшифровка через тот же ключ.

Облачная копия (опционально): Yandex Disk WebDAV или Yandex Object Storage
S3 — настраивается через env (см. helper'ы _upload_*).
"""
import asyncio
import logging
import os

log = logging.getLogger("scheduler")


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
    import base64, secrets as _secrets
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

    # Файловый ключ (корень проекта — два уровня вверх от server/cron/db_backup.py)
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    import datetime, glob, subprocess
    from server.db import IS_SQLITE, IS_POSTGRES, DATABASE_URL
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
            pg_dump = "pg_dump"
            try:
                subprocess.run(
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


async def db_backup_loop():
    """Раз в 24ч hot-backup БД (с advisory lock — не дублируется).

    Лок: TTL = 86700 (24ч + 5 мин буфера). Раньше было 23ч и race-окно
    3-мин при крэше — второй воркер мог стартовать второй бэкап до того
    как истинный TTL закроется. С TTL > sleep лок гарантированно жив до
    следующего тика этого же воркера.
    """
    from server.worker_lock import worker_lock
    await asyncio.sleep(120)  # подождать 2 мин после старта (миграции должны успеть)
    while True:
        try:
            with worker_lock("db_backup", ttl_sec=86400 + 300) as acquired:
                if acquired:
                    await _db_backup_tick()
        except Exception as e:
            log.error(f"[db-backup] tick error: {e}")
        await asyncio.sleep(86400)
