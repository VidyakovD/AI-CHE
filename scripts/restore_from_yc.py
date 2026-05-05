"""
Восстановление БД из Yandex Object Storage.

Use case: "сервер сгорел / случайно удалили / нужно проверить старый дамп".
Скачивает зашифрованный pg_dump из YC S3, расшифровывает AES-GCM, кладёт
в /tmp/aiche-restore-<date>.dump. Дальше юзер сам решает: посмотреть,
залить в новый postgres через `pg_restore`, etc.

Использование:
  YC_S3_ENDPOINT=https://storage.yandexcloud.net \
  YC_S3_BUCKET=aiche-backups \
  YC_S3_KEY_ID=YCAJ... \
  YC_S3_SECRET=YCN... \
  python scripts/restore_from_yc.py             # последний backup
  python scripts/restore_from_yc.py 2026-05-05  # конкретная дата

Требует: ключ AES в .backup_encryption_key (тот же что использовался при
создании бэкапа), boto3 в venv.

Восстановление в новый postgres:
  pg_restore -h localhost -U aiche -d aiche --clean --if-exists /tmp/aiche-restore-*.dump
"""
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("restore")

DATE = sys.argv[1] if len(sys.argv) > 1 else None  # YYYY-MM-DD или None=latest

ENDPOINT = os.getenv("YC_S3_ENDPOINT", "https://storage.yandexcloud.net").strip()
BUCKET = os.getenv("YC_S3_BUCKET", "").strip()
KEY_ID = os.getenv("YC_S3_KEY_ID", "").strip()
SECRET = os.getenv("YC_S3_SECRET", "").strip()
if not (BUCKET and KEY_ID and SECRET):
    log.error("YC_S3_BUCKET / YC_S3_KEY_ID / YC_S3_SECRET — задай в env")
    sys.exit(1)

# Корень проекта в sys.path для импорта server.scheduler._get_backup_encryption_key
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    import boto3
    from botocore.config import Config

    s3 = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=KEY_ID,
        aws_secret_access_key=SECRET,
        region_name="ru-central1",
        config=Config(connect_timeout=10, read_timeout=120),
    )

    # 1. Ищем нужный backup в bucket'е
    log.info(f"Список backup'ов в s3://{BUCKET}/db/ …")
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix="db/")
    contents = resp.get("Contents", [])
    if not contents:
        log.error("В bucket'е нет файлов в /db/. Возможно бэкапы не загрузились.")
        sys.exit(1)
    contents.sort(key=lambda c: c["LastModified"], reverse=True)
    if DATE:
        wanted = [c for c in contents if DATE in c["Key"]]
        if not wanted:
            log.error(f"Backup за {DATE} не найден. Доступные:")
            for c in contents[:10]:
                log.error(f"  {c['Key']} ({c['Size']/1024/1024:.1f} MB) {c['LastModified']}")
            sys.exit(1)
        target = wanted[0]
    else:
        target = contents[0]
    log.info(f"Выбран: {target['Key']} ({target['Size']/1024/1024:.1f} MB) от {target['LastModified']}")

    # 2. Скачиваем
    enc_path = "/tmp/" + os.path.basename(target["Key"])
    log.info(f"Скачиваем в {enc_path} …")
    s3.download_file(BUCKET, target["Key"], enc_path)

    # 3. Расшифровываем
    from server.scheduler import _get_backup_encryption_key
    key = _get_backup_encryption_key()
    if key is None:
        log.error("Не найден backup encryption key (.backup_encryption_key или env)")
        sys.exit(1)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aesgcm = AESGCM(key)
    with open(enc_path, "rb") as f:
        blob = f.read()
    nonce, ciphertext = blob[:12], blob[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=b"aiche-db-backup-v1")
    out_path = enc_path.replace(".enc", ".dump")
    with open(out_path, "wb") as f:
        f.write(plaintext)
    log.info(f"Расшифрованный дамп: {out_path} ({len(plaintext)/1024/1024:.1f} MB)")
    log.info("")
    log.info("Дальше:")
    log.info(f"  pg_restore -h localhost -U aiche -d aiche --clean --if-exists {out_path}")
    log.info("  (или: создать новую БД, затем pg_restore без --clean)")

    # cleanup encrypted file
    try:
        os.remove(enc_path)
    except Exception:
        pass


if __name__ == "__main__":
    main()
