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
            })
            new_last_uid = max(new_last_uid, uid_int)
        M.logout()
    except Exception as e:
        log.error(f"[IMAP] {user}@{host}: {e}")
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
    """Главный цикл — проверка каждые 60 секунд."""
    log.info("IMAP loop started")
    while True:
        try:
            await imap_tick()
        except Exception as e:
            log.error(f"[IMAP] loop error: {e}")
        await asyncio.sleep(60)


def start_imap_watcher():
    asyncio.create_task(imap_loop())
