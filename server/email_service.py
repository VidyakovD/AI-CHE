"""
Email service — SMTP + fallback console logging.

Set in .env:
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=you@gmail.com
  SMTP_PASS=app_password
  SMTP_FROM=AI Студия Че <you@gmail.com>
  APP_URL=https://yourdomain.com

If SMTP_HOST is not set, codes are only printed to console (dev mode).
"""
import os, smtplib, logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.utils import make_msgid, formatdate

log = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "AI Студия Че <noreply@ai-che.ru>")
APP_URL   = os.getenv("APP_URL", "http://localhost:8000")


def _send(to: str, subject: str, html: str) -> None:
    if not SMTP_HOST:
        log.warning(f"[EMAIL STUB] To: {to} | Subject: {subject}")
        # print body so dev can see the link/code
        import re
        codes = re.findall(r'\b\d{6}\b', html)
        links = re.findall(r'href="([^"]+verify[^"]+)"', html)
        if codes:  log.warning(f"[EMAIL STUB] Code: {codes[0]}")
        if links:  log.warning(f"[EMAIL STUB] Link: {links[0]}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM
    msg["To"]      = to
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, to, msg.as_string())
        try:
            from server.security import mask_email
            log.info(f"Email sent to {mask_email(to)}")
        except Exception:
            log.info("Email sent")
    except Exception as e:
        log.error(f"Email send failed: {e}")
        raise


# ── templates ─────────────────────────────────────────────────────────────────

def _base_template(title: str, body: str) -> str:
    return f"""
<!DOCTYPE html><html><head><meta charset="utf-8"/>
<style>
  body{{margin:0;padding:0;background:#131313;font-family:'Inter',Arial,sans-serif;color:#E5E2E1}}
  .wrap{{max-width:520px;margin:40px auto;background:#1C1B1B;border-radius:16px;overflow:hidden;border:1px solid rgba(70,69,84,0.3)}}
  .header{{background:linear-gradient(135deg,#c0c1ff,#ddb7ff);padding:32px;text-align:center}}
  .header h1{{margin:0;font-size:22px;font-weight:800;color:#0d0096;letter-spacing:-0.5px}}
  .body{{padding:32px}}
  .code{{display:inline-block;background:#2A2A2A;border:1px solid rgba(192,193,255,0.3);border-radius:12px;padding:16px 32px;font-size:32px;font-weight:800;letter-spacing:8px;color:#c0c1ff;margin:24px 0}}
  .btn{{display:inline-block;background:linear-gradient(135deg,#c0c1ff,#ddb7ff);color:#0d0096;font-weight:700;font-size:15px;padding:14px 32px;border-radius:12px;text-decoration:none;margin:16px 0}}
  .note{{font-size:12px;color:rgba(199,196,215,0.5);margin-top:24px;line-height:1.6}}
  .footer{{padding:20px 32px;border-top:1px solid rgba(70,69,84,0.2);font-size:11px;color:rgba(199,196,215,0.4);text-align:center}}
</style></head><body>
<div class="wrap">
  <div class="header"><h1>🤖 AI Студия Че</h1></div>
  <div class="body">
    <h2 style="margin:0 0 8px;font-size:20px;font-weight:700">{title}</h2>
    {body}
  </div>
  <div class="footer">© AI Студия Че · Это письмо отправлено автоматически, не отвечайте на него</div>
</div>
</body></html>"""


def send_verification(to: str, code: str) -> None:
    body = f"""
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">Для подтверждения email введите код на странице регистрации:</p>
    <div style="text-align:center"><div class="code">{code}</div></div>
    <p class="note">Код действителен 15 минут. Если вы не регистрировались — просто проигнорируйте это письмо.</p>"""
    _send(to, "Подтвердите email — AI Студия Че", _base_template("Подтверждение email", body))


def send_password_reset(to: str, code: str) -> None:
    body = f"""
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">Вы запросили сброс пароля. Введите код ниже:</p>
    <div style="text-align:center"><div class="code">{code}</div></div>
    <p class="note">Код действителен 15 минут. Если вы не запрашивали сброс — смените пароль немедленно.</p>"""
    _send(to, "Сброс пароля — AI Студия Че", _base_template("Сброс пароля", body))


def send_welcome(to: str, name: str) -> None:
    body = f"""
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">Привет, <strong>{name}</strong>! 🎉</p>
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">Ваш аккаунт успешно подтверждён. На баланс начислено <strong style="color:#c0c1ff">5 000 токенов</strong> в подарок.</p>
    <div style="text-align:center"><a href="{APP_URL}" class="btn">Открыть Obsidian AI</a></div>"""
    _send(to, "Добро пожаловать в Obsidian AI!", _base_template("Добро пожаловать!", body))


def send_with_attachment(to: str, subject: str, html_body: str,
                         attachments: list[tuple[str, bytes, str]] | None = None,
                         in_reply_to: str | None = None,
                         from_override: str | None = None) -> str | None:
    """Универсальный SMTP-send с возможностью аттачей и In-Reply-To.

    attachments — список (filename, bytes, mime_type), напр.
        [("kp.pdf", pdf_bytes, "application/pdf")]
    in_reply_to — Message-ID входящего письма (для threading в Gmail/Yandex)
    from_override — нестандартный From (если SMTP-relay поддерживает)

    Возвращает Message-ID отправленного письма (или None если SMTP не настроен).
    """
    if not SMTP_HOST:
        log.warning(f"[EMAIL STUB] To: {to} | Subject: {subject}")
        log.warning(f"[EMAIL STUB] Body length: {len(html_body or '')} chars, attachments: {len(attachments or [])}")
        return None

    msg = MIMEMultipart("mixed")
    sender = from_override or SMTP_FROM
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    new_msg_id = make_msgid(domain=(SMTP_HOST.split(".")[-2] + "." + SMTP_HOST.split(".")[-1])
                                   if "." in SMTP_HOST else "aiche.local")
    msg["Message-ID"] = new_msg_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    # Тело — alternative (text + html)
    body_alt = MIMEMultipart("alternative")
    # Plain-text fallback из html (грубо — без html-тегов)
    import re as _re_html
    plain = _re_html.sub(r"<[^>]+>", " ", html_body or "")
    plain = _re_html.sub(r"\s+", " ", plain).strip()[:5000]
    body_alt.attach(MIMEText(plain, "plain", "utf-8"))
    body_alt.attach(MIMEText(html_body or "", "html", "utf-8"))
    msg.attach(body_alt)

    # Аттачи
    for fname, data, mtype in (attachments or []):
        if not data:
            continue
        maintype, _, subtype = mtype.partition("/")
        att = MIMEApplication(data, _subtype=subtype or "octet-stream")
        att.add_header("Content-Disposition", "attachment", filename=fname)
        msg.attach(att)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(sender, to, msg.as_string())
        try:
            from server.security import mask_email
            log.info(f"Email+attachment sent to {mask_email(to)} attach={len(attachments or [])}")
        except Exception:
            log.info("Email sent")
        return new_msg_id
    except Exception as e:
        log.error(f"Email send (with attachment) failed: {type(e).__name__}: {e}")
        raise


def send_login_alert(to: str, name: str, ip: str, when: str) -> None:
    """Уведомление о входе в аккаунт с нового IP. Если это не вы — смените пароль."""
    body = f"""
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">Здравствуйте, <strong>{name or 'пользователь'}</strong>!</p>
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">Зафиксирован вход в ваш аккаунт с нового устройства:</p>
    <ul style="color:rgba(199,196,215,0.8);line-height:1.8">
      <li>Время: <strong>{when}</strong> (UTC)</li>
      <li>IP-адрес: <code style="background:#2A2A2A;padding:2px 6px;border-radius:4px">{ip}</code></li>
    </ul>
    <p style="color:rgba(199,196,215,0.8);line-height:1.6"><strong>Это были вы?</strong> Тогда ничего делать не нужно.</p>
    <p style="color:rgba(199,196,215,0.8);line-height:1.6"><strong>Не вы?</strong> Срочно смените пароль и проверьте свои API-ключи:</p>
    <div style="text-align:center"><a href="{APP_URL}/?openCabinet=1" class="btn">Открыть кабинет</a></div>
    <p class="note">Уведомление отправляется только при входе с нового IP, чтобы не спамить.</p>"""
    _send(to, "Вход с нового устройства — AI Студия Че",
          _base_template("🔔 Новый вход в аккаунт", body))


def send_low_balance_alert(to: str, name: str, balance: int, threshold: int) -> None:
    """Уведомление о низком балансе CH."""
    body = f"""
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">Здравствуйте, <strong>{name or 'пользователь'}</strong>!</p>
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">На вашем балансе AI Студии Че осталось <strong style="color:#c0c1ff">{balance} CH</strong> (лимит уведомления — {threshold} CH).</p>
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">Чтобы боты и агенты продолжали работать без перерыва, пополните баланс:</p>
    <div style="text-align:center"><a href="{APP_URL}/?openCabinet=1" class="btn">Пополнить баланс</a></div>
    <p class="note">Порог уведомления можно настроить в личном кабинете → Настройки → Уведомления.</p>"""
    _send(to, "Низкий баланс CH — AI Студия Че", _base_template("⚠️ Баланс заканчивается", body))
