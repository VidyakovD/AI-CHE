"""
QR-логин: вход на десктопе через подтверждение со смартфона.

Flow:
  1. Десктоп: POST /qr-login/init → {token, expires_in}
     Десктоп показывает QR с URL https://aiche.ru/qr/{token}.
  2. Десктоп: GET /qr-login/poll/{token} (каждые 1.5s) — пока pending.
  3. Юзер на телефоне сканирует QR → открывается /qr/{token}.
     Если он залогинен в наш сервис на телефоне — видит подтверждение.
     Если нет — обычный логин, потом подтверждение.
  4. Телефон: POST /qr-login/approve/{token} (с auth) → status=approved.
  5. Десктоп получает approved в poll-е → читает access/refresh ОДИН РАЗ →
     ставит httpOnly cookies → перезагружает страницу → залогинен.

Безопасность:
  - Token = secrets.token_urlsafe(24) ≈ 192 bit, угадать невозможно.
  - TTL 120 секунд (короткое окно для физического сканирования).
  - Single-use: poll после consumed=True не отдаёт токены повторно.
  - Approve логирует IP+UA с двух сторон (init и approve) — если совпадают,
    это подозрительно (один и тот же браузер сканирует свой же QR), но не
    блокируем — может быть валидный кейс «компьютер и смартфон в одной сети».
  - Rate-limit на /qr-login/init и /qr-login/poll (см. server/security.py).
"""
import logging
import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user, _user_dict
from server.models import QrLoginSession, User
from server.auth import create_token, create_refresh_token, set_auth_cookies

log = logging.getLogger(__name__)
router = APIRouter(prefix="/qr-login", tags=["auth"])

# QR-сессия живёт 120 сек — окна физически достаточно отсканировать,
# но малое для брутфорса.
_TTL_SEC = 120


def _client_meta(request: Request) -> tuple[str, str]:
    from server.security import _get_client_ip
    ua = (request.headers.get("user-agent") or "")[:255]
    return _get_client_ip(request), ua


@router.post("/init")
def qr_init(request: Request, db: Session = Depends(get_db)):
    """Десктоп инициирует QR-сессию. Anonymous — токен ещё не привязан к юзеру."""
    token = secrets.token_urlsafe(24)
    ip, ua = _client_meta(request)
    db.add(QrLoginSession(
        token=token,
        status="pending",
        init_ip=ip, init_ua=ua,
        expires_at=datetime.utcnow() + timedelta(seconds=_TTL_SEC),
    ))
    db.commit()
    return {
        "token": token,
        "expires_in": _TTL_SEC,
        "qr_url": f"/qr/{token}",  # фронт сам подставит APP_URL
    }


@router.get("/poll/{token}")
def qr_poll(token: str, response: Response, db: Session = Depends(get_db)):
    """
    Десктоп опрашивает статус. Если approved — отдаём access/refresh ОДИН РАЗ
    и сразу ставим httpOnly cookies.
    """
    sess = db.query(QrLoginSession).filter_by(token=token).first()
    if not sess:
        raise HTTPException(404, "Сессия не найдена")
    if sess.expires_at < datetime.utcnow() and sess.status == "pending":
        sess.status = "expired"
        db.commit()
    if sess.status in ("expired", "cancelled"):
        return {"status": sess.status}
    if sess.status == "pending":
        return {"status": "pending"}
    # approved
    if sess.consumed:
        # Кто-то уже забрал токены — повторно не отдаём (защита от replay).
        return {"status": "consumed"}
    user = db.query(User).filter_by(id=sess.user_id).first()
    if not user:
        return {"status": "expired"}
    access = create_token(user.id, user.email)
    refresh = create_refresh_token(user.id, user.email)
    csrf = set_auth_cookies(response, access, refresh)
    sess.consumed = True
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("qr.consumed", user_id=user.id, target_type="qr_session",
                   target_id=str(sess.id))
    except Exception:
        pass
    return {
        "status": "approved",
        "access": access,
        "refresh": refresh,
        "csrf_token": csrf,
        "user": _user_dict(user),
    }


@router.get("/info/{token}")
def qr_info(token: str, db: Session = Depends(get_db)):
    """Информация о QR-сессии для страницы подтверждения (без секретов).
    Используется на /qr/{token} чтобы показать «откуда инициирован вход»."""
    sess = db.query(QrLoginSession).filter_by(token=token).first()
    if not sess:
        raise HTTPException(404, "QR-сессия не найдена")
    if sess.expires_at < datetime.utcnow():
        return {"status": "expired"}
    if sess.status == "cancelled":
        return {"status": "cancelled"}
    if sess.status == "approved":
        return {"status": "approved"}
    # pending — возвращаем только публичные метаданные о происхождении
    ua = sess.init_ua or ""
    # Грубая категоризация UA → "ПК Chrome" / "iPhone Safari" / ...
    label = _humanize_ua(ua)
    return {
        "status": "pending",
        "from": label,
        "expires_in": int((sess.expires_at - datetime.utcnow()).total_seconds()),
    }


@router.post("/approve/{token}")
def qr_approve(token: str, request: Request,
               db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    """Юзер на мобиле подтверждает вход. Требует авторизации."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email перед использованием QR-входа")
    sess = db.query(QrLoginSession).filter_by(token=token).first()
    if not sess:
        raise HTTPException(404, "QR-сессия не найдена")
    if sess.expires_at < datetime.utcnow():
        raise HTTPException(410, "QR-код истёк, обновите страницу на компьютере")
    if sess.status != "pending":
        raise HTTPException(409, f"QR-сессия уже {sess.status}")
    ip, ua = _client_meta(request)
    sess.status = "approved"
    sess.user_id = user.id
    sess.approve_ip = ip
    sess.approve_ua = ua
    sess.approved_at = datetime.utcnow()
    # Расширяем TTL чтобы у десктопа было время дотянуться при медленной сети
    sess.expires_at = datetime.utcnow() + timedelta(seconds=60)
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("qr.approved", user_id=user.id, target_type="qr_session",
                   target_id=str(sess.id),
                   details={"init_ua": (sess.init_ua or "")[:80],
                            "approve_ip": ip})
    except Exception:
        pass
    return {"status": "approved"}


@router.get("/image/{token}.png")
def qr_image(token: str, request: Request, db: Session = Depends(get_db)):
    """PNG QR-кода с URL `https://<host>/qr/{token}`. 220x220 px."""
    sess = db.query(QrLoginSession).filter_by(token=token).first()
    if not sess:
        raise HTTPException(404, "QR-сессия не найдена")
    if sess.expires_at < datetime.utcnow():
        raise HTTPException(410, "QR-сессия истекла")
    import os as _os, qrcode
    from io import BytesIO
    from fastapi.responses import Response as _Resp
    base = _os.getenv("APP_URL", "").rstrip("/")
    if not base:
        # Fallback: используем хост из запроса
        scheme = "https" if request.url.scheme == "https" else "http"
        base = f"{scheme}://{request.url.hostname}"
        if request.url.port and request.url.port not in (80, 443):
            base += f":{request.url.port}"
    url = f"{base}/qr/{token}"
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1c1c1c", back_color="#ffffff")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return _Resp(content=buf.getvalue(), media_type="image/png",
                 headers={"Cache-Control": "no-store"})


@router.post("/cancel/{token}")
def qr_cancel(token: str, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    """Юзер отказался подтверждать. Десктоп получит status=cancelled."""
    sess = db.query(QrLoginSession).filter_by(token=token).first()
    if not sess:
        raise HTTPException(404, "QR-сессия не найдена")
    if sess.status != "pending":
        return {"status": sess.status}
    sess.status = "cancelled"
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("qr.cancelled", user_id=user.id, target_type="qr_session",
                   target_id=str(sess.id))
    except Exception:
        pass
    return {"status": "cancelled"}


def _humanize_ua(ua: str) -> str:
    """Грубая категоризация UA для отображения юзеру."""
    if not ua:
        return "неизвестное устройство"
    s = ua.lower()
    device = "ПК"
    if any(x in s for x in ("iphone", "android", "mobile", "ipad")):
        device = "Смартфон" if "ipad" not in s else "Планшет"
    browser = "браузер"
    for needle, name in [
        ("yabrowser", "Яндекс.Браузер"),
        ("edg/", "Edge"),
        ("opr/", "Opera"),
        ("opera", "Opera"),
        ("firefox", "Firefox"),
        ("chrome", "Chrome"),
        ("safari", "Safari"),
    ]:
        if needle in s:
            browser = name
            break
    os_name = ""
    if "windows" in s: os_name = "Windows"
    elif "mac os" in s or "macintosh" in s: os_name = "macOS"
    elif "linux" in s: os_name = "Linux"
    elif "iphone" in s or "ipad" in s: os_name = "iOS"
    elif "android" in s: os_name = "Android"
    parts = [device, browser]
    if os_name:
        parts.append(os_name)
    return " · ".join(parts)
