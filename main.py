"""
AI Студия Че — FastAPI application entry point.
All endpoints live in server/routes/*.py; this file wires them together.
"""
import os, logging
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv

from server.db import SessionLocal, engine
from server import models  # noqa: F401 -- needed for table creation

# ── Routers ────────────────────────────────────────────────────────────────────
from server.routes.auth import router as auth_router
from server.routes.payments import router as payments_router
from server.routes.chat import router as chat_router
from server.routes.user import router as user_router
from server.routes.admin import router as admin_router, _load_all_apikeys_from_db
from server.routes.solutions import router as solutions_router
from server.routes.sites import router as sites_router
from server.routes.presentations import router as presentations_router
from server.routes.agent import router as agent_router, init_agent_queue
import server.agents.registry  # noqa: F401 — registers all agent types on import
from server.routes.public import router as public_router, startup_public
from server.routes.user_apikeys import router as user_apikeys_router
from server.routes.oauth import router as oauth_router
from server.routes.chatbots import router as chatbots_router
from server.routes.assets import router as assets_router
from server.routes.webhook import router as webhook_router
from server.routes.widget import router as widget_router
from server.routes.proposals import router as proposals_router
from server.routes.assistant import router as assistant_router
from server.routes.qr_login import router as qr_login_router
from server.routes.mobile import router as mobile_router
from server.routes.knowledge import router as knowledge_router

load_dotenv()


# ── Логирование: structured JSON опционально ────────────────────────────────
# В проде: STRUCTURED_LOGS=1 → JSON-строки (grep/jq friendly, для централизованных логов).
# В деве: текстовый формат, человекочитаемый.
def _setup_logging():
    import json as _json, sys
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)

    if os.getenv("STRUCTURED_LOGS", "").lower() in ("1", "true", "yes"):
        class _JsonFmt(logging.Formatter):
            def format(self, record):
                payload = {
                    "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
                if record.exc_info:
                    payload["exc"] = self.formatException(record.exc_info)
                # Любые extra-поля — добавляем в payload
                for key in ("user_id", "bot_id", "payment_id", "request_id", "ip"):
                    if hasattr(record, key):
                        payload[key] = getattr(record, key)
                return _json.dumps(payload, ensure_ascii=False)
        handler.setFormatter(_JsonFmt())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Defence-in-depth: маскируем секреты на уровне root-handler. Любой логгер
    # (свой / SDK / framework) пройдёт через фильтр на handler'е, не нужно
    # навешивать на каждый логгер отдельно. Фильтр режет sk-*/Bearer/AIza.../
    # прокси-креды/key= в URL — см. server.ai._SecretFilter.
    try:
        from server.ai import _SecretFilter as _SF
        handler.addFilter(_SF())
    except Exception:
        pass


_setup_logging()
log = logging.getLogger(__name__)


# ── Sentry опционально ──────────────────────────────────────────────────────
# Если SENTRY_DSN задан — инициализируем перед созданием FastAPI app, чтобы
# отлавливать exceptions в startup-хуках и middleware. PII (email, токены) не шлём.
def _setup_sentry():
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("SENTRY_ENV", "production"),
            release=os.getenv("APP_VERSION", "unknown"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_RATE", "0.05")),
            send_default_pii=False,
            integrations=[FastApiIntegration(), StarletteIntegration()],
        )
        log.info("Sentry initialized")
    except ImportError:
        log.warning("SENTRY_DSN задан, но sentry-sdk не установлен — pip install sentry-sdk[fastapi]")
    except Exception as e:
        log.error(f"Sentry init failed: {e}")


_setup_sentry()

models.Base.metadata.create_all(bind=engine)
from server.db import apply_lightweight_migrations  # noqa: E402
apply_lightweight_migrations()

# Засеять дефолтные цены в БД (no-op если уже есть)
from server.pricing import seed_pricing_defaults  # noqa: E402
seed_pricing_defaults()

app = FastAPI(title="AI Студия Че")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── CORS ───────────────────────────────────────────────────────────────────────
_raw_origins = os.getenv("ALLOWED_ORIGINS", "")
_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()] if _raw_origins else []
_dev_mode = os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")
_app_env = os.getenv("APP_ENV", "production").lower()  # дефолт production — fail-safe
# Защита от опечатки: даже если кто-то в .env проставит DEV_MODE=true рядом
# с APP_ENV=production, мы НЕ открываем CORS на "*". Лучше чтобы сервис
# не стартанул, чем работал с открытыми кросс-доменными запросами.
if _dev_mode and _app_env == "production":
    raise RuntimeError(
        "DEV_MODE=true несовместим с APP_ENV=production. "
        "Уберите DEV_MODE или установите APP_ENV=dev."
    )
if not _origins:
    if _dev_mode and _app_env != "production":
        log.warning("DEV_MODE: CORS allows all origins — НЕ ВКЛЮЧАЙТЕ В ПРОДЕ")
        _origins = ["*"]
    else:
        raise RuntimeError(
            "ALLOWED_ORIGINS не задан. В проде укажите домены через запятую "
            "(например: https://aiche.ru,https://www.aiche.ru). "
            "Для локальной разработки установите DEV_MODE=true и APP_ENV=dev."
        )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=bool(_origins) and _origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from server.security import rate_limit_middleware  # noqa: E402
from fastapi import Request  # noqa: E402  ВАЖНО: до middleware с типом Request
app.middleware("http")(rate_limit_middleware)


# ── CSRF middleware (double-submit cookie) ─────────────────────────────────
# Защита от CSRF после миграции JWT в httpOnly cookie. Браузер автоматически
# шлёт cookie на каждый запрос — даже с чужого origin → атакующий мог бы
# выполнить любой POST. Защита: на write-методах требуем заголовок
# X-CSRF-Token равный cookie csrf_token. Атакующий с другого origin не
# может прочитать cookie через document.cookie (CORS) → не сможет
# подделать заголовок.
#
# Исключения (write без CSRF check):
#   - /payment/webhook    — внешний webhook ЮKassa (HMAC проверка)
#   - /webhook/*          — TG/VK/Avito/MAX webhooks (свои секреты)
#   - /auth/login,/register,/oauth/*,/exchange — токена ещё нет
#   - /widget/ws          — WS не имеет body, проверяется Origin
from server.auth import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, ACCESS_COOKIE_NAME  # noqa: E402

_CSRF_EXEMPT_PREFIXES = (
    "/payment/webhook",
    "/webhook/",
    "/auth/login",
    "/auth/register",
    "/auth/verify-email",
    "/auth/resend-verify",
    "/auth/reset-password",
    "/auth/request-reset",
    "/auth/forgot-password",
    "/auth/refresh",
    "/auth/oauth/",
    "/auth/logout",
    "/widget/",
    "/internal/deploy",  # CI deploy hook (свой DEPLOY_TOKEN)
)


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    # Только write-методы. GET/HEAD/OPTIONS — CORS уже защищает от cross-origin.
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)
    path = request.url.path or ""
    for prefix in _CSRF_EXEMPT_PREFIXES:
        if path.startswith(prefix):
            return await call_next(request)
    # CSRF нужен ТОЛЬКО если запрос использует cookie-based auth.
    # Если есть Authorization header С НЕПУСТЫМ ТОКЕНОМ — это API-клиент / legacy
    # frontend, cross-site атака не может подделать Authorization (CORS блокирует).
    # ВАЖНО: проверяем длину после "Bearer ", иначе атакующий может прислать
    # `Authorization: Bearer ` (только префикс) и обойти CSRF проверку.
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer ") and len(auth_header.strip()) > 10:
        return await call_next(request)
    # Если нет ни cookie с access_token — пропускаем (anon/public endpoint
    # сам решит надо ли auth)
    if not request.cookies.get(ACCESS_COOKIE_NAME):
        return await call_next(request)
    # Cookie-based auth → требуем CSRF
    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME, "")
    header_csrf = request.headers.get(CSRF_HEADER_NAME, "")
    import hmac as _hmac
    if not cookie_csrf or not _hmac.compare_digest(cookie_csrf, header_csrf):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)
    return await call_next(request)


# ── Request-ID middleware (для трассировки в structured logs) ───────────────
import uuid as _uuid_mod  # noqa: E402

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Каждый запрос получает X-Request-ID — попадает в response header
    и в `log.extra={'request_id': ...}` через record.request_id.
    Помогает связать строки логов одного юзер-запроса в Sentry/grafana."""
    rid = request.headers.get("X-Request-ID") or _uuid_mod.uuid4().hex[:16]
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


# ── Body size limit (10 MB для JSON-эндпоинтов) + security headers ─────────────
_MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(12 * 1024 * 1024)))  # 12MB — upload до 10MB + оверхед

@app.middleware("http")
async def body_size_and_headers(request: Request, call_next):
    # Body size limit (проверка по Content-Length — для больших chunked можно обойти,
    # но базовая защита от «100 GB JSON»)
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_BODY_BYTES:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Payload too large"}, status_code=413)
    response = await call_next(request)
    # Security headers (OWASP recommended minimum)
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    # CSP — умеренная: inline JS/CSS разрешены (прод-ввёрстано), CDN-зависимости явно перечислены.
    # Не выставляем CSP на /uploads и /sites/hosted (там пользовательский контент).
    path = request.url.path or ""
    if not path.startswith("/uploads") and not path.startswith("/sites/hosted"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com https://yookassa.ru https://*.yookassa.ru; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://cdn.tailwindcss.com; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "img-src 'self' data: blob: https:; "
            "media-src 'self' data: blob: https:; "
            "connect-src 'self' https: wss:; "
            # blob: нужен для превью сайтов в /sites.html (URL.createObjectURL с HTML)
            "frame-src 'self' blob: https://yookassa.ru https://*.yookassa.ru; "
            "object-src 'none'; base-uri 'self'; form-action 'self'"
        )
    return response

# ── Include all routers ────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(payments_router)
app.include_router(chat_router)
app.include_router(user_router)
app.include_router(admin_router)
app.include_router(solutions_router)
app.include_router(sites_router)
app.include_router(presentations_router)
app.include_router(agent_router)
app.include_router(user_apikeys_router)
app.include_router(oauth_router)
app.include_router(chatbots_router)
app.include_router(webhook_router)
app.include_router(widget_router)
app.include_router(public_router)
app.include_router(assets_router)
app.include_router(proposals_router)
app.include_router(assistant_router)
app.include_router(qr_login_router)
app.include_router(mobile_router)
app.include_router(knowledge_router)

# ── Static files (uploads) ────────────────────────────────────────────────────
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# ── Static files (sites hosted) ───────────────────────────────────────────────
_sites_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "sites")
os.makedirs(_sites_dir, exist_ok=True)
app.mount("/sites/hosted", StaticFiles(directory=_sites_dir), name="sites-hosted")

# ── HTML pages ─────────────────────────────────────────────────────────────────
_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "views")

# no-cache headers для HTML чтобы браузер всегда брал свежую версию после деплоя
_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}

def _html(name: str) -> FileResponse:
    return FileResponse(os.path.join(_BASE, name), headers=_NO_CACHE)

@app.get("/icons.js", include_in_schema=False)
def serve_icons():
    """Единый набор векторных иконок — заменяет эмодзи в UI.

    Cache: 60 секунд + must-revalidate. Так после деплоя клиент получит свежий
    icons.js за минуту, а не через час (старый max-age=3600 однажды залип
    PWA-кэшем и юзер не видел обновлений UI до жёсткого hard-reload).
    """
    return FileResponse(
        os.path.join(_BASE, "icons.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=60, must-revalidate"},
    )


@app.get("/knowledge-ui.js", include_in_schema=False)
def serve_knowledge_ui():
    """Общая модалка управления RAG-базой знаний (для агентов и ботов)."""
    return FileResponse(
        os.path.join(_BASE, "knowledge-ui.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=60, must-revalidate"},
    )


# ── PWA: manifest, service worker, icon ───────────────────────────────────
# После регистрации SW + manifest + theme-color сайт можно установить
# «как приложение» на iOS, Android, Windows, Mac, Linux. На десктопе
# работает install-prompt в Chrome/Edge, на iOS — через Share → "На экран Домой".

@app.get("/manifest.json", include_in_schema=False)
def serve_manifest():
    return FileResponse(
        os.path.join(_BASE, "manifest.json"),
        media_type="application/manifest+json",
        # Манифест меняется редко — кэшируем на час, при изменении PWA-инфры
        # достаточно поднять CACHE_VERSION в sw.js (там уже наш кэш-механизм).
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/sw.js", include_in_schema=False)
def serve_sw():
    """Service Worker — регистрация на корне сайта.
    ВАЖНО: scope SW определяется его расположением. Раздаём с /, чтобы
    он контролировал всё приложение."""
    return FileResponse(
        os.path.join(_BASE, "sw.js"),
        media_type="application/javascript",
        # SW сам обновляется при изменении байтов — браузер сравнивает.
        # Поэтому no-store не нужен, но max-age=0 чтобы ловить обновления.
        headers={"Cache-Control": "public, max-age=0, must-revalidate",
                 "Service-Worker-Allowed": "/"},
    )


@app.get("/icon.svg", include_in_schema=False)
def serve_icon():
    """Legacy — раньше PWA-иконкой был SVG. Сейчас бренд-лого PNG-набор
    отдаётся через /logo-*.png. Оставлен для обратной совместимости со
    старыми SW-кэшами и закладками."""
    return FileResponse(
        os.path.join(_BASE, "icon.svg"),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# Бренд-лого: набор разных размеров с прозрачным фоном.
@app.get("/logo-{variant}.png", include_in_schema=False)
def serve_logo(variant: str):
    """Раздача брендовых иконок: 32, 192, 512, maskable-512, email-128."""
    allowed = {"32", "192", "512", "maskable-512", "email-128"}
    if variant not in allowed:
        raise HTTPException(404)
    return FileResponse(
        os.path.join(_BASE, f"logo-{variant}.png"),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/favicon.ico", include_in_schema=False)
def serve_favicon():
    """Браузер запрашивает favicon.ico по умолчанию. Отдаём 32×32 PNG —
    все современные браузеры принимают через Content-Type."""
    return FileResponse(
        os.path.join(_BASE, "favicon.png"),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/qr/{token}", include_in_schema=False)
def serve_qr_confirm(token: str):
    """Страница подтверждения QR-логина (для мобильного скана).
    Сама страница без auth — внутри JS проверяет авторизацию и рисует
    кнопки «Подтвердить» / «Отмена»."""
    return FileResponse(os.path.join(_BASE, "qr_confirm.html"), headers=_NO_CACHE)


@app.get("/mobile.html", include_in_schema=False)
def serve_mobile():
    """Лайт-режим: компактный дашборд для смартфонов с голосовым управлением."""
    return FileResponse(os.path.join(_BASE, "mobile.html"), headers=_NO_CACHE)


@app.get("/m", include_in_schema=False)
def serve_mobile_short():
    """Короткий алиас для лайт-режима."""
    return FileResponse(os.path.join(_BASE, "mobile.html"), headers=_NO_CACHE)


@app.get("/", include_in_schema=False)
def serve_root():
    return _html("index.html")

@app.get("/index.html", include_in_schema=False)
def serve_index():
    return _html("index.html")

@app.get("/admin.html", include_in_schema=False)
def serve_admin():
    return _html("admin.html")

@app.get("/agents.html", include_in_schema=False)
def serve_agents():
    return _html("agents.html")

@app.get("/chatbots.html", include_in_schema=False)
def serve_chatbots():
    return _html("chatbots.html")

@app.get("/workflows.html", include_in_schema=False)
def serve_workflows():
    return _html("workflows.html")

@app.get("/workflow.html", include_in_schema=False)
def serve_workflow_editor():
    return _html("workflow.html")

@app.get("/sites.html", include_in_schema=False)
def serve_sites():
    return _html("sites.html")

@app.get("/presentations.html", include_in_schema=False)
def serve_presentations():
    return _html("presentations.html")

@app.get("/proposals.html", include_in_schema=False)
def serve_proposals():
    return _html("proposals.html")


@app.get("/p/{public_token}", include_in_schema=False)
def serve_public_proposal(public_token: str):
    """Публичная ссылка на КП. Без auth, по токену.
    При первом открытии — отмечает opened_at + crm_stage=opened."""
    from fastapi.responses import FileResponse, JSONResponse
    from server.db import db_session
    from server.models import ProposalProject
    from datetime import datetime as _dt
    if not public_token or len(public_token) < 16:
        return JSONResponse({"detail": "Invalid token"}, status_code=404)
    with db_session() as _db:
        p = _db.query(ProposalProject).filter_by(public_token=public_token).first()
        if not p or not p.generated_pdf:
            return JSONResponse({"detail": "КП не найдено или удалено"}, status_code=404)
        # Tracking первого открытия
        first_open = (p.opened_at is None)
        if first_open:
            p.opened_at = _dt.utcnow()
            if (p.crm_stage or "new") in ("new", "sent"):
                p.crm_stage = "opened"
            _db.commit()
        # Путь к файлу
        base = os.path.dirname(os.path.abspath(__file__))
        pdf_path = os.path.join(base, p.generated_pdf.lstrip("/"))
        if not os.path.exists(pdf_path):
            return JSONResponse({"detail": "PDF файл недоступен"}, status_code=404)
        # Audit-лог только при первом открытии (не спамить)
        if first_open:
            try:
                from server.audit_log import log_action
                log_action("proposal.public_opened", user_id=p.user_id,
                            target_type="proposal", target_id=str(p.id))
            except Exception:
                pass
        # Иконка для имени файла из проекта
        import re as _re
        safe = _re.sub(r"[^\w\-]", "_", p.name or "proposal")[:40]
        return FileResponse(pdf_path, media_type="application/pdf",
                             filename=f"{safe}.pdf")

@app.get("/terms.html", include_in_schema=False)
def serve_terms():
    return _html("terms.html")

# ── Deploy endpoint ────────────────────────────────────────────────────────────
import subprocess as _subprocess  # noqa: E402

DEPLOY_TOKEN = os.getenv("DEPLOY_TOKEN")
if not DEPLOY_TOKEN:
    log.warning("DEPLOY_TOKEN not set — /internal/deploy endpoint is insecure")

@app.post("/internal/deploy")
async def deploy_endpoint(authorization: str = Header(None)):
    if not DEPLOY_TOKEN:
        raise HTTPException(503, "Deploy endpoint disabled — set DEPLOY_TOKEN env var")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "No token")
    if authorization[7:] != DEPLOY_TOKEN:
        raise HTTPException(403, "Invalid token")
    try:
        r = _subprocess.run(
            ["/root/AI-CHE/scripts/deploy.sh"],
            capture_output=True, text=True, timeout=120
        )
        if r.returncode != 0:
            log.error(f"Deploy failed (exit {r.returncode}): {r.stderr[:500]}")
            return {"status": "error", "message": "Deploy script failed"}
        log.info("Deploy completed successfully")
        return {"status": "ok"}
    except _subprocess.TimeoutExpired:
        log.error("Deploy timed out after 120s")
        return {"status": "timeout"}
    except Exception as e:
        log.error(f"Deploy exception: {e}")
        raise HTTPException(500, "Deploy failed")

# ── Startup ────────────────────────────────────────────────────────────────────
from fastapi import Depends  # noqa: E402

@app.on_event("startup")
async def startup():
    db = SessionLocal()
    try:
        # Seed default pricing, features, and start exchange-rate updater
        await startup_public(db)
    finally:
        db.close()
    # Load API keys from DB into env
    _load_all_apikeys_from_db()
    # Start agent queue
    await init_agent_queue()
    # Start workflow scheduler + IMAP watcher
    from server.scheduler import start_scheduler
    from server.email_imap import start_imap_watcher
    start_scheduler()
    start_imap_watcher()
    log.info("AI Студия Че запущена")
