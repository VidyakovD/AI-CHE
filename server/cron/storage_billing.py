"""Storage billing cron — раз в сутки списывает плату за хранилище юзеров.

Вынесено из server/scheduler.py.

Логика:
  - SUM bytes по active StoredAsset + KnowledgeFile per user
  - округление вверх до 100 МБ блоков (один общий лимит)
  - daily_rate = storage.per_100mb_month // 30
  - deduct_strict с записью Transaction
  - last_billed_at обновляется только у файлов из этого тика (race-safe)

Жизненный цикл просроченных оплат:
  >7 дней без оплаты   → StoredAsset.is_active=False, KnowledgeFile.enabled=False
  >37 дней без оплаты  → физическое удаление файла + строки в БД
"""
import asyncio
import logging
from datetime import datetime, timedelta

log = logging.getLogger("scheduler")


async def _storage_billing_tick():
    """Раз в сутки списывает плату за хранение файлов (см. docstring модуля)."""
    from server.db import db_session
    from server.models import StoredAsset, KnowledgeFile, Transaction
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
            _proj_root = _P(__file__).resolve().parent.parent.parent
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
