"""
Email service — SMTP + fallback console logging.

Set in .env:
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=you@gmail.com
  SMTP_PASS=app_password
  SMTP_FROM=Obsidian AI <you@gmail.com>
  APP_URL=https://yourdomain.com

If SMTP_HOST is not set, codes are only printed to console (dev mode).
"""
import os, smtplib, logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "Obsidian AI <noreply@obsidian.ai>")
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
        log.info(f"Email sent to {to}")
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
  <div class="header"><h1>🔮 Obsidian AI</h1></div>
  <div class="body">
    <h2 style="margin:0 0 8px;font-size:20px;font-weight:700">{title}</h2>
    {body}
  </div>
  <div class="footer">© Obsidian AI · Это письмо отправлено автоматически, не отвечайте на него</div>
</div>
</body></html>"""


def send_verification(to: str, code: str) -> None:
    body = f"""
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">Для подтверждения email введите код на странице регистрации:</p>
    <div style="text-align:center"><div class="code">{code}</div></div>
    <p class="note">Код действителен 15 минут. Если вы не регистрировались — просто проигнорируйте это письмо.</p>"""
    _send(to, "Подтвердите email — Obsidian AI", _base_template("Подтверждение email", body))


def send_password_reset(to: str, code: str) -> None:
    body = f"""
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">Вы запросили сброс пароля. Введите код ниже:</p>
    <div style="text-align:center"><div class="code">{code}</div></div>
    <p class="note">Код действителен 15 минут. Если вы не запрашивали сброс — смените пароль немедленно.</p>"""
    _send(to, "Сброс пароля — Obsidian AI", _base_template("Сброс пароля", body))


def send_welcome(to: str, name: str) -> None:
    body = f"""
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">Привет, <strong>{name}</strong>! 🎉</p>
    <p style="color:rgba(199,196,215,0.8);line-height:1.6">Ваш аккаунт успешно подтверждён. На баланс начислено <strong style="color:#c0c1ff">5 000 токенов</strong> в подарок.</p>
    <div style="text-align:center"><a href="{APP_URL}" class="btn">Открыть Obsidian AI</a></div>"""
    _send(to, "Добро пожаловать в Obsidian AI!", _base_template("Добро пожаловать!", body))
