"""Data retention cron (152-ФЗ ст. 5).

Вынесено из server/scheduler.py (split на доменные файлы).

Раз в сутки:
  1. Анонимизирует юзеров с `last_login_at > N мес` (default 24).
  2. Очищает `ProposalProject.generated_html` для КП старше N лет (default 3).

ENV:
  DATA_RETENTION_USER_INACTIVE_MONTHS (default 24, 0 = выкл)
  DATA_RETENTION_PROPOSAL_YEARS       (default 3,  0 = выкл)
  DATA_RETENTION_DRY_RUN              (default false — на проде пока выкл,
                                       включить когда наберётся база юзеров)
"""
import asyncio
import logging
import os

log = logging.getLogger("scheduler")


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
                        from server.security import mask_email as _mask
                        log.info(f"[data-retention] DRY: would anonymize user={u.id} email={_mask(u.email)}")
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
