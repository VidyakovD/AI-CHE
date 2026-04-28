"""
IMAP Email Trigger — периодически проверяет inbox по заданным credentials,
при появлении новых писем запускает воркфлоу бота.
"""
import asyncio, logging, json, imaplib, email as _email
from email.header import decode_header
from datetime import datetime

log = logging.getLogger("imap")


def _decode_mime(s: str) -> str:
    if not s: return ""
    try:
        parts = decode_header(s)
        result = []
        for text, charset in parts:
            if isinstance(text, bytes):
                result.append(text.decode(charset or "utf-8", errors="ignore"))
            else:
                result.append(text)
        return "".join(result)
    except Exception:
        return s


def _extract_body(msg) -> str:
    """Извлекает text/plain часть письма."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="ignore")
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="ignore")
    return ""


def _fetch_new_emails_sync(host, port, user, password, use_ssl, last_uid, limit=10) -> list:
    """Синхронно забирает новые письма через IMAP. Возвращает list[dict] + новый last_uid."""
    emails = []
    new_last_uid = last_uid
    try:
        M = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
        M.login(user, password)
        M.select("INBOX")
        # UID SEARCH: (UID last_uid+1:*)
        search_criteria = f"UID {last_uid+1}:*" if last_uid > 0 else "ALL"
        typ, data = M.uid("search", None, search_criteria)
        if typ != "OK":
            M.logout()
            return [], last_uid
        uids = data[0].split()
        # Берём последние `limit`
        uids = uids[-limit:] if len(uids) > limit else uids
        for uid in uids:
            uid_int = int(uid)
            if uid_int <= last_uid:
                continue
            typ, msg_data = M.uid("fetch", uid, "(RFC822)")
            if typ != "OK": continue
            raw = msg_data[0][1]
            msg = _email.message_from_bytes(raw)
            emails.append({
                "uid": uid_int,
                "from": _decode_mime(msg.get("From", "")),
                "to": _decode_mime(msg.get("To", "")),
                "subject": _decode_mime(msg.get("Subject", "")),
                "date": msg.get("Date", ""),
                "body": _extract_body(msg)[:5000],
                # Headers для threading: In-Reply-To указывает на оригинальное
                # сообщение; References — цепочку. Используется в B.7
                # (proposals threading) — если пришёл ответ на наше КП-письмо,
                # автоматически отмечаем replied_at + crm_stage=replied.
                "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
                "references": (msg.get("References") or "").strip(),
                "message_id": (msg.get("Message-ID") or "").strip(),
            })
            new_last_uid = max(new_last_uid, uid_int)
        M.logout()
    except Exception as e:
        from server.security import mask_email
        log.error(f"[IMAP] {mask_email(user)}@{host}: {e}")
    return emails, new_last_uid


async def check_imap_for_user(cred):
    """Проверить одного юзера, вернуть новые письма."""
    from server.secrets_crypto import decrypt
    password_plain = decrypt(cred.password)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: _fetch_new_emails_sync(
            cred.host, cred.port, cred.username, password_plain,
            cred.use_ssl, cred.last_uid or 0, 10
        )
    )
    return result


async def imap_tick():
    """Одна проверка — бежим по всем IMAP credentials + привязанным ботам с trigger_imap."""
    from server.db import SessionLocal
    from server.models import ImapCredential, ChatBot
    from server.chatbot_engine import _execute_workflow

    db = SessionLocal()
    try:
        creds = db.query(ImapCredential).all()
        bots = db.query(ChatBot).filter_by(status="active").all()
    finally:
        db.close()

    # Сначала соберём привязки: какие боты имеют trigger_imap с указанием cred_id
    bot_by_cred: dict[int, list] = {}
    for bot in bots:
        if not bot.workflow_json: continue
        try:
            wf = json.loads(bot.workflow_json)
        except Exception:
            continue
        for n in wf.get("wfc_nodes", []):
            if n.get("type") == "trigger_imap":
                cred_id = n.get("cfg", {}).get("cred_id", "")
                try:
                    cid = int(cred_id)
                except Exception:
                    continue
                bot_by_cred.setdefault(cid, []).append((bot, wf))

    for cred in creds:
        if cred.id not in bot_by_cred: continue
        try:
            emails, new_uid = await check_imap_for_user(cred)
        except Exception as e:
            log.error(f"[IMAP tick] cred {cred.id}: {e}")
            continue
        if not emails: continue

        # Обновляем last_uid
        db = SessionLocal()
        try:
            c = db.query(ImapCredential).filter_by(id=cred.id).first()
            if c:
                c.last_uid = new_uid
                db.commit()
            # B.7 — Threading: ищем proposals у которых outbox_message_id
            # совпадает с In-Reply-To/References входящего письма.
            # Если нашли — отмечаем replied_at + crm_stage='replied'.
            #
            # Защита от подделки threading'а: если кто-то узнал Message-ID
            # исходящего письма (через cc/forward/leak), он мог бы прислать
            # фейковый "ответ" и закрыть сделку. Поэтому проверяем, что
            # отправитель совпадает с client_email проекта (полный адрес или
            # хотя бы домен). Если client_email не задан — fallback на
            # threading без проверки (обратная совместимость).
            from server.models import ProposalProject as _PP
            from datetime import datetime as _dt
            import re as _re

            def _addr(s: str) -> str:
                """Извлекает 'a@b.c' из строки 'Имя <a@b.c>' или просто 'a@b.c'."""
                if not s:
                    return ""
                m = _re.search(r"[\w.\-+]+@[\w.\-]+\.[A-Za-z]{2,}", s)
                return (m.group(0) if m else s).strip().lower()

            for em in emails:
                refs = []
                if em.get("in_reply_to"):
                    refs.append(em["in_reply_to"])
                if em.get("references"):
                    refs.extend(em["references"].split())
                if not refs:
                    continue
                from_addr = _addr(em.get("from", ""))
                from_dom = from_addr.split("@", 1)[1] if "@" in from_addr else ""
                for r in refs:
                    rid = r.strip().strip("<>").strip()
                    # Раньше допускался любой rid >=10 символов с LIKE %rid% —
                    # это слишком широко. Принимаем только точные совпадения.
                    if not rid or len(rid) < 16 or "@" not in rid:
                        continue
                    targets = (db.query(_PP)
                                 .filter(_PP.outbox_message_id != None)
                                 .filter((_PP.outbox_message_id == r.strip()) |
                                         (_PP.outbox_message_id == f"<{rid}>") |
                                         (_PP.outbox_message_id == rid))
                                 .all())
                    for proj in targets:
                        if proj.replied_at:
                            continue
                        # Anti-spoof: если client_email указан — отправитель
                        # должен совпадать (по полному адресу или домену).
                        client_email = (getattr(proj, "client_email", "") or "").lower().strip()
                        if client_email and from_addr:
                            if (from_addr != client_email
                                    and from_dom != client_email.split("@", 1)[-1]):
                                log.warning(
                                    f"[IMAP threading] proposal {proj.id}: from={from_addr} "
                                    f"!= client={client_email} → skip (possible spoof)")
                                continue
                        proj.replied_at = _dt.utcnow()
                        if (proj.crm_stage or "new") in ("new", "sent", "opened"):
                            proj.crm_stage = "replied"
                        log.info(f"[IMAP threading] proposal {proj.id} → replied (from={from_addr})")
                        try:
                            from server.audit_log import log_action as _la
                            _la("proposal.client_replied", user_id=proj.user_id,
                                target_type="proposal", target_id=str(proj.id),
                                details={"from": em.get("from", "")[:80]})
                        except Exception:
                            pass
            db.commit()
        finally:
            db.close()

        # Запускаем воркфлоу для каждого связанного бота по каждому письму
        for bot, wf in bot_by_cred[cred.id]:
            for em in emails:
                try:
                    text = f"From: {em['from']}\nSubject: {em['subject']}\n\n{em['body'][:2000]}"
                    await _execute_workflow(
                        bot=bot,
                        chat_id=f"imap_{cred.id}_{em['uid']}",
                        user_text=text,
                        platform="imap",
                        user_name=em['from'],
                        workflow=wf,
                        extra_ctx={"email": em, "is_email": True},
                    )
                except Exception as e:
                    log.error(f"[IMAP→workflow] bot={bot.id}: {e}")


async def imap_loop():
    """Главный цикл — проверка каждые 60 секунд.
    Advisory lock гарантирует что при нескольких workers письма обрабатываются один раз."""
    from server.worker_lock import worker_lock
    log.info("IMAP loop started")
    while True:
        try:
            with worker_lock("imap_tick", ttl_sec=55) as acquired:
                if acquired:
                    await imap_tick()
        except Exception as e:
            log.error(f"[IMAP] loop error: {e}")
        await asyncio.sleep(60)


def start_imap_watcher():
    asyncio.create_task(imap_loop())
