"""Cron-задача: auto-followup на отправленные КП без открытия.

Логика: КП отправлено клиенту, но клиент не открыл его за 3+ дня → шлём
автоматическое вежливое напоминание на тот же email-thread (через
outbox_message_id для In-Reply-To, чтобы письмо легло в ту же цепочку).

Условия выборки:
  - status = 'done' (КП готово, не draft/error/refunded)
  - sent_at IS NOT NULL и < now - FOLLOWUP_DELAY_DAYS дней
  - opened_at IS NULL (клиент не открыл публичную ссылку)
  - followup_sent_at IS NULL (мы ещё не напоминали)
  - auto_followup_enabled = TRUE (юзер не выключил)
  - client_email указан (есть куда писать)
  - crm_stage NOT IN ('won', 'lost', 'replied')  — закрытые/отвечённые не трогаем

Цикл — раз в 1 час. Worker-lock против дублей на multi-worker.

Лимит 30 КП за тик чтобы не упереться в SMTP-лимит провайдера за раз.
"""
import asyncio
import logging
from datetime import datetime, timedelta

log = logging.getLogger("scheduler")


FOLLOWUP_DELAY_DAYS = 3   # сколько ждать до напоминания
FOLLOWUP_BATCH_SIZE = 30  # макс КП за один тик
FOLLOWUP_TICK_INTERVAL = 3600  # раз в час


async def _proposals_followup_tick():
    """Один проход cron: найти неоткрытые КП → отправить followup."""
    from server.db import db_session
    from server.models import ProposalProject
    from sqlalchemy import and_

    now = datetime.utcnow()
    cutoff = now - timedelta(days=FOLLOWUP_DELAY_DAYS)
    sent = 0
    errors = 0

    try:
        with db_session() as db:
            q = (db.query(ProposalProject)
                   .filter(
                       ProposalProject.status == "done",
                       ProposalProject.sent_at.isnot(None),
                       ProposalProject.sent_at < cutoff,
                       ProposalProject.opened_at.is_(None),
                       ProposalProject.followup_sent_at.is_(None),
                       ProposalProject.auto_followup_enabled.is_(True),
                       ProposalProject.client_email.isnot(None),
                       ~ProposalProject.crm_stage.in_(("won", "lost", "replied")),
                   )
                   .order_by(ProposalProject.sent_at.asc())
                   .limit(FOLLOWUP_BATCH_SIZE))
            candidates = q.all()

            for p in candidates:
                try:
                    ok = _send_followup_email(p)
                    if ok:
                        p.followup_sent_at = now
                        sent += 1
                    else:
                        errors += 1
                except Exception as e:
                    log.exception(f"[proposals.followup] project={p.id} failed: {e}")
                    errors += 1

            if sent > 0 or errors > 0:
                db.commit()
                log.info(f"[proposals.followup] tick: sent={sent} errors={errors}")
    except Exception as e:
        log.error(f"[proposals.followup] tick fatal: {type(e).__name__}: {e}")


def _send_followup_email(p) -> bool:
    """Сформировать и отправить followup-email клиенту.

    Возвращает True если send успешен. False — если SMTP упал или
    email-инфра не настроена.

    Email-thread: In-Reply-To = outbox_message_id (наше первое письмо с КП),
    чтобы клиенту письмо легло в ту же цепочку — выглядит как естественное
    напоминание, а не «новый КП».
    """
    if not p.client_email:
        return False

    public_url = None
    if p.public_token:
        import os as _os
        base = _os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
        public_url = f"{base}/p/{p.public_token}"

    client_name = (p.client_name or "").strip() or "коллега"
    sent_date_str = p.sent_at.strftime("%d.%m") if p.sent_at else "неделю назад"
    project_name = (p.name or "коммерческое предложение").strip()

    # Текст письма — короткий, вежливый, без давления. Цель — мягко
    # напомнить, что КП ждёт открытия. Без агрессии и "AI-копирайт" штампов.
    text_body = (
        f"Здравствуйте, {client_name}!\n\n"
        f"{sent_date_str} мы отправляли вам коммерческое предложение "
        f"«{project_name}». Хотелось узнать — успели ли с ним ознакомиться?\n\n"
    )
    if public_url:
        text_body += f"Открыть КП можно по ссылке: {public_url}\n\n"
    text_body += (
        "Если возникли вопросы или нужно что-то уточнить — просто ответьте на "
        "это письмо. Будем рады обсудить.\n\n"
        "С уважением,\nкоманда"
    )

    # HTML версия — те же тексты с минимальным форматированием
    safe_name = _html_escape(client_name)
    safe_project = _html_escape(project_name)
    html_body = (
        f'<p>Здравствуйте, <b>{safe_name}</b>!</p>'
        f'<p>{sent_date_str} мы отправляли вам коммерческое предложение '
        f'<b>«{safe_project}»</b>. Хотелось узнать — успели ли с ним ознакомиться?</p>'
    )
    if public_url:
        html_body += f'<p><a href="{_html_escape(public_url)}">Открыть КП</a></p>'
    html_body += (
        '<p>Если возникли вопросы или нужно что-то уточнить — просто ответьте '
        'на это письмо. Будем рады обсудить.</p>'
        '<p style="color:#888;font-size:13px">С уважением,<br>команда</p>'
    )

    subject = f"Re: {project_name}"
    if p.outbox_message_id:
        subject = f"Напоминаем: {project_name}"

    try:
        from server.email_service import _send
        # _send(to, subject, html_body, text_body=None, in_reply_to=None)
        _send(p.client_email, subject, html_body,
              text_body=text_body,
              in_reply_to=p.outbox_message_id)
        try:
            from server.audit_log import log_action
            log_action("proposal.followup_sent",
                       user_id=p.user_id, target_type="proposal_project",
                       target_id=p.id, level="info",
                       details={"client_email": p.client_email,
                                "days_since_sent": (datetime.utcnow() - p.sent_at).days})
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning(f"[proposals.followup] send failed project={p.id}: {type(e).__name__}: {e}")
        return False


def _html_escape(s: str) -> str:
    """HTML-escape для безопасной вставки клиентских данных в email."""
    from html import escape
    return escape(str(s or ""))


async def proposals_followup_loop():
    """Раз в час пробуждаемся, ищем КП требующие followup, шлём.

    Worker-lock через server.worker_lock защищает от дубликатов когда
    запущено несколько uvicorn-воркеров (4 на проде).

    Старт через 5 минут после boot — чтобы scheduler уже инициализировался
    и не конкурировать с критичными loops (creators_publish, agents cron).
    """
    from server.worker_lock import worker_lock
    await asyncio.sleep(300)
    while True:
        try:
            with worker_lock("proposals_followup",
                              ttl_sec=FOLLOWUP_TICK_INTERVAL - 60) as acquired:
                if acquired:
                    await _proposals_followup_tick()
        except Exception as e:
            log.error(f"[proposals.followup] loop error: {e}")
        await asyncio.sleep(FOLLOWUP_TICK_INTERVAL)
