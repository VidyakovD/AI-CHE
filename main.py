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
from server.routes.webhook import router as webhook_router
from server.routes.widget import router as widget_router

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

models.Base.metadata.create_all(bind=engine)
from server.db import apply_lightweight_migrations  # noqa: E402
apply_lightweight_migrations()

app = FastAPI(title="AI Студия Че")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── CORS ───────────────────────────────────────────────────────────────────────
_raw_origins = os.getenv("ALLOWED_ORIGINS", "")
_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()] if _raw_origins else []
_dev_mode = os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")
if not _origins:
    if _dev_mode:
        log.warning("DEV_MODE: CORS allows all origins — НЕ ВКЛЮЧАЙТЕ В ПРОДЕ")
        _origins = ["*"]
    else:
        raise RuntimeError(
            "ALLOWED_ORIGINS не задан. В проде укажите домены через запятую "
            "(например: https://aiche.ru,https://www.aiche.ru). "
            "Для локальной разработки установите DEV_MODE=true."
        )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=bool(_origins) and _origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from server.security import rate_limit_middleware  # noqa: E402
app.middleware("http")(rate_limit_middleware)


# ── Body size limit (10 MB для JSON-эндпоинтов) + security headers ─────────────
_MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(12 * 1024 * 1024)))  # 12MB — upload до 10MB + оверхед


from fastapi import Request  # noqa: E402

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
