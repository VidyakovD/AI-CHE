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
from server.routes.marketplace import router as marketplace_router
from server.routes.public_api import (
    mgmt_router as api_tokens_router,
    api_router as public_api_router,
)
from server.routes.schedules import router as schedules_router
from server.routes.crm import router as crm_router
from server.routes.mcp import router as mcp_router

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


# ── Fail-fast валидация env ────────────────────────────────────────────────
# В проде раньше пустые YOOKASSA_SHOP_ID / SECRET_KEY загружались как ""
# молча — юзер делал оплату, ЮKassa отвечала 401, мы возвращали 500. Лучше
# не стартовать, чем работать сломанно. Fatal — RuntimeError, warn — log.
def _validate_env():
    is_prod = os.getenv("APP_ENV", "production").lower() == "production"
    is_dev = os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")

    errors: list[str] = []
    warns: list[str] = []

    # JWT_SECRET — обязательный, в проде минимум 32 символа
    jwt_secret = os.getenv("JWT_SECRET", "").strip()
    if not jwt_secret:
        errors.append(
            "JWT_SECRET не задан — без него не работают refresh-токены, "
            "EncryptedString-поля и httpOnly cookie auth."
        )
    elif is_prod and not is_dev and len(jwt_secret) < 32:
        errors.append(
            f"JWT_SECRET слишком короткий ({len(jwt_secret)} симв) — "
            "в проде минимум 32 (HKDF derived ключи + bruteforce resistance)."
        )

    # ЮKassa: обе переменные либо обе пусты. Несимметрично — это ошибка
    # конфигурации (юзер думает что платежи работают, а они отказывают).
    shop_id = os.getenv("YOOKASSA_SHOP_ID", "").strip()
    secret_key = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
    if bool(shop_id) != bool(secret_key):
        errors.append(
            "YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY должны быть заданы вместе "
            "(сейчас одно есть, другое пусто — оплаты будут падать в 401)."
        )
    if is_prod and not is_dev and not shop_id:
        warns.append(
            "YOOKASSA_SHOP_ID/SECRET_KEY пусты в проде — платежи отключены."
        )

    # DATABASE_URL: в проде рекомендуем PostgreSQL (multi-worker safe)
    db_url = os.getenv("DATABASE_URL", "").strip()
    if is_prod and not is_dev:
        if not db_url:
            warns.append(
                "DATABASE_URL не задан — будет SQLite. В проде с 4 worker'ами "
                "это race-conditions, рекомендуется PostgreSQL."
            )
        elif db_url.startswith("sqlite"):
            warns.append(
                "DATABASE_URL=sqlite в проде с multi-worker — рекомендуется PostgreSQL."
            )

    # ALLOWED_ORIGINS уже валидируется ниже при настройке CORS — не дублируем

    # DEPLOY_TOKEN: warn если пусто, чтобы /internal/deploy не открывался без auth
    if is_prod and not os.getenv("DEPLOY_TOKEN", "").strip():
        warns.append(
            "DEPLOY_TOKEN не задан — /internal/deploy будет возвращать 503. "
            "Это безопасное состояние, но CI деплой не будет работать."
        )

    for w in warns:
        log.warning(f"[env-check] {w}")
    if errors:
        msg = "\n  • " + "\n  • ".join(errors)
        raise RuntimeError(
            f"Невалидная конфигурация environment:{msg}\n\n"
            "Почините .env и перезапустите сервис."
        )


_validate_env()


# create_all с защитой от race-condition: при многих uvicorn workers оба
# процесса вызывают create_all одновременно. SQLAlchemy checkfirst=True
# делает SELECT FROM sqlite_master, потом CREATE TABLE — между этим
# происходит гонка и второй worker падает с «table already exists».
# Catch'им и игнорируем — таблица УЖЕ создана первым worker'ом.
try:
    models.Base.metadata.create_all(bind=engine)
except Exception as _e:
    if "already exists" in str(_e).lower():
        log.debug(f"create_all race (table already exists): {_e!s:.120}")
    else:
        raise
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
    # /widget/ удалён — там только GET /widget/{bot_id}.js (CSRF-middleware
    # пропускает GET автоматически), а WebSocket идёт по /ws/widget/ и
    # обходит HTTP-middleware. Если понадобится write-endpoint под /widget/ —
    # явно добавить точный путь, не префикс.
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
    # ПОРЯДОК ВАЖЕН:
    #   1. Если cookie access_token присутствует → ВСЕГДА требуем CSRF.
    #      Не доверяем наличию Authorization header'а как разрешению,
    #      потому что cookie всё равно автоматически отправляется браузером
    #      и используется в `current_user`. Если кто-то расширит CORS на
    #      сторонние домены — Bearer-bypass превратится в полный CSRF-bypass.
    #   2. Иначе если есть Bearer >= "Bearer XXXX" — это чистый API-клиент,
    #      cookie нет → CSRF не нужен (CORS защищает Authorization header).
    #   3. Иначе anon/public endpoint — пропускаем.
    has_cookie_auth = bool(request.cookies.get(ACCESS_COOKIE_NAME))
    auth_header = request.headers.get("authorization", "")
    has_bearer = (auth_header.startswith("Bearer ")
                   and len(auth_header.strip()) > len("Bearer ") + 3)
    if not has_cookie_auth:
        # Нет cookie — либо чистый API-вызов, либо public anon endpoint
        return await call_next(request)
    # Cookie-based auth → требуем CSRF (даже если есть и Bearer)
    _ = has_bearer  # сохранено для потенциальной будущей логики
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
            # Шрифты захостили локально (/fonts/*.woff2) — никаких внешних
            # font CDN не нужно. Google Fonts/Bunny.net убраны из CSP.
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.tailwindcss.com; "
            "font-src 'self' data:; "
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
app.include_router(marketplace_router)
app.include_router(api_tokens_router)
app.include_router(public_api_router)
app.include_router(schedules_router)
app.include_router(crm_router)
app.include_router(mcp_router)

# ── Static files (uploads) ────────────────────────────────────────────────────
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# Локальные шрифты (Inter, Manrope, Material Symbols) — захостили сами,
# чтобы не зависеть от Google Fonts CDN (частично блокируется в РФ).
app.mount("/fonts", StaticFiles(directory="views/fonts"), name="fonts")

# ── Hosted sites: убран StaticFiles mount ─────────────────────────────────
# Раньше mount /sites/hosted → uploads/sites/ позволял прямой доступ к файлам
# по sequential ID (`/sites/hosted/123/index.html`) — обходил sandbox-обёртку
# и токен-проверку из routes/sites.py. Сайты теперь раздаются ТОЛЬКО через
# endpoint `/sites/hosted/{public_token}/{path}` (см. routes/sites.py).
_sites_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "sites")
os.makedirs(_sites_dir, exist_ok=True)

# ── HTML pages ─────────────────────────────────────────────────────────────────
_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "views")

# no-cache headers для HTML чтобы браузер всегда брал свежую версию после деплоя
_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}

def _html(name: str) -> FileResponse:
    return FileResponse(os.path.join(_BASE, name), headers=_NO_CACHE)

@app.get("/fonts.css", include_in_schema=False)
def serve_fonts_css():
    """Единый файл @font-face для всех views/*.html — Golos Text + Material
    Symbols + display-эффект на заголовках. Один источник истины, чтобы
    при смене шрифта менять в одном месте.
    """
    return FileResponse(
        os.path.join(_BASE, "fonts.css"),
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600, must-revalidate"},
    )


@app.get("/icons.js", include_in_schema=False)
def serve_icons():
    """Единый набор векторных иконок — заменяет эмодзи в UI.

    Временно: Cache-Control no-cache, must-revalidate чтобы юзеры быстрее
    получали правки после деплоя (бывшие проблемы с PWA-кэшем).
    """
    return FileResponse(
        os.path.join(_BASE, "icons.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/shared.js", include_in_schema=False)
def serve_shared_js():
    """Общие хелперы для всех HTML: esc/escHtml/fmtRub/aiFetch/humanizeError."""
    return FileResponse(
        os.path.join(_BASE, "shared.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/styles.css", include_in_schema=False)
def serve_styles_css():
    """Скомпилированный Tailwind. Генерируется через `npm run build:css`.
    Подключается как замена CDN-скрипта `cdn.tailwindcss.com` (~100KB JIT).

    Если файл отсутствует — возвращаем 404; HTML тогда всё ещё работает на
    CDN-script-fallback. Это безопасно во время постепенного перехода."""
    path = os.path.join(_BASE, "styles.css")
    if not os.path.exists(path):
        from fastapi.responses import Response
        return Response(status_code=404)
    return FileResponse(
        path,
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=600, must-revalidate"},
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


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Лёгкий health-check для мониторинга/балансировщика. Без БД-запросов
    чтобы не нагружать. Если процесс жив и роутер дошёл сюда — 200 OK."""
    return {"status": "ok"}


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

@app.get("/marketplace.html", include_in_schema=False)
def serve_marketplace():
    """Marketplace отключён (продуктовое решение, 2026-05-10).
    Старые ссылки → редирект на главную. Установленные боты от прошлых
    публикаций продолжают работать как обычные ChatBot."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/", status_code=307)

@app.get("/api.html", include_in_schema=False)
def serve_api_docs():
    return _html("api.html")


def _verify_proposal_pdf_path(p):
    """Helper: безопасный путь к PDF. ValueError при попытке traversal."""
    from pathlib import Path as _P
    base = os.path.dirname(os.path.abspath(__file__))
    uploads_root = _P(base, "uploads").resolve()
    pdf_path = _P(base, p.generated_pdf.lstrip("/")).resolve()
    pdf_path.relative_to(uploads_root)
    if not pdf_path.exists() or not pdf_path.is_file():
        raise FileNotFoundError(p.generated_pdf)
    return pdf_path


@app.get("/p/{public_token}", include_in_schema=False)
def serve_public_proposal(public_token: str):
    """Публичная страница КП — HTML с iframe-превью PDF + блоком
    электронной подписи. Без auth, по токену.

    При первом открытии — отмечает opened_at + crm_stage=opened.
    PDF доступен на /p/{token}/pdf для скачивания.
    """
    from fastapi.responses import JSONResponse
    from server.db import db_session
    from server.models import ProposalProject, ProposalSignature
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
        # Audit-лог только при первом открытии (не спамить)
        if first_open:
            try:
                from server.audit_log import log_action
                log_action("proposal.public_opened", user_id=p.user_id,
                            target_type="proposal", target_id=str(p.id))
            except Exception:
                pass
            # Web Push: клиент открыл КП — уведомление владельцу
            try:
                from server.push import push_to_user as _push
                _push(p.user_id,
                      f"Клиент открыл КП «{p.name}»",
                      f"{p.client_name or p.client_email or 'Клиент'} только что посмотрел документ.",
                      url=f"/proposals.html#proposal-{p.id}")
            except Exception:
                pass
            # Public API webhook: proposal.opened
            try:
                from server.webhooks import dispatch_event
                dispatch_event(p.user_id, "proposal.opened", {
                    "proposal_id": p.id,
                    "name": p.name,
                    "client_name": p.client_name,
                    "client_email": p.client_email,
                    "opened_at": p.opened_at.isoformat() + "Z" if p.opened_at else None,
                })
            except Exception:
                pass
        # Подгружаем существующую подпись (если есть) — чтобы не дать подписать второй раз
        sig = _db.query(ProposalSignature).filter_by(proposal_id=p.id).first()
        sig_dict = {
            "signer_name": sig.signer_name,
            "signer_email": sig.signer_email,
            "signer_position": sig.signer_position,
            "signed_at": sig.signed_at.isoformat() if sig.signed_at else None,
        } if sig else None
        # Готовим HTML-страницу со встроенными данными
        title = (p.name or "Коммерческое предложение")[:120]
        client = (p.client_name or "")[:120]
    return _proposal_public_page(public_token, title, client, sig_dict)


def _proposal_public_page(token: str, title: str, client_name: str,
                            sig: dict | None) -> "HTMLResponse":
    """Рендер публичной страницы КП без auth: PDF iframe + canvas-подпись."""
    import json as _json
    from html import escape as _esc
    from fastapi.responses import HTMLResponse
    sig_block = ""
    if sig:
        signed_at_fmt = sig.get("signed_at", "")
        if signed_at_fmt:
            try:
                from datetime import datetime as _dt
                signed_at_fmt = _dt.fromisoformat(signed_at_fmt.rstrip("Z")).strftime("%d.%m.%Y %H:%M")
            except Exception:
                pass
        sig_block = f"""
        <div class="card signed">
          <div class="signed-icon">✓</div>
          <div>
            <div class="signed-title">Подписано</div>
            <div class="signed-meta">
              <b>{_esc(sig.get('signer_name','') or '')}</b>
              {_esc(sig.get('signer_position') or '')}
              <br>{signed_at_fmt}
            </div>
          </div>
        </div>"""
    else:
        sig_block = """
        <div class="card sign-form">
          <h2>✍️ Электронная подпись</h2>
          <p class="sub">Подпишите документ — займёт 30 секунд. Подпись + время + IP сохраняются как audit-trail (юридически достаточно для B2B).</p>
          <div class="row">
            <input id="signerName" placeholder="ФИО *" required maxlength="200">
            <input id="signerPosition" placeholder="Должность" maxlength="100">
          </div>
          <div class="row">
            <input id="signerEmail" type="email" placeholder="Email" maxlength="200">
            <input id="signerPhone" placeholder="Телефон" maxlength="50">
          </div>
          <label class="canvas-label">Нарисуйте подпись:</label>
          <div class="canvas-wrap">
            <canvas id="sigCanvas" width="700" height="200"></canvas>
            <button onclick="clearSignature()" class="btn-ghost" type="button">Очистить</button>
          </div>
          <label class="checkbox">
            <input type="checkbox" id="signAgree">
            <span>Я согласен с условиями коммерческого предложения и подтверждаю, что моя подпись действительна.</span>
          </label>
          <button onclick="submitSignature()" id="signBtn" class="btn-primary">Подписать документ</button>
          <div id="signResult" class="result-box" style="display:none"></div>
        </div>"""

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{_esc(title)}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;font-family:-apple-system,system-ui,sans-serif;background:#fafaf8;color:#1a1a1a;min-height:100vh}}
.hdr{{background:#fff;border-bottom:1px solid #e5e5e5;padding:14px 20px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:10}}
.hdr h1{{margin:0;font-size:16px;font-weight:600;flex:1}}
.hdr .meta{{font-size:12px;color:#888}}
.hdr .actions{{display:flex;gap:8px}}
.btn-download{{background:#1a1a1a;color:#fff;border:none;border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:5px}}
.btn-download:hover{{background:#333}}
.container{{max-width:900px;margin:0 auto;padding:18px}}
.pdf-box{{background:#fff;border:1px solid #e5e5e5;border-radius:12px;overflow:hidden;margin-bottom:18px;box-shadow:0 2px 12px rgba(0,0,0,0.04)}}
.pdf-box iframe{{display:block;width:100%;height:80vh;min-height:500px;border:0}}
.card{{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:24px;box-shadow:0 2px 12px rgba(0,0,0,0.04)}}
.card h2{{margin:0 0 8px;font-size:18px}}
.card.sign-form .sub{{color:#666;font-size:13px;line-height:1.5;margin:0 0 16px}}
.row{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}}
@media(max-width:520px){{.row{{grid-template-columns:1fr}}}}
.row input{{padding:10px 12px;border:1px solid #d4d4d4;border-radius:8px;font-size:14px;font-family:inherit;width:100%}}
.row input:focus{{outline:none;border-color:#ff8c42;box-shadow:0 0 0 3px rgba(255,140,66,0.1)}}
.canvas-label{{display:block;font-size:12px;color:#666;margin:14px 0 6px;font-weight:600}}
.canvas-wrap{{position:relative;border:2px dashed #d4d4d4;border-radius:10px;padding:8px;background:#fff}}
#sigCanvas{{width:100%;height:200px;display:block;cursor:crosshair;touch-action:none;background:#fafaf8;border-radius:6px}}
.btn-ghost{{position:absolute;top:14px;right:14px;background:rgba(255,255,255,0.95);border:1px solid #d4d4d4;border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer}}
.btn-ghost:hover{{background:#f5f5f5}}
.checkbox{{display:flex;gap:10px;align-items:flex-start;margin:14px 0;font-size:13px;line-height:1.4;cursor:pointer}}
.checkbox input{{margin-top:2px;width:16px;height:16px;flex-shrink:0;cursor:pointer}}
.btn-primary{{background:linear-gradient(135deg,#ff8c42,#ffb347);color:#1a1a1a;border:none;border-radius:10px;padding:13px 28px;font-size:15px;font-weight:700;cursor:pointer;width:100%;transition:opacity .15s}}
.btn-primary:hover{{opacity:.92}}
.btn-primary:disabled{{opacity:.5;cursor:not-allowed}}
.result-box{{margin-top:14px;padding:12px;border-radius:8px;font-size:14px}}
.result-box.success{{background:#e8f5e9;color:#1b5e20;border:1px solid #66bb6a}}
.result-box.error{{background:#ffebee;color:#c62828;border:1px solid #ef9a9a}}
.signed{{display:flex;gap:18px;align-items:center;background:linear-gradient(135deg,#e8f5e9,#fff);border-color:#66bb6a}}
.signed-icon{{width:48px;height:48px;border-radius:50%;background:#4caf50;color:#fff;display:flex;align-items:center;justify-content:center;font-size:26px;flex-shrink:0;font-weight:bold}}
.signed-title{{font-size:18px;font-weight:700;color:#1b5e20}}
.signed-meta{{color:#666;font-size:13px;line-height:1.5;margin-top:4px}}
.footer{{text-align:center;padding:30px 16px 40px;color:#999;font-size:11px}}
.footer a{{color:#ff8c42;text-decoration:none}}
</style>
</head><body>
<div class="hdr">
  <h1>{_esc(title)}</h1>
  {('<div class="meta">для ' + _esc(client_name) + '</div>') if client_name else ''}
  <div class="actions">
    <a href="/p/{_esc(token)}/pdf" class="btn-download" download>↓ PDF</a>
  </div>
</div>
<div class="container">
  <div class="pdf-box">
    <iframe src="/p/{_esc(token)}/pdf#toolbar=0" title="Превью КП"></iframe>
  </div>
  {sig_block}
</div>
<div class="footer">
  Документ создан в <a href="https://aiche.ru" target="_blank">AI Студии Че</a>
</div>
<script>
const TOKEN = {_json.dumps(token)};
// Canvas подпись с поддержкой mouse + touch
(function(){{
  const canvas = document.getElementById('sigCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let drawing = false;
  let hasInk = false;
  // Resize canvas под container width × keep aspect 700:200
  function resize(){{
    const w = canvas.offsetWidth;
    canvas.width = w * 2;  // retina
    canvas.height = (w * 200 / 700) * 2;
    ctx.scale(2, 2);
    ctx.lineWidth = 2.4;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = '#1a1a1a';
    ctx.fillStyle = '#fafaf8';
    ctx.fillRect(0, 0, canvas.width / 2, canvas.height / 2);
  }}
  resize();
  window.addEventListener('resize', resize);
  function getPos(e){{
    const r = canvas.getBoundingClientRect();
    const t = e.touches ? e.touches[0] : e;
    return {{x: t.clientX - r.left, y: t.clientY - r.top}};
  }}
  function start(e){{ e.preventDefault(); drawing = true; const p = getPos(e); ctx.beginPath(); ctx.moveTo(p.x, p.y); }}
  function move(e){{ if(!drawing) return; e.preventDefault(); const p = getPos(e); ctx.lineTo(p.x, p.y); ctx.stroke(); hasInk = true; }}
  function end(e){{ drawing = false; }}
  canvas.addEventListener('mousedown', start);
  canvas.addEventListener('mousemove', move);
  canvas.addEventListener('mouseup', end);
  canvas.addEventListener('mouseleave', end);
  canvas.addEventListener('touchstart', start, {{passive:false}});
  canvas.addEventListener('touchmove', move, {{passive:false}});
  canvas.addEventListener('touchend', end);
  window.clearSignature = function(){{ resize(); hasInk = false; }};
  window._hasSignatureInk = () => hasInk;
  window._getSignatureDataUrl = () => canvas.toDataURL('image/png');
}})();

async function submitSignature(){{
  const name = document.getElementById('signerName').value.trim();
  const pos = document.getElementById('signerPosition').value.trim();
  const email = document.getElementById('signerEmail').value.trim();
  const phone = document.getElementById('signerPhone').value.trim();
  const agree = document.getElementById('signAgree').checked;
  const result = document.getElementById('signResult');
  if (name.length < 2){{ showRes('Укажите ФИО (минимум 2 символа)', 'error'); return; }}
  if (!window._hasSignatureInk()){{ showRes('Нарисуйте подпись на canvas', 'error'); return; }}
  if (!agree){{ showRes('Поставьте галочку согласия', 'error'); return; }}
  const sigData = window._getSignatureDataUrl();
  const btn = document.getElementById('signBtn');
  btn.disabled = true; btn.textContent = 'Подписываю…';
  try {{
    const r = await fetch(`/p/${{encodeURIComponent(TOKEN)}}/sign`, {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{
        signer_name: name, signer_position: pos,
        signer_email: email, signer_phone: phone,
        signature_data: sigData,
      }}),
    }});
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'HTTP ' + r.status);
    showRes('✓ Подпись принята! Уведомление отправлено владельцу. Через 2 секунды страница обновится.', 'success');
    setTimeout(() => window.location.reload(), 2000);
  }} catch(e){{
    showRes('Ошибка: ' + (e.message || 'не удалось подписать'), 'error');
    btn.disabled = false; btn.textContent = 'Подписать документ';
  }}
}}

function showRes(msg, type){{
  const el = document.getElementById('signResult');
  el.textContent = msg;
  el.className = 'result-box ' + type;
  el.style.display = 'block';
}}
</script>
</body></html>""", headers={
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })


@app.get("/p/{public_token}/pdf", include_in_schema=False)
def serve_public_proposal_pdf(public_token: str):
    """Прямой PDF — для скачивания / iframe-превью на /p/{token}."""
    from fastapi.responses import FileResponse, JSONResponse
    from server.db import db_session
    from server.models import ProposalProject
    if not public_token or len(public_token) < 16:
        return JSONResponse({"detail": "Invalid token"}, status_code=404)
    with db_session() as _db:
        p = _db.query(ProposalProject).filter_by(public_token=public_token).first()
        if not p or not p.generated_pdf:
            return JSONResponse({"detail": "КП не найдено или удалено"}, status_code=404)
        try:
            pdf_path = _verify_proposal_pdf_path(p)
        except (ValueError, OSError, FileNotFoundError):
            return JSONResponse({"detail": "PDF файл недоступен"}, status_code=404)
        import re as _re
        safe = _re.sub(r"[^\w\-]", "_", p.name or "proposal")[:40]
        return FileResponse(str(pdf_path), media_type="application/pdf",
                             filename=f"{safe}.pdf")


@app.post("/p/{public_token}/sign", include_in_schema=False)
async def sign_public_proposal(public_token: str, request: "Request"):
    """Принять электронную подпись клиента под КП.

    Body JSON: {signer_name, signer_position?, signer_email?, signer_phone?, signature_data}
    signature_data — data-URL от canvas (data:image/png;base64,...).
    Идемпотентно: если уже подписано → 409.

    После сохранения:
      - audit-log proposal.signed
      - push владельцу
      - email владельцу (если SMTP настроен)
      - webhook proposal.signed диспатчится в SaaS-интеграции
    """
    import hashlib as _hashlib
    import json as _json
    from fastapi.responses import JSONResponse
    from sqlalchemy.exc import IntegrityError as _IntegrityError
    from server.db import db_session
    from server.models import ProposalProject, ProposalSignature
    from datetime import datetime as _dt
    if not public_token or len(public_token) < 16:
        return JSONResponse({"detail": "Invalid token"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    name = (body.get("signer_name") or "").strip()[:200]
    if len(name) < 2:
        return JSONResponse({"detail": "ФИО минимум 2 символа"}, status_code=400)
    sig_data = (body.get("signature_data") or "").strip()
    if not sig_data.startswith("data:image/") or len(sig_data) < 200:
        return JSONResponse({"detail": "Подпись отсутствует или некорректна"}, status_code=400)
    if len(sig_data) > 2_000_000:  # 2 МБ data-URL — overkill для подписи
        return JSONResponse({"detail": "Подпись слишком большая"}, status_code=413)
    email = (body.get("signer_email") or "").strip()[:200] or None
    phone = (body.get("signer_phone") or "").strip()[:50] or None
    position = (body.get("signer_position") or "").strip()[:100] or None
    # IP юзера (через nginx proxy_pass)
    ip = (request.headers.get("x-real-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "")
            or "unknown")[:64]
    ua = (request.headers.get("user-agent") or "")[:500]
    with db_session() as _db:
        p = _db.query(ProposalProject).filter_by(public_token=public_token).first()
        if not p:
            return JSONResponse({"detail": "КП не найдено"}, status_code=404)
        existing = _db.query(ProposalSignature).filter_by(proposal_id=p.id).first()
        if existing:
            return JSONResponse({
                "detail": "Документ уже подписан",
                "signer_name": existing.signer_name,
                "signed_at": existing.signed_at.isoformat() + "Z" if existing.signed_at else None,
            }, status_code=409)
        signed_at = _dt.utcnow()
        # Hash для верификации: невозможно подменить без обнаружения
        hash_src = "|".join([
            str(p.id), name, email or "", str(signed_at.timestamp()),
            sig_data[:200], ip,
        ])
        sig_hash = _hashlib.sha256(hash_src.encode("utf-8")).hexdigest()
        sig = ProposalSignature(
            proposal_id=p.id,
            signer_name=name,
            signer_email=email,
            signer_phone=phone,
            signer_position=position,
            signature_data=sig_data,
            ip=ip,
            user_agent=ua,
            sig_hash=sig_hash,
            signed_at=signed_at,
        )
        _db.add(sig)
        # Auto-CRM transition
        if (p.crm_stage or "new") in ("new", "sent", "opened"):
            p.crm_stage = "won"
        # IntegrityError ловит race: два POST'а на /sign одновременно с разных
        # воркеров — оба прошли SELECT-проверку existing=None, оба идут на INSERT.
        # UNIQUE constraint на proposal_id защищает на DB-level. Превращаем
        # IntegrityError в 409 (как для любого "уже подписано").
        try:
            _db.commit()
        except _IntegrityError:
            _db.rollback()
            return JSONResponse({
                "detail": "Документ уже подписан",
            }, status_code=409)
        owner_id = p.user_id
        proposal_name = p.name
        client_label = p.client_name or p.client_email or "Клиент"

    # Audit log
    try:
        from server.audit_log import log_action
        log_action("proposal.signed", user_id=owner_id,
                    target_type="proposal", target_id=str(p.id),
                    details={"signer_name": name, "signer_email": email, "ip": ip,
                             "sig_hash": sig_hash[:16]})
    except Exception:
        pass
    # Push владельцу
    try:
        from server.push import push_to_user as _push
        _push(owner_id,
              f"✓ Подписано: «{proposal_name}»",
              f"{name}{', '+position if position else ''} только что подписал документ.",
              url=f"/proposals.html#proposal-{p.id}")
    except Exception:
        pass
    # Email владельцу
    try:
        from server.db import db_session as _ds
        from server.models import User as _U
        from server.email_service import _send, _base_template
        with _ds() as _db2:
            owner = _db2.query(_U).filter_by(id=owner_id).first()
            owner_email = owner.email if owner else None
        if owner_email:
            email_html = _base_template(
                f"Подписано: {proposal_name[:60]}",
                f'<p style="color:rgba(199,196,215,0.85);line-height:1.6">'
                f'<b>{name}</b>{(", "+position) if position else ""} только что подписал ваше КП.</p>'
                f'<p style="color:rgba(199,196,215,0.7);font-size:13px">'
                f'IP: {ip}<br>Время: {signed_at.strftime("%d.%m.%Y %H:%M")} UTC<br>'
                f'Hash: {sig_hash[:16]}…</p>'
            )
            _send(owner_email, f"✓ Подписано: «{proposal_name[:40]}»", email_html)
    except Exception:
        pass
    # Webhook (Public API подписчики)
    try:
        from server.webhooks import dispatch_event
        dispatch_event(owner_id, "proposal.signed", {
            "proposal_id": p.id,
            "name": proposal_name,
            "signer_name": name,
            "signer_email": email,
            "signer_position": position,
            "signed_at": signed_at.isoformat() + "Z",
            "sig_hash": sig_hash,
            "ip": ip,
        })
    except Exception:
        pass
    # CRM-интеграция: подписавший клиент = лид. UX в /api.html → CRM обещает
    # «лид в Bitrix24/amoCRM», без этого вызова туда не приходило ничего.
    try:
        from server.crm import dispatch_record_to_crm
        dispatch_record_to_crm(owner_id, {
            "customer_name": name,
            "customer_email": email,
            "customer_phone": phone,
            "bot_name": f"КП «{proposal_name[:50]}»",
            "platform": "proposal_signed",
            "record_type": "lead",
            "comment": f"Подписано КП «{proposal_name}». "
                       f"Подписант: {name}"
                       + (f", {position}" if position else "")
                       + f". IP={ip}, hash={sig_hash[:16]}",
        })
    except Exception as e:
        log.warning(f"[proposal-sign] CRM dispatch failed: {type(e).__name__}: {e}")
    return {"status": "signed", "signer_name": name,
            "signed_at": signed_at.isoformat() + "Z",
            "sig_hash": sig_hash[:16]}

@app.get("/s/{public_token}", include_in_schema=False)
def serve_public_solution(public_token: str):
    """Публичная ссылка на результат бизнес-решения (orchestra). Без auth.
    Отдаёт PDF если есть, иначе плейн-текст итогового markdown."""
    from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
    from server.db import db_session
    from server.models import SolutionRun, Solution
    if (not public_token or len(public_token) < 16 or len(public_token) > 64
            or not all(c.isalnum() or c in "-_" for c in public_token)):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    with db_session() as _db:
        run = _db.query(SolutionRun).filter_by(public_token=public_token).first()
        if not run or run.status != "done":
            return JSONResponse({"detail": "Решение не найдено или не завершено"}, status_code=404)
        sol = _db.query(Solution).filter_by(id=run.solution_id).first()
        title = (sol.title if sol else "Бизнес-решение")
        pdf_path_rel = run.pdf_path
        final_md = run.final_output or ""
    # PDF приоритетнее
    if pdf_path_rel:
        from pathlib import Path as _P
        base = os.path.dirname(os.path.abspath(__file__))
        uploads_root = _P(base, "uploads").resolve()
        try:
            pdf_abs = _P(base, pdf_path_rel.lstrip("/")).resolve()
            pdf_abs.relative_to(uploads_root)
            if pdf_abs.exists() and pdf_abs.is_file():
                import re as _re
                safe = _re.sub(r"[^\w\-]", "_", title)[:40]
                return FileResponse(str(pdf_abs), media_type="application/pdf",
                                     filename=f"{safe}.pdf")
        except (ValueError, OSError):
            pass
    # Fallback: markdown как plain text
    return PlainTextResponse(final_md or "Результат недоступен",
                              headers={"Cache-Control": "no-store"})


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
    # Сравнение через compare_digest — защита от timing-атаки на побайтовый
    # подбор DEPLOY_TOKEN по network-латентности (за токеном — shell от root).
    import hmac as _hmac
    if not _hmac.compare_digest(authorization[7:], DEPLOY_TOKEN):
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
    # In-memory cache sweepers (idempotency in /message + /assistant/ask)
    from server.routes.chat import _start_idempotency_sweeper
    from server.routes.assistant import _start_assistant_sweeper
    _start_idempotency_sweeper()
    _start_assistant_sweeper()
    log.info("AI Студия Че запущена")
