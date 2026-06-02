import os, random, time, base64, httpx, logging
from openai import OpenAI
import anthropic as AnthropicSDK
from server.privacy_guard import PrivacyGuard

# Anthropic SDK по умолчанию ставит httpx-timeout 60 сек, но Claude Sonnet
# с max_tokens=16000 + большой prompt легко уходит за 90 сек. Для генерации
# сайта особенно важно — ждём до 10 минут (override через env при необходимости).
_ANTHROPIC_TIMEOUT = float(os.getenv("ANTHROPIC_TIMEOUT_SEC", "600"))
from dotenv import load_dotenv
load_dotenv()
log = logging.getLogger(__name__)

# ── helpers ──────────────────────────────────────────────────────────────────

_api_key_cache: dict[str, tuple[float, list[str]]] = {}
_API_KEY_CACHE_TTL = 60  # секунд

_ENV_MAP = {
    "openai":     "OPENAI_API_KEYS",
    "anthropic":  "ANTHROPIC_API_KEYS",
    "gemini":     "GOOGLE_API_KEYS",
    "google":     "GOOGLE_API_KEYS",
    "nanobanana": "GOOGLE_API_KEYS",
    "veo":        "GOOGLE_API_KEYS",
    "grok":       "GROK_API_KEYS",
    "perplexity": "PERPLEXITY_API_KEYS",
    "kling":      "KLING_API_KEYS",
}


def _get_api_keys(provider: str, sep=","):
    """Читает ключи сначала из БД, fallback на env. Кэш 60 сек."""
    now = time.time()
    cached = _api_key_cache.get(provider)
    if cached and (now - cached[0]) < _API_KEY_CACHE_TTL:
        return list(cached[1])

    from server.db import db_session
    from server.models import ApiKey
    # Через контекст-менеджер: rollback при ошибке + гарантированный close.
    with db_session() as db:
        if provider == "kling":
            rows = db.query(ApiKey).filter_by(provider="kling").all()
            result = [r.key_value.strip() for r in rows if r.key_value.strip()]
        else:
            rows = db.query(ApiKey).filter_by(provider=provider).all()
            result = []
            for r in rows:
                result.extend(k.strip() for k in r.key_value.split(sep) if k.strip())

        # Fallback: если в БД нет — читаем из env
        if not result:
            env_var = _ENV_MAP.get(provider)
            if env_var:
                env_val = os.getenv(env_var, "")
                if env_val:
                    if provider == "kling":
                        result = [k.strip() for k in env_val.split(";;") if k.strip()]
                    else:
                        result = [k.strip() for k in env_val.split(sep) if k.strip()]

        _api_key_cache[provider] = (now, result)
        return list(result)


# Регулярки для маскирования секретов в логах/нотификациях.
# AI-провайдеры часто бросают exception с полным URL запроса (включая
# api_key в query) или с заголовком Authorization. Не должно попадать в логи.
import re as _re
_SECRET_PATTERNS = [
    (_re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),               r"sk-***"),
    (_re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", _re.I),   r"\1***"),
    (_re.compile(r"(Authorization[:=]\s*)[^\s,;]+", _re.I), r"\1***"),
    (_re.compile(r"(api[_\-]?key[:=]\s*)[^\s,;&]+", _re.I), r"\1***"),
    # Прокси-URL с креденшалами:  http://user:pass@host:port/...
    (_re.compile(r"(https?://)([^:/@\s]+):([^@\s]+)@"),     r"\1***:***@"),
    # Google API key (AIza...)
    (_re.compile(r"(AIza[A-Za-z0-9_\-]{30,})"),              r"AIza***"),
    # query-параметр key=...&
    (_re.compile(r"([?&]key=)[^&\s]+"),                     r"\1***"),
    # query-параметр access_token=... (MAX API legacy, попадал в logs)
    (_re.compile(r"([?&]access_token=)[^&\s]+"),            r"\1***"),
    # query-параметр token=... (общий случай)
    (_re.compile(r"([?&]token=)[^&\s]+"),                   r"\1***"),
]


def _sanitize_error(msg) -> str:
    """Удаляет секреты из текста ошибки/сообщения перед логированием."""
    s = str(msg)
    for pat, repl in _SECRET_PATTERNS:
        s = pat.sub(repl, s)
    return s[:1500]


class _SecretFilter(logging.Filter):
    """
    Logging-фильтр: пропускает каждый record.msg / args через _sanitize_error.
    Подключается к логгеру `server.ai` чтобы все f-string'и с exception-ами
    автоматически чистились от API-ключей и proxy creds.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                # Сначала отрендерим сообщение с args, потом санитизируем —
                # иначе паттерны ошибок в args не отловятся.
                if record.args:
                    try:
                        rendered = record.msg % record.args
                    except Exception:
                        rendered = record.msg
                    record.msg = _sanitize_error(rendered)
                    record.args = ()
                else:
                    record.msg = _sanitize_error(record.msg)
        except Exception:
            pass
        return True


# Подключаем фильтр на собственный логгер модуля.
log.addFilter(_SecretFilter())

# ВАЖНО: httpx сам по INFO-уровню логирует полный URL запроса (включая
# ?key=... для Google AI Studio и Authorization headers — последние
# обычно не в URL, но Google API key — да). Без фильтра ключ вида
# AIza... попадает в journalctl и виден любому с доступом к серверу.
# Навешиваем тот же фильтр на корневой httpx-логгер.
for _ext_logger in ("httpx", "httpcore", "openai", "anthropic"):
    try:
        logging.getLogger(_ext_logger).addFilter(_SecretFilter())
    except Exception:
        pass


def invalidate_api_key_cache(provider: str = None):
    """Сбросить кэш ключей (вызывать при добавлении/удалении ключей)."""
    if provider:
        _api_key_cache.pop(provider, None)
    else:
        _api_key_cache.clear()

def _shuffle(lst): random.shuffle(lst); return lst


# ── Универсальный wrapper «попробовать каждый ключ» ─────────────────────────
# Раньше каждый *_response функция дублировал паттерн:
#   keys = _shuffle(_get_api_keys(provider))
#   if not keys: _notify_admin(...); return {"type":"text","content":"...недоступен..."}
#   for key in keys:
#     try: ... return result
#     except Exception as e: log.warning(...); continue
#   _notify_admin("все ключи исчерпаны")
#   return {"type":"text","content":"...недоступен..."}
# Теперь вынесено сюда. Используется в новых провайдерах + постепенно мигрируем
# существующие (риск-оф-зрения: чтобы не сломать прод за один проход).

_FALLBACK_TEXT = "Сервис временно недоступен. Повторите попытку позже…"


def _fallback_response() -> dict:
    """Стандартный ответ когда AI-провайдер недоступен (все ключи в ауте)."""
    return {"type": "text", "content": _FALLBACK_TEXT}


def try_with_keys(provider: str, call_fn, *, on_no_keys: str | None = None):
    """Прогоняет call_fn(key) по всем ключам провайдера, возвращает первый
    успешный результат. При полном провале — _notify_admin + None (caller
    должен вернуть _fallback_response()).

    Не пишет fallback-ответ сам — провайдеры могут хотеть кастомный stub
    (например, openai_image добавляет «Опишите что нарисовать»).
    """
    keys = _shuffle(_get_api_keys(provider))
    if not keys:
        _notify_admin(on_no_keys or f"{provider}: API keys пуст")
        return None
    last_err: Exception | None = None
    for key in keys:
        try:
            return call_fn(key)
        except Exception as e:
            last_err = e
            tail = key[-6:] if len(key) >= 6 else "***"
            log.warning(f"[{provider}] key=...{tail} error: {_sanitize_error(e)}")
            continue
    _notify_admin(f"{provider}: все ключи исчерпаны: {_sanitize_error(last_err)}")
    return None


def _notify_admin(error_msg: str, context: dict | None = None):
    """Отправляет ошибку в Telegram админу + в ERROR_WEBHOOK если настроен.

    Сообщение проходит _sanitize_error чтобы не утекли API-ключи / proxy creds
    в Telegram-чат админа (история чата может быть скомпрометирована).
    """
    error_msg = _sanitize_error(error_msg)
    # 1. Custom webhook (для интеграции с внешним error-handler)
    err_hook = os.getenv("ERROR_WEBHOOK_URL")
    if err_hook:
        try:
            httpx.post(err_hook, json={
                "source": "aiche",
                "error": error_msg,
                "context": context or {},
                "ts": int(time.time()),
            }, timeout=5)
        except Exception:
            pass

    # 2. Telegram
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_ADMIN_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        text = f"⚠️ AI-CHE Error\n\nОшибка: {error_msg}"
        httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                   timeout=10)
    except Exception:
        pass

# ── token cost map (КОПЕЙКИ за запрос — fallback если нет model_pricing записи) ─
# Для chat-моделей основной расчёт идёт по input/output токенам в model_pricing.
# Здесь — fallback и фикс-цены для media (картинки/видео).
TOKEN_COST = {
    "gpt-4o-mini":              500,   # ≈ 5 ₽ fallback за запрос
    "gpt-4o":                  1000,
    "claude-haiku-4-5-20251001": 400,
    "claude-sonnet-4-6":       1200,
    "claude-opus-4-6":         3000,
    "claude-opus-4-20250514":  3000,
    "claude-opus-4-1-20250805": 3000,
    # Perplexity sonar models (старые sonar-small-chat / sonar-large-chat
    # сняты с поддержки — используем актуальные имена sonar / sonar-pro).
    "sonar":                    300,
    "sonar-pro":                800,
    "sonar-reasoning-pro":     1200,
    # Aliases для legacy-записей, если где-то ещё остались в БД pricing
    "sonar-small-chat":         300,
    "sonar-large-chat":         800,
    "grok-3-mini":              300,
    "grok-3":                   800,
    # ── Картинки (фикс-цена за 1 картинку, в копейках) ──────────────────────
    # ВАЖНО: эти цены посчитаны под HD/high quality (по умолчанию ai.py теперь
    # ставит quality=hd для DALL-E и quality=high для gpt-image-1 — чтобы
    # картинки реально были чёткие, а не «говно»).
    "dall-e-3":                3000,   # 30 ₽ — HD себест $0.08 ≈ 7.2 ₽ (margin ≈4×)
    "gpt-image-1":             6000,   # 60 ₽ — high себест $0.19 ≈ 17 ₽ (margin ≈3.5×)
    # Imagen 4: себест $0.02-$0.06 → продаём с маржой 4-5×.
    # nano-v1 теперь алиас = imagen-4-ultra (был fast). Поднята цена с 10→25 ₽.
    "nano-v1":                 2500,                                  # alias = imagen-4-ultra
    "imagen-4.0-fast-generate-001":   1000,   # 10 ₽ (себест $0.02 ≈ 1.8₽)
    "imagen-4.0-generate-001":        1500,   # 15 ₽ (себест $0.04 ≈ 3.6₽)
    "imagen-4.0-ultra-generate-001":  2500,   # 25 ₽ (себест $0.06 ≈ 5.4₽)
    # ── Видео (фикс-цена за 1 ролик ~5 сек, в копейках) ─────────────────────
    # Veo себест: $0.30-$0.75 за 5-сек ролик в зависимости от качества.
    "veo-3":                          30000,  # 300 ₽ (legacy alias = veo-3 fast)
    "veo-2.0-generate-001":           20000,  # 200 ₽ (себест ~$0.30 = 27₽; маржа 7×)
    "veo-3.0-fast-generate-001":      30000,  # 300 ₽ (себест ~$0.40 = 36₽; маржа 8×)
    "veo-3.0-generate-001":           50000,  # 500 ₽ (себест ~$0.60 + аудио = 60₽; маржа 8×)
    "veo-3.1-fast-generate-preview":  40000,  # 400 ₽ (preview, чуть дороже fast)
    "veo-3.1-generate-preview":       60000,  # 600 ₽ (лучшее качество)
    # ── Kling AI (text2video / image2video / motion / avatar) ──────────────
    # Себест Kling published API rates × ×3 margin × округление до 10 ₽.
    # std mode = 5-сек 720p; pro = 1080p; v3+ — 1080p по дефолту.
    "kling-v1":                 6000,   # 60 ₽  (легаси, std)
    "kling-v1-5":               9000,   # 90 ₽  (legacy v1.5)
    "kling-v1-6":              12000,   # 120 ₽ (v1.6 std — баланс цена/качество)
    "kling-v1-6-pro":          18000,   # 180 ₽ (v1.6 pro 1080p)
    # Kling 2.0 — резкое улучшение качества:
    "kling-v2-master":         15000,   # 150 ₽ (v2.0 std)
    "kling-v2-pro":            30000,   # 300 ₽ (v2.0 pro)
    # Kling 2.1 — быстрый/дешёвый вариант:
    "kling-v2-1-master":       10000,   # 100 ₽ (v2.1 std fast)
    # Kling v3.0 — топ (motion_control использует это по умолчанию):
    "kling-v3-0":              24000,   # 240 ₽ (v3.0 std — лучшее качество для motion)
}

def get_token_cost(model: str) -> int:
    return TOKEN_COST.get(model, 50)


# ── image helper: path → base64 ──────────────────────────────────────────────

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _file_to_base64(file_url: str) -> tuple[str, str]:
    """Read any file from disk by /uploads/... path, return (base64_data, media_type)."""
    import mimetypes
    local_path = os.path.join(_BASE_DIR, file_url.lstrip("/"))
    try:
        with open(local_path, "rb") as f:
            data = f.read()
        mime = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        return base64.b64encode(data).decode(), mime
    except Exception as e:
        raise RuntimeError(f"Failed to read file: {e}")


# backward compat
_image_to_base64 = _file_to_base64


# ── OpenAI ────────────────────────────────────────────────────────────────────

def openai_response(model: str, messages: list, extra: dict = None,
                    user_key: str = None) -> dict:
    keys = [user_key] if user_key else _shuffle(_get_api_keys("openai"))
    if not keys:
        _notify_admin("OpenAI: OPENAI_API_KEYS пуст")
        return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}

    formatted = []
    for m in messages:
        content = m["content"]

        if isinstance(content, dict) and "file_url" in content:
            parts = []
            if content.get("text"):
                parts.append({"type": "text", "text": content["text"]})
            # Картинка → base64, не URL с локального сервера
            file_url = content["file_url"]
            try:
                b64, mime = _image_to_base64(file_url)
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"}
                })
            except Exception as e:
                parts.append({"type": "text", "text": f"[Не удалось загрузить изображение: {e}]"})
            formatted.append({"role": m["role"], "content": parts})

        elif isinstance(content, str) and content.startswith("/uploads/"):
            try:
                b64, mime = _image_to_base64(content)
                formatted.append({"role": m["role"], "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                ]})
            except Exception as e:
                formatted.append({"role": m["role"], "content": [{"type": "text", "text": str(e)}]})
        else:
            formatted.append({"role": m["role"], "content": [
                {"type": "text", "text": content if isinstance(content, str) else str(content)}
            ]})

    last_error = None
    for key in keys:
        try:
            client = OpenAI(api_key=key, timeout=90, **_openai_client_kwargs("openai"))
            for attempt in range(2):
                try:
                    resp = client.chat.completions.create(model=model, messages=formatted)
                    usage = getattr(resp, "usage", None)
                    return {
                        "type": "text",
                        "content": resp.choices[0].message.content,
                        "input_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                        "output_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
                    }
                except Exception as e:
                    err = str(e).lower()
                    if "401" in err or "invalid" in err:
                        raise  # невалидный ключ — outer except поймает, попробует следующий
                    if attempt == 1:
                        raise
                    time.sleep(1)
        except Exception as e:
            last_error = e
            continue
    raise Exception(f"OpenAI failed: {last_error}")


# ── Claude / Anthropic ────────────────────────────────────────────────────────


def _kling_jwt_token(access_key: str, secret_key: str) -> str:
    """Generate JWT token for Kling API auth."""
    import time, jwt
    return jwt.encode(
        {"iss": access_key, "exp": int(time.time()) + 1800, "nbf": int(time.time()) - 5},
        secret_key,
        headers={"alg": "HS256", "typ": "JWT"}
    )


def _get_kling_jwt() -> str | None:
    """Get a fresh JWT token from available Kling key pairs."""
    raw_keys = _get_api_keys("kling")
    for pair in raw_keys:
        if "," in pair:
            ak, sk = pair.split(",", 1)
            try:
                return _kling_jwt_token(ak.strip(), sk.strip())
            except Exception:
                continue
    return None


def kling_response(model: str, messages: list, extra: dict = None) -> dict:
    """
    Полная реализация Kling API с JWT auth.
    generation_mode: text2video | image2video | image2video_frames | motion_control | avatar
    KLING_API_KEYS format: ak_XXXXX,sk_YYYYY  (access_key,secret_key pairs, comma-separated for multiple)
    """
    extra = extra or {}
    log.info(f"[Kling] model={model}, extra={extra}")

    token = _get_kling_jwt()
    if not token:
        log.error("[Kling] KLING_API_KEYS пуст или невалиден")
        _notify_admin("Kling: KLING_API_KEYS пуст или невалиден")
        return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}
    log.info("[Kling] JWT сгенерирован успешно")

    KLING_MODEL_MAP = {
        "kling":         "kling-v1",
        "kling-pro":     "kling-v1-6",       # legacy alias
        "kling-1-6":     "kling-v1-6",
        "kling-1-6-pro": "kling-v1-6-pro",
        "kling-2":       "kling-v2-master",
        "kling-2-pro":   "kling-v2-pro",
        "kling-2-1":     "kling-v2-1-master",
        "kling-3":       "kling-v3-0",
    }
    prompt    = extra.get("prompt") or _last_text(messages) or ""
    neg       = extra.get("negative_prompt", "")
    aspect    = extra.get("aspect_ratio", "16:9")
    duration  = int(extra.get("duration", 5))
    mode      = extra.get("mode", "std")
    cfg       = float(extra.get("cfg_scale", 0.5))
    gen_mode  = extra.get("generation_mode", "text2video")
    api_model = KLING_MODEL_MAP.get(model, "kling-v1-6")

    log.info(f"[Kling] prompt={prompt[:50]}... gen_mode={gen_mode} model={api_model}")

    # Camera control
    cam_type  = extra.get("camera_type", "")
    cam_val   = float(extra.get("camera_value", 0))
    camera    = {"type": cam_type, cam_type: cam_val} if cam_type and cam_val != 0 else None

    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = "https://api.klingai.com/v1"

    try:
        if gen_mode == "text2video":
            payload = {"model_name": api_model, "prompt": prompt, "negative_prompt": neg,
                       "aspect_ratio": aspect, "duration": duration, "mode": mode, "cfg_scale": cfg}
            if camera:
                payload["camera_control"] = {"type": "preset", "config": camera}
            if extra.get("native_audio"):
                payload["audio_generation"] = True
            resp = httpx.post(f"{base}/videos/text2video", json=payload, headers=h, timeout=60)

        elif gen_mode == "image2video":
            if not extra.get("image_url"):
                return {"type": "text", "content": "[Kling] Загрузите стартовое изображение"}
            payload = {"model_name": api_model, "image": extra["image_url"], "prompt": prompt,
                       "negative_prompt": neg, "duration": duration, "mode": mode, "cfg_scale": cfg}
            if camera:
                payload["camera_control"] = {"type": "preset", "config": camera}
            resp = httpx.post(f"{base}/videos/image2video-frames", json=payload, headers=h, timeout=60)

        elif gen_mode == "image2video_frames":
            if not extra.get("image_url"):
                return {"type": "text", "content": "[Kling] Загрузите стартовое изображение"}
            payload = {"model_name": api_model, "image": extra["image_url"], "prompt": prompt,
                       "duration": duration, "mode": "pro", "cfg_scale": cfg}
            if extra.get("tail_image_url"):
                payload["image_tail"] = extra["tail_image_url"]
            resp = httpx.post(f"{base}/videos/image2video-frames", json=payload, headers=h, timeout=60)

        elif gen_mode == "motion_control":
            if not extra.get("image_url"):
                return {"type": "text", "content": "[Kling] Загрузите фото с чётко видимой позой"}
            if not extra.get("motion_url"):
                return {"type": "text", "content": "[Kling] Загрузите видео-референс движения (3–30 сек)"}
            payload = {"model_name": "kling-v3-0", "imageUrl": extra["image_url"],
                       "motionUrl": extra["motion_url"], "prompt": prompt,
                       "keepAudio": bool(extra.get("keep_audio", False)), "mode": mode}
            resp = httpx.post(f"{base}/videos/motion-create", json=payload, headers=h, timeout=60)

        elif gen_mode == "avatar":
            avatar_img  = extra.get("avatar_image_url")
            avatar_text = extra.get("avatar_text", prompt)
            if not avatar_img:
                return {"type": "text", "content": "[Kling] Загрузите фото лица для аватара"}
            if not avatar_text:
                return {"type": "text", "content": "[Kling] Введите текст для озвучки"}
            # Шаг 1: создать аватар
            av = httpx.post(f"{base}/avatars",
                            json={"image_url": avatar_img, "name": "temp_avatar"},
                            headers=h, timeout=60).json()
            avatar_id = (av.get("data") or {}).get("avatar_id")
            if not avatar_id:
                return {"type": "text", "content": f"[Kling Avatar] Ошибка: {av}"}
            # Шаг 2: генерировать видео
            vid_pl = {"avatar_id": avatar_id, "text": avatar_text, "mode": mode}
            if extra.get("avatar_voice"):
                vid_pl["voice_id"] = extra["avatar_voice"]
            resp = httpx.post(f"{base}/avatars/video", json=vid_pl, headers=h, timeout=60)

        else:
            return {"type": "text", "content": f"[Kling] Неизвестный режим: {gen_mode}"}

        resp.raise_for_status()
        data = resp.json()
        task_id = (data.get("task_id") or
                   (data.get("data") or {}).get("task_id") or
                   (data.get("task") or {}).get("id"))
        if task_id:
            return {"type": "video_task",
                    "content": f"✅ Задача создана (режим: {gen_mode})\nID: {task_id}",
                    "task_id": str(task_id)}
        return {"type": "text", "content": str(data)}

    except Exception as e:
        log.error(f"[Kling] Ошибка: {type(e).__name__}: {e}")
        _notify_admin(f"Kling ошибка: {e}")
        return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}


# ── Google Veo (видео-генерация) ────────────────────────────────────────────
# Использует Generative Language API (predictLongRunning + операция).
# Через прокси — Google AI Studio блокирует RU-сегменты по ASN.
# Veo 3.0-fast иногда отвечает 503 «Deadline expired» (квота). Fallback:
# по очереди пробуем veo-3.1-fast → veo-3.0 → veo-2.0.

# Маппинг variant из UI → точное имя модели в Google API.
# Каждый вариант — список fallback'ов: если первая занята квотой/503, пробуем
# следующую. Это критично для Veo 3.0 fast — оно регулярно «Deadline expired».
_VEO_MODELS = {
    # Дефолт (когда model="veo-3" из MODEL_REGISTRY и нет model_variant)
    "veo-3":      ["veo-3.0-fast-generate-001", "veo-3.1-fast-generate-preview", "veo-3.0-generate-001", "veo-2.0-generate-001"],
    # UI-варианты:
    "veo-3-fast": ["veo-3.0-fast-generate-001", "veo-3.1-fast-generate-preview", "veo-2.0-generate-001"],
    "veo-3-1":    ["veo-3.1-fast-generate-preview", "veo-3.1-generate-preview", "veo-3.0-generate-001"],
    "veo-2":      ["veo-2.0-generate-001"],
}


def _save_video_bytes(data: bytes, ext: str = "mp4") -> str:
    """Сохраняет видео в /uploads/ и возвращает URL."""
    import os as _os, uuid as _uuid
    project_root = _os.path.dirname(_BASE_DIR)
    upload_dir = _os.path.join(project_root, "uploads")
    _os.makedirs(upload_dir, exist_ok=True)
    fid = f"vid_{_uuid.uuid4().hex[:12]}.{ext}"
    path = _os.path.join(upload_dir, fid)
    with open(path, "wb") as f:
        f.write(data)
    return f"/uploads/{fid}"


def veo_response(model: str, messages: list, extra: dict = None) -> dict:
    """Google Veo — генерация видео. Через Generative Language API + прокси.

    extra params:
      prompt, aspect_ratio (16:9 | 9:16), sample_count (1)
    Возвращает {type:'video', url, content} при успехе или текст при ошибке.

    ВАЖНО: операция асинхронная (predictLongRunning), polling до 5 минут.
    Если хочется неблокирующего UX — лучше через очередь (server.agent_runner),
    но пока для простоты ждём прямо здесь.
    """
    import time
    extra = extra or {}
    keys = _shuffle(_get_api_keys("google"))
    if not keys:
        _notify_admin("Veo: GOOGLE_API_KEYS пуст")
        return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}

    prompt = extra.get("prompt") or _last_text(messages) or ""
    if not prompt:
        return {"type": "text", "content": "Опишите что снять (хотя бы пару слов)."}

    ar = extra.get("aspect_ratio") or "16:9"
    if ar not in ("16:9", "9:16"):
        ar = "16:9"

    # variant из UI приоритетнее MODEL_REGISTRY id
    variant = (extra.get("model_variant") or "").strip()
    candidates = _VEO_MODELS.get(variant) or _VEO_MODELS.get(model) or _VEO_MODELS["veo-3"]

    # Доп. параметры
    neg = (extra.get("negative_prompt") or "").strip()
    person_gen = (extra.get("person_generation") or "").strip()
    generate_audio = bool(extra.get("generate_audio", True))
    enhance_prompt = bool(extra.get("enhance_prompt", True))
    seed = extra.get("seed")
    try:
        seed = int(seed) if seed not in (None, "", "null") else None
    except (TypeError, ValueError):
        seed = None

    # Image-to-video: первый кадр. С фронта может прийти как абсолютный URL
    # (https://aiche.ru/uploads/img_xxx.png) — берём только path-часть после
    # /uploads/ и читаем локальный файл.
    image_url = (extra.get("image_url") or extra.get("file_url") or "").strip()
    image_payload = None
    if image_url:
        try:
            import base64 as _b64, mimetypes, urllib.parse as _up
            # Извлекаем «/uploads/...» из любого URL
            parsed = _up.urlparse(image_url)
            rel_path = parsed.path or image_url
            project_root = os.path.dirname(_BASE_DIR)
            local_path = os.path.join(project_root, rel_path.lstrip("/"))
            if os.path.exists(local_path):
                with open(local_path, "rb") as f:
                    img_b64 = _b64.b64encode(f.read()).decode("ascii")
                mime = mimetypes.guess_type(local_path)[0] or "image/png"
                image_payload = {"bytesBase64Encoded": img_b64, "mimeType": mime}
                log.info(f"[Veo] image2video mode: {local_path} ({mime})")
            else:
                log.warning(f"[Veo] image not found locally: {local_path}")
        except Exception as e:
            log.warning(f"[Veo] failed to load image {image_url}: {e}")

    # Capabilities per real_model — Google по-разному поддерживает параметры.
    # Если параметр не поддержан — вообще не отправляем, иначе 400 INVALID_ARGUMENT.
    def _build_params(real_model: str) -> dict:
        is_veo3 = real_model.startswith("veo-3.")
        is_fast = "fast" in real_model
        # Только Veo 3 НЕ-fast и Veo 3.1 поддерживают нативное аудио.
        supports_audio = is_veo3 and not is_fast
        # negativePrompt в Veo 3 deprecated (как и в Imagen 4).
        supports_neg = real_model.startswith("veo-2")

        params = {"aspectRatio": ar, "sampleCount": 1,
                  "personGeneration": person_gen or "allow_all"}
        if seed is not None:
            params["seed"] = seed
        if not enhance_prompt:
            params["enhancePrompt"] = False
        if supports_neg and neg:
            params["negativePrompt"] = neg
        if supports_audio:
            params["generateAudio"] = bool(generate_audio)
        return params

    proxy = _google_proxy()

    last_err: str | None = None
    for key in keys:
        for real_model in candidates:
            try:
                start_url = (f"https://generativelanguage.googleapis.com/v1beta/"
                             f"models/{real_model}:predictLongRunning?key={key}")
                instance = {"prompt": prompt}
                # i2v поддерживается только Veo 3.x — для Veo 2 пропускаем.
                if image_payload and real_model.startswith("veo-3."):
                    instance["image"] = image_payload
                payload = {"instances": [instance],
                           "parameters": _build_params(real_model)}
                with httpx.Client(proxy=proxy, timeout=180) as client:
                    r = client.post(start_url, json=payload)
                if r.status_code != 200:
                    err = r.text[:200]
                    last_err = f"start {real_model}: {r.status_code} {err}"
                    log.warning(f"[Veo] {last_err}")
                    continue
                op_name = r.json().get("name")
                if not op_name:
                    last_err = f"no operation name: {r.text[:200]}"
                    continue

                # Polling операции до wallclock-лимита (по умолчанию 360с).
                # Раньше было `for _ in range(30): time.sleep(10)` — но при медленном
                # GET'е (timeout=60) общая длительность могла дойти до 30·(10+60)=35мин,
                # удерживая воркер. Теперь жёсткий wallclock-cap.
                op_url = f"https://generativelanguage.googleapis.com/v1beta/{op_name}?key={key}"
                video_uri = None
                _veo_poll_deadline = time.monotonic() + float(
                    os.getenv("VEO_POLL_TIMEOUT_SEC", "360")
                )
                with httpx.Client(proxy=proxy, timeout=30) as client:
                    while time.monotonic() < _veo_poll_deadline:
                        time.sleep(10)
                        if time.monotonic() >= _veo_poll_deadline:
                            last_err = "polling deadline exceeded"
                            break
                        try:
                            pr = client.get(op_url)
                        except Exception as poll_e:
                            last_err = f"poll exception: {type(poll_e).__name__}"
                            continue
                        if pr.status_code != 200:
                            continue
                        opd = pr.json()
                        if not opd.get("done"):
                            continue
                        if "error" in opd:
                            last_err = f"op error: {opd['error']}"
                            break
                        # Достаём первый сгенеренный видео-URI
                        resp = opd.get("response", {}) or {}
                        gvr = resp.get("generateVideoResponse", {}) or {}
                        samples = gvr.get("generatedSamples", []) or []
                        if samples:
                            video_uri = samples[0].get("video", {}).get("uri")
                        break
                if not video_uri:
                    log.warning(f"[Veo] {real_model}: no video uri ({last_err})")
                    continue

                # Скачиваем видео по URI (всё ещё через прокси) — это файл из Files API.
                # URI-формат: .../v1beta/files/<id>:download?alt=media — нужен ?key= параметр.
                # follow_redirects=True — Files API часто отдаёт 302 на storage.googleapis.com.
                dl_url = video_uri + (("&" if "?" in video_uri else "?") + f"key={key}")
                with httpx.Client(proxy=proxy, timeout=300, follow_redirects=True) as client:
                    dr = client.get(dl_url)
                if dr.status_code != 200:
                    last_err = f"download: {dr.status_code}"
                    continue
                url_local = _save_video_bytes(dr.content, "mp4")
                return {"type": "video", "url": url_local, "content": url_local,
                        "model": real_model}
            except Exception as e:
                last_err = f"{real_model}: {e}"
                log.warning(f"[Veo] key=...{key[-6:]} {last_err}")
                continue
    _notify_admin(f"Veo: все ключи и модели исчерпаны: {last_err}")
    return {"type": "text", "content": f"Видео не сгенерировано: {last_err or 'неизвестная ошибка'}"}


# ── NanoBanana / Imagen 4 ────────────────────────────────────────────────────
# Использует Google Generative Language API через прокси (для обхода
# гео-блока Google AI Studio из РФ-сегментов хостинга).
# Прокси задаётся env GOOGLE_HTTPS_PROXY=http://user:pass@host:port — если
# не задан, идём напрямую (что в РФ обычно не сработает).

def _google_proxy() -> str | None:
    """Возвращает URL прокси для Google-вызовов или None если не задан."""
    return _ai_proxy("google")


def _ai_proxy(provider: str) -> str | None:
    """Универсальный helper: возвращает прокси для AI-вызовов.

    Логика поиска:
      1. <PROVIDER>_HTTPS_PROXY задан → использовать его. Если задан
         пустой строкой (`PERPLEXITY_HTTPS_PROXY=`) — это **явный override
         «не использовать прокси для этого провайдера»**. Полезно когда
         API провайдера доступен напрямую с РФ (например Perplexity),
         а common-прокси не пропускает к нему.
      2. AI_HTTPS_PROXY — общий fallback для всех AI-вызовов.
      3. None — идём напрямую (работает на NL-сервере; на РФ-сервере
         для OpenAI/Anthropic/Google обычно нужен прокси).

    На РФ-проде нужно настроить как минимум `AI_HTTPS_PROXY=http://user:pass@eu-relay:3128`
    (squid на VPS в Hetzner/DO за €4/мес). Тогда все AI-вызовы пойдут
    через ваш прокси, и провайдеры (OpenAI/Anthropic/Google) увидят EU-IP
    вместо российского.

    Поддерживаемые provider-имена: openai, anthropic, google, xai (grok),
    perplexity. Можно использовать любую другую строку — будет искать
    `<UPPER>_HTTPS_PROXY` env-переменную.
    """
    p = (provider or "").upper().strip()
    specific_raw = os.getenv(f"{p}_HTTPS_PROXY")
    # None = переменная не задана → fallback на common.
    # "" = задана пустой → явный override «не использовать прокси».
    # "..." = задана с URL → используем его.
    if specific_raw is not None:
        specific = specific_raw.strip()
        return specific or None  # пустая → None, override common
    common = (os.getenv("AI_HTTPS_PROXY") or "").strip()
    return common or None


def _httpx_with_proxy(provider: str, **kwargs):
    """Хелпер: httpx.Client с подменой прокси на нужный AI-провайдер.

    Использование:
        with _httpx_with_proxy("openai", timeout=90) as client:
            r = client.post(...)
    """
    import httpx as _httpx
    proxy = _ai_proxy(provider)
    if proxy:
        kwargs["proxy"] = proxy
    return _httpx.Client(**kwargs)


def _openai_client_kwargs(provider: str = "openai") -> dict:
    """Возвращает kwargs для OpenAI() / Anthropic() — добавляет http_client
    с прокси если нужно. SDK-клиенты используют httpx внутри.

    Пример:
        client = OpenAI(api_key=key, **_openai_client_kwargs("openai"))
    """
    proxy = _ai_proxy(provider)
    if not proxy:
        return {}
    import httpx as _httpx
    return {"http_client": _httpx.Client(proxy=proxy, timeout=90)}


def _save_image_b64(b64: str, mime: str = "image/png") -> str:
    """Сохраняет base64-картинку в /uploads/ и возвращает URL.
    Так фронт может показать через <img src=/uploads/...> без data: схем."""
    import base64, os as _os, uuid as _uuid
    project_root = _os.path.dirname(_BASE_DIR)
    upload_dir = _os.path.join(project_root, "uploads")
    _os.makedirs(upload_dir, exist_ok=True)
    ext = "png" if "png" in mime else "jpg"
    fid = f"img_{_uuid.uuid4().hex[:12]}.{ext}"
    path = _os.path.join(upload_dir, fid)
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))
    return f"/uploads/{fid}"


# Таблица: ID модели в нашем UI → реальное имя в Google API.
# Imagen 3 устарел — есть только Imagen 4. Ранжирование по качеству:
# fast (быстрее, дешевле) → standard → ultra (медленнее, лучше).
#
# Дефолт `nano-v1` поднят с fast → ultra (юзер пожаловался что генерит на
# слабой модели; ultra — флагман Google для текст→изображение). Себест выше
# (~5.4 ₽ vs 1.8 ₽), но картинки реально чёткие. Юзер может перейти на fast
# через UI-селектор extra.model_variant='imagen-4-fast'.
_IMAGEN_MODELS = {
    "nano-v1":           "imagen-4.0-ultra-generate-001",  # дефолт — Ultra (топ)
    "imagen-4-fast":     "imagen-4.0-fast-generate-001",
    "imagen-4":          "imagen-4.0-generate-001",
    "imagen-4-ultra":    "imagen-4.0-ultra-generate-001",
}


def nanobanana_response(model: str, messages: list, extra: dict = None) -> dict:
    """Google Imagen 4 — генерация изображений через REST API.

    Поддерживает model_variant (fast/standard/ultra), aspectRatio, sampleCount,
    negative_prompt, personGeneration. Прокси через GOOGLE_HTTPS_PROXY.
    """
    keys = _shuffle(_get_api_keys("google"))
    extra = extra or {}
    if not keys:
        _notify_admin("Imagen: GOOGLE_API_KEYS пуст")
        return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}
    prompt = _last_text(messages)
    if not prompt:
        return {"type": "text", "content": "Опишите изображение в сообщении чата."}

    ar = (extra.get("aspect_ratio") or "1:1").strip()
    if ar not in ("1:1", "16:9", "9:16", "4:3", "3:4"):
        ar = "1:1"
    sample_count = max(1, min(int(extra.get("sample_count", 1) or 1), 4))
    # model_variant из UI приоритетнее чем model_id — так юзер выбирает fast/std/ultra.
    variant = (extra.get("model_variant") or "").strip()
    real_model = _IMAGEN_MODELS.get(variant) or _IMAGEN_MODELS.get(model) or _IMAGEN_MODELS["nano-v1"]
    neg = (extra.get("negative_prompt") or "").strip()
    person_gen = (extra.get("person_generation") or "").strip()

    parameters = {"sampleCount": sample_count, "aspectRatio": ar}
    if person_gen in ("allow_all", "allow_adult", "dont_allow"):
        parameters["personGeneration"] = person_gen
    # ВАЖНО: Imagen 4 убрал поддержку negativePrompt (Google deprecated в 2025).
    # Если юзер задал негатив — пихаем его в основной промпт текстом.
    if neg:
        prompt = f"{prompt}\n\nDo NOT include: {neg}"

    proxy = _google_proxy()
    last_err: Exception | None = None
    for key in keys:
        try:
            url = (f"https://generativelanguage.googleapis.com/v1beta/"
                   f"models/{real_model}:predict?key={key}")
            payload = {"instances": [{"prompt": prompt}], "parameters": parameters}
            with httpx.Client(proxy=proxy, timeout=120) as client:
                resp = client.post(url, json=payload)
            if resp.status_code != 200:
                log.warning(f"[Imagen] {real_model} key=...{key[-6:]} status={resp.status_code} body={resp.text[:200]}")
                last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
                continue
            data = resp.json()
            preds = data.get("predictions", [])
            if not preds:
                log.warning(f"[Imagen] empty predictions: {data}")
                continue
            p0 = preds[0]
            b64 = p0.get("bytesBase64Encoded")
            mime = p0.get("mimeType", "image/png")
            if not b64:
                log.warning(f"[Imagen] no bytesBase64Encoded: keys={list(p0.keys())}")
                continue
            url_local = _save_image_b64(b64, mime)
            return {"type": "image", "url": url_local, "content": url_local,
                    "model": real_model}
        except Exception as e:
            last_err = e
            log.warning(f"[Imagen] key=...{key[-6:]} error: {e}")
            continue
    _notify_admin(f"Imagen: все ключи исчерпаны: {last_err}")
    return {"type": "text", "content": f"Сервис временно недоступен: {last_err}"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _last_text(messages: list) -> str:
    for m in reversed(messages):
        c = m.get("content", "")
        if isinstance(c, dict):
            text = c.get("text", "")
            if text:
                return text
            continue
        if isinstance(c, str) and not c.startswith("/uploads/"):
            return c
    return ""


# ── model registry ────────────────────────────────────────────────────────────

MODEL_REGISTRY = {
    "gpt":             {"provider": "openai",      "real_model": "gpt-4o-mini"},
    "gpt-4o-mini":     {"provider": "openai",      "real_model": "gpt-4o-mini"},  # alias — UI агентов/ботов использует это имя напрямую
    "gpt-4o":          {"provider": "openai",      "real_model": "gpt-4o"},
    "claude":          {"provider": "anthropic",   "real_model": "claude-sonnet-4-6"},
    "claude-sonnet":   {"provider": "anthropic",   "real_model": "claude-sonnet-4-6"},
    # Алиасы по полному real_model имени — нужно потому что orchestra_json
    # пилотов и creators/proposals/sites вызывают generate_response("claude-sonnet-4-6").
    # Без них resolve_model() возвращает None → «Модель не найдена».
    "claude-sonnet-4-6":         {"provider": "anthropic", "real_model": "claude-sonnet-4-6"},
    "claude-haiku":              {"provider": "anthropic", "real_model": "claude-haiku-4-5-20251001"},
    "claude-haiku-4":            {"provider": "anthropic", "real_model": "claude-haiku-4-5-20251001"},
    "claude-haiku-4-5":          {"provider": "anthropic", "real_model": "claude-haiku-4-5-20251001"},
    "claude-haiku-4-5-20251001": {"provider": "anthropic", "real_model": "claude-haiku-4-5-20251001"},
    "claude-opus-4-1":           {"provider": "anthropic", "real_model": "claude-opus-4-1-20250805"},
    "claude-opus-4-1-20250805":  {"provider": "anthropic", "real_model": "claude-opus-4-1-20250805"},
    # Премиум-tier для генерации сайтов: лучшее качество кода, дороже в 5×
    # (~$0.30-$0.50 себест за сайт), используется в /sites/...?quality=premium.
    "claude-opus":     {"provider": "anthropic",   "real_model": "claude-opus-4-1-20250805"},
    "gemini":          {"provider": "gemini",      "real_model": "gemini-1.5-flash"},
    "gemini-pro":      {"provider": "gemini",      "real_model": "gemini-1.5-pro"},
    # Perplexity sonar models (2024-2025): sonar (быстрый, дешёвый, с поиском),
    # sonar-pro (большой контекст + сложные запросы), sonar-reasoning (CoT).
    # `sonar-small-chat` была снята с поддержки.
    "perplexity":      {"provider": "perplexity",  "real_model": "sonar"},
    "perplexity-pro":  {"provider": "perplexity",  "real_model": "sonar-pro"},
    "perplexity-large":{"provider": "perplexity",  "real_model": "sonar-pro"},  # alias для legacy
    "grok":            {"provider": "grok",        "real_model": "grok-3"},
    "grok-large":      {"provider": "grok",        "real_model": "grok-3"},
    "nano":            {"provider": "nanobanana",  "real_model": "nano-v1"},
    "dalle":           {"provider": "openai_image","real_model": "dall-e-3"},
    # Новая модель OpenAI gpt-image-1 — поддерживает генерацию и редактирование
    # изображений. Идёт через тот же эндпоинт images.generate.
    "gpt-image":       {"provider": "openai_image","real_model": "gpt-image-1"},
    "kling":           {"provider": "kling",       "real_model": "kling-v1"},        # 60 ₽
    "kling-pro":       {"provider": "kling",       "real_model": "kling-v1-6"},      # 120 ₽ — legacy alias
    "kling-1-6":       {"provider": "kling",       "real_model": "kling-v1-6"},      # 120 ₽ — явный alias
    "kling-1-6-pro":   {"provider": "kling",       "real_model": "kling-v1-6-pro"},  # 180 ₽
    "kling-2":         {"provider": "kling",       "real_model": "kling-v2-master"}, # 150 ₽
    "kling-2-pro":     {"provider": "kling",       "real_model": "kling-v2-pro"},    # 300 ₽
    "kling-2-1":       {"provider": "kling",       "real_model": "kling-v2-1-master"}, # 100 ₽ (fast)
    "kling-3":         {"provider": "kling",       "real_model": "kling-v3-0"},      # 240 ₽
    "veo":             {"provider": "veo",         "real_model": "veo-3"},
}


# ── ANTHROPIC (Claude) ────────────────────────────────────────────────────────
def _extract_pdf_text(file_url: str) -> str:
    """Extract text content from a PDF file."""
    from PyPDF2 import PdfReader
    import io
    local_path = os.path.join(_BASE_DIR, file_url.lstrip("/"))
    try:
        with open(local_path, "rb") as f:
            reader = PdfReader(io.BytesIO(f.read()))
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text and text.strip():
                pages.append(f"[Страница {i+1}]\n{text.strip()}")
        if pages:
            return "\n\n".join(pages)
        return "[PDF не содержит извлекаемого текста (возможно, это скан изображений)]"
    except Exception as e:
        return f"[Ошибка чтения PDF: {e}]"


def _prepare_claude_content(content):
    """Convert file_url dict to Claude-compatible content blocks.

    Because the proxy api.aws-us-east-3.com doesn't support base64 image/document
    blocks (returns 502), we extract text from PDFs server-side and send as text.
    """
    import mimetypes
    if isinstance(content, str):
        return [{"type": "text", "text": content}]

    if isinstance(content, dict) and "file_url" in content:
        file_url = content.get("file_url", "")
        text = content.get("text", "")
        try:
            b64, mime = _file_to_base64(file_url)
            is_pdf = mime == "application/pdf"
            if is_pdf:
                # Extract text from PDF instead of sending base64 (proxy doesn't support it)
                pdf_text = _extract_pdf_text(file_url)
                blocks = [{"type": "text", "text": f"[Файл: {file_url.split('/')[-1]}]\n\n{pdf_text}"}]
            else:
                # Images: can't extract text meaningfully, so describe the file
                file_name = file_url.split("/")[-1]
                blocks = [{"type": "text", "text": f"[Прикреплено изображение: {file_name}, тип: {mime}]\n\nПользователь прикрепил это изображение и попросил его проанализировать. Опишите, что вы можете сказать об этом изображении на основе вашего понимания контекста из текстового запроса пользователя."}]
            if text:
                blocks = [{"type": "text", "text": text}] + blocks
            return blocks
        except Exception as e:
            return [{"type": "text", "text": f"[Ошибка загрузки файла: {e}]"}]

    if isinstance(content, dict):
        return [{"type": "text", "text": str(content)}]
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": str(content)}]


def anthropic_response(model: str, messages: list, extra: dict = None,
                       user_key: str = None) -> dict:
    if user_key:
        keys = [user_key]
    else:
        keys = _shuffle(_get_api_keys("anthropic"))
    if not keys:
        _notify_admin("Anthropic: ANTHROPIC_API_KEYS пуст")
        return {"type":"text","content":"Сервис временно недоступен. Повторите попытку позже…"}
    base_url = os.getenv("ANTHROPIC_BASE_URL") if not user_key else None
    system = next((m["content"] for m in messages if m["role"]=="system"), "Ты полезный ассистент.")
    user_msgs = [m for m in messages if m["role"]!="system"]
    # Convert messages to Claude-compatible content blocks
    claude_msgs = []
    for m in user_msgs:
        claude_msgs.append({"role": m["role"], "content": _prepare_claude_content(m["content"])})
    # Prompt caching: если system prompt длинный (>1024 символов), кэшируем его
    system_text = system if isinstance(system, str) else "Ты полезный ассистент."
    use_caching = len(system_text) > 1024
    system_block = (
        [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
        if use_caching else system_text
    )

    for key in keys:
        try:
            if base_url:
                # Non-streaming request (проще и надёжнее)
                headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                           "content-type": "application/json"}
                # CAP: Sonnet hard-max = 64k output, реально нужно ≤16k для UI/КП.
                # Клиент с max_tokens=100000 заставил бы модель генерить долго
                # и оплачивать в копейках выше реальной потребности.
                _max_tok = min(int((extra or {}).get("max_tokens", 8192)), 16384)
                r = httpx.post(
                    f"{base_url.rstrip('/')}/v1/messages",
                    json={"model": model, "max_tokens": _max_tok,
                          "thinking": {"type": "disabled"},
                          "system": system_block,
                          "messages": claude_msgs},
                    headers=headers,
                    timeout=180,
                )
                if r.status_code != 200:
                    raise RuntimeError(_sanitize_error(f"proxy HTTP {r.status_code}: {r.text[:200]}"))
                data = r.json()
                text_parts = []
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                text = "".join(text_parts)
                usage = data.get("usage", {})
                log.info(f"[Anthropic] proxy OK model={model} chars={len(text)} in={usage.get('input_tokens',0)} out={usage.get('output_tokens',0)} cached={usage.get('cache_read_input_tokens',0)}")
                if text:
                    return {
                        "type": "text", "content": text,
                        "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "cached_tokens": usage.get("cache_read_input_tokens", 0),
                    }
                # Fallback: без thinking и без caching
                log.warning(f"[Anthropic] empty response, retrying basic")
                r2 = httpx.post(
                    f"{base_url.rstrip('/')}/v1/messages",
                    json={"model": model, "max_tokens": _max_tok,
                          "system": system_text,
                          "messages": claude_msgs},
                    headers=headers,
                    timeout=180,
                )
                if r2.status_code == 200:
                    data2 = r2.json()
                    text2 = "".join(b.get("text", "") for b in data2.get("content", []) if b.get("type") == "text")
                    usage2 = data2.get("usage", {})
                    if text2:
                        return {
                            "type": "text", "content": text2,
                            "input_tokens": usage2.get("input_tokens", 0),
                            "output_tokens": usage2.get("output_tokens", 0),
                        }
                raise RuntimeError(_sanitize_error(f"Empty response from proxy. Raw: {json.dumps(data)[:300]}"))
            else:
                import anthropic as _ant
                # CAP: Sonnet hard-max = 64k output, реально нужно ≤16k для UI/КП.
                # Клиент с max_tokens=100000 заставил бы модель генерить долго
                # и оплачивать в копейках выше реальной потребности.
                _max_tok = min(int((extra or {}).get("max_tokens", 8192)), 16384)
                # timeout=600s — для генерации сайтов (Sonnet с 16k max_tokens
                # часто думает 90-180 сек, а с auto-continue ещё дольше).
                resp = _ant.Anthropic(api_key=key, timeout=_ANTHROPIC_TIMEOUT,
                                        **_openai_client_kwargs("anthropic")
                                        ).messages.create(
                    model=model, max_tokens=_max_tok,
                    messages=claude_msgs,
                    system=system_block if use_caching else system_text,
                )
                return {
                    "type": "text", "content": resp.content[0].text,
                    "input_tokens": getattr(resp.usage, "input_tokens", 0) + getattr(resp.usage, "cache_creation_input_tokens", 0),
                    "output_tokens": getattr(resp.usage, "output_tokens", 0),
                    "cached_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
                }
        except Exception as e:
            log.error(f"[Anthropic] key=...{key[-6:]} model={model} error={e}")
            if key == keys[-1]:
                _notify_admin(f"Anthropic: все ключи исчерпаны (модель {model}): {e}")
                return {"type":"text","content":"Сервис временно недоступен. Повторите попытку позже…"}

# ── GEMINI ────────────────────────────────────────────────────────────────────
def gemini_response(model: str, messages: list, extra: dict = None) -> dict:
    keys = _shuffle(_get_api_keys("google"))
    if not keys:
        _notify_admin("Gemini: GOOGLE_API_KEYS пуст")
        return {"type":"text","content":"Сервис временно недоступен. Повторите попытку позже…"}
    prompt = _last_text(messages)
    for key in keys:
        try:
            resp = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                json={"contents":[{"parts":[{"text":prompt}]}]},
                timeout=30
            )
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            usage = data.get("usageMetadata", {})
            return {
                "type": "text", "content": text,
                "input_tokens": usage.get("promptTokenCount", 0),
                "output_tokens": usage.get("candidatesTokenCount", 0),
            }
        except:
            if key == keys[-1]:
                _notify_admin(f"Gemini: все ключи исчерпаны (модель {model})")
                return {"type":"text","content":"Сервис временно недоступен. Повторите попытку позже…"}

def grok_search_response(prompt: str, enable_web: bool = True, enable_x: bool = True,
                         model: str = "grok-4-fast-reasoning") -> dict:
    """
    Grok через /v1/responses с tools web_search + x_search.
    prompt — задача для Grok.
    Возвращает {content, input_tokens, output_tokens}.
    """
    keys = _shuffle(_get_api_keys("grok"))
    if not keys:
        return {"type": "text", "content": "[Grok: нет ключей]"}

    tools = []
    if enable_web: tools.append({"type": "web_search"})
    if enable_x:   tools.append({"type": "x_search"})

    for key in keys:
        try:
            r = httpx.post(
                "https://api.x.ai/v1/responses",
                json={
                    "model": model,
                    "input": prompt,
                    "tools": tools,
                },
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=180,
            )
            if r.status_code != 200:
                log.error(f"[Grok Search] {r.status_code}: {r.text[:200]}")
                if key == keys[-1]:
                    return {"type": "text", "content": f"[Grok error: {r.status_code}]"}
                continue
            data = r.json()
            # Собираем текст из output[].content[].text
            text_parts = []
            for out in data.get("output", []):
                for c in out.get("content", []) or []:
                    if c.get("type") in ("output_text", "text"):
                        text_parts.append(c.get("text", ""))
            text = "".join(text_parts)
            usage = data.get("usage", {})
            return {
                "type": "text", "content": text,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }
        except Exception as e:
            log.error(f"[Grok Search] exception: {e}")
            if key == keys[-1]:
                return {"type": "text", "content": f"[Grok exception: {e}]"}
    return {"type": "text", "content": "[Grok: недоступен]"}


# ── GROK (xAI) ───────────────────────────────────────────────────────────────
# Хелпер для OpenAI-совместимых endpoint'ов (Grok, Perplexity, и любые другие
# с `client.chat.completions.create`). Извлекаем 1 раз — экономит ~30 строк дублей.
def _openai_compatible_response(provider: str, base_url: str, model: str,
                                 messages: list, default_model: str = "") -> dict:
    from openai import OpenAI
    def _call(key):
        client = OpenAI(api_key=key, base_url=base_url, timeout=90,
                          **_openai_client_kwargs(provider))
        resp = client.chat.completions.create(
            model=model or default_model, messages=messages,
        )
        usage = getattr(resp, "usage", None)
        return {
            "type": "text",
            "content": resp.choices[0].message.content,
            "input_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
        }
    return try_with_keys(provider, _call) or _fallback_response()


def grok_response(model: str, messages: list, extra: dict = None) -> dict:
    return _openai_compatible_response("grok", "https://api.x.ai/v1", model, messages)


# ── PERPLEXITY ────────────────────────────────────────────────────────────────
def perplexity_response(model: str, messages: list, extra: dict = None) -> dict:
    return _openai_compatible_response(
        "perplexity", "https://api.perplexity.ai", model, messages,
        default_model="sonar",  # старая sonar-small-chat снята с поддержки
    )

# ── OPENAI IMAGE (DALL-E) ─────────────────────────────────────────────────────
def openai_image_response(model: str, messages: list, extra: dict = None) -> dict:
    """Генерация и редактирование изображений: dall-e-3 ИЛИ gpt-image-1.

    Если в последнем user-message прикреплён file_url с картинкой —
    используем images.edit (gpt-image-1 умеет генерить новую картинку
    с reference). Иначе обычный images.generate.

    Размер берём из extra.size (1024x1024 / 1024x1536 портрет / 1536x1024 пейзаж).
    """
    keys = _shuffle(_get_api_keys("openai"))
    if not keys:
        _notify_admin("Image gen: OPENAI_API_KEYS пуст")
        return {"type":"text","content":"Сервис временно недоступен. Повторите попытку позже…"}

    extra = extra or {}
    real_model = model or "dall-e-3"

    # Извлекаем prompt и список reference-картинок из last user message.
    # Content может быть строкой ИЛИ dict. Поддерживаем оба формата:
    # legacy {text, file_url} и новый {text, file_urls: [...]}.
    prompt = ""
    ref_image_urls: list[str] = []
    if messages:
        last = None
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last = m.get("content", "")
                break
        if last is None:
            last = messages[-1].get("content", "") if isinstance(messages[-1], dict) else ""

        def _extract(p_dict):
            urls = []
            if p_dict.get("file_urls"):
                urls = list(p_dict["file_urls"])
            elif p_dict.get("file_url"):
                urls = [p_dict["file_url"]]
            return urls, (p_dict.get("text", "") or "")

        if isinstance(last, dict):
            ref_image_urls, prompt = _extract(last)
        elif isinstance(last, str):
            try:
                p = json.loads(last)
                if isinstance(p, dict) and (p.get("file_url") or p.get("file_urls")):
                    ref_image_urls, prompt = _extract(p)
                else:
                    prompt = last
            except Exception:
                prompt = last
    if not prompt:
        prompt = extra.get("prompt", "")
    if not prompt and ref_image_urls:
        prompt = "Сгенерируй похожее изображение в той же стилистике"
    if not prompt:
        return {"type":"text","content":"Опишите что нарисовать (хотя бы пару слов)."}

    # Размеры: dall-e-3 поддерживает 1024x1024/1024x1792/1792x1024
    # gpt-image-1 поддерживает 1024x1024/1024x1536 (portrait)/1536x1024 (landscape)
    size = extra.get("size", "1024x1024")
    if real_model == "gpt-image-1":
        # нормализуем dall-e размеры в gpt-image
        size = {"1024x1792": "1024x1536", "1792x1024": "1536x1024"}.get(size, size)
        if size not in ("1024x1024", "1024x1536", "1536x1024", "auto"):
            log.warning(f"[Image gen] gpt-image-1: некорректный size={size}, fallback 1024x1024")
            size = "1024x1024"
    elif real_model == "dall-e-3":
        if size not in ("1024x1024", "1024x1792", "1792x1024"):
            log.warning(f"[Image gen] dall-e-3: некорректный size={size}, fallback 1024x1024")
            size = "1024x1024"
    quality = extra.get("quality")

    from openai import OpenAI
    import base64, os as _os, uuid as _uuid

    project_root = _os.path.dirname(_BASE_DIR)

    def _save_b64(b64: str) -> str:
        fid = f"img_{_uuid.uuid4().hex[:12]}.png"
        upload_dir = _os.path.join(project_root, "uploads")
        _os.makedirs(upload_dir, exist_ok=True)
        path = _os.path.join(upload_dir, fid)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return f"/uploads/{fid}"

    for key in keys:
        try:
            client = OpenAI(api_key=key, **_openai_client_kwargs("openai"))

            # ── Path A: edit mode (gpt-image-1 + reference image[s]) ──
            # gpt-image-1 принимает массив до 10 reference картинок.
            if ref_image_urls and real_model == "gpt-image-1":
                refs_local = []
                for u in ref_image_urls[:10]:
                    p = _os.path.join(project_root, u.lstrip("/"))
                    if _os.path.exists(p):
                        refs_local.append(p)
                if refs_local:
                    log.info(f"[Image gen] edit mode, refs={len(refs_local)} size={size} model={real_model}")
                    open_files = [open(p, "rb") for p in refs_local]
                    try:
                        edit_params = {
                            "model": real_model,
                            "image": open_files if len(open_files) > 1 else open_files[0],
                            "prompt": prompt, "n": 1, "size": size,
                            "quality": quality or "high",  # high по умолчанию — чёткие правки
                        }
                        resp = client.images.edit(**edit_params)
                    finally:
                        for f in open_files:
                            try: f.close()
                            except Exception: pass
                    data = resp.data[0]
                    url = getattr(data, "url", None)
                    if not url and getattr(data, "b64_json", None):
                        url = _save_b64(data.b64_json)
                    return {"type":"image","url":url,"content":url}

            # ── Path B: обычная генерация ──
            params = {"model": real_model, "prompt": prompt, "n": 1, "size": size}
            if real_model == "dall-e-3":
                # natural реалистичнее vivid (vivid сильно стилизует, не подходит
                # для бизнес-картинок). HD по умолчанию — себест +50%, но картинка
                # значительно чётче.
                params["style"] = extra.get("style", "natural")
                params["quality"] = quality or "hd"
                params["response_format"] = "url"
            elif real_model == "gpt-image-1":
                # gpt-image-1: low / medium / high / auto. Дефолт high — лучшее
                # качество для бизнес-задач. low дёшево, но размывает мелкие детали.
                params["quality"] = quality or "high"
            else:
                if quality:
                    params["quality"] = quality
            resp = client.images.generate(**params)
            data = resp.data[0]
            url = getattr(data, "url", None)
            if not url and getattr(data, "b64_json", None):
                url = _save_b64(data.b64_json)
            return {"type":"image","url":url,"content":url}
        except Exception as e:
            log.warning(f"[Image gen] key=...{key[-6:]} model={real_model} error={e}")
            if key == keys[-1]:
                _notify_admin(f"Image gen ({real_model}): все ключи исчерпаны")
                return {"type":"text","content":f"Не удалось сгенерировать картинку: {e}"}


PROVIDERS = {
    "openai":       openai_response,
    "openai_image": openai_image_response,
    "anthropic":    anthropic_response,
    "gemini":       gemini_response,
    "perplexity":   perplexity_response,
    "grok":         grok_response,
    "nanobanana":   nanobanana_response,
    "kling":        kling_response,
    "veo":          veo_response,
}


def resolve_model(model: str):
    return MODEL_REGISTRY.get(model)


def _log_ai_request(*, provider: str, model: str, purpose: str | None,
                    user_id: int | None, input_tokens: int, output_tokens: int,
                    cost_kop: int, duration_ms: int, success: bool,
                    error: str | None = None, user_key: bool = False,
                    request_id: str | None = None) -> None:
    """Fire-and-forget запись в ai_request_logs. Никогда не raise'ит —
    AI-биллинг не блокируется логированием."""
    try:
        from server.db import db_session
        from server.models import AiRequestLog
        with db_session() as db:
            db.add(AiRequestLog(
                user_id=user_id,
                provider=provider[:50],
                model=model[:100] if model else "?",
                purpose=(purpose or None) and str(purpose)[:50],
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                cost_kop=int(cost_kop or 0),
                duration_ms=int(duration_ms or 0),
                success=bool(success),
                error=(error or None) and str(error)[:200],
                user_key=bool(user_key),
                request_id=(request_id or None) and str(request_id)[:64],
            ))
            db.commit()
    except Exception as e:
        log.warning(f"[ai_request_log] failed: {type(e).__name__}: {e}")


# ── PII protection (152-ФЗ) ─────────────────────────────────────────────────
# Все user-content сообщений маскируются перед отправкой в LLM. PrivacyGuard
# заменяет email/phone/INN/SNILS/CC/IBAN на токены [[EMAIL_1]] и т.д., держит
# мапу в инстансе. После ответа result["content"] анмаскируется.
#
# Escape hatch: extra={"_privacy_skip": True} — для модулей, которые ДОЛЖНЫ
# видеть PII в исходном виде (например финансовый парсер выписки).
#
# Провайдеры image/video (kling, veo) — пропускаются: PII в prompt'е для генерации
# картинки нет смысла маскировать (картинка не строится по реальному ИНН).

_PG_SEP = "\x00@@AIPG@@\x00"  # уникальный разделитель, не матчится regex'ами


def _privacy_collect_texts(messages: list) -> tuple[list[str], list[tuple]]:
    """Возвращает (тексты, пути-к-ним) для последующей замены.

    path: ("str", msg_idx) — content был строкой
          ("block", msg_idx, block_idx) — content был list[dict], text внутри блока"""
    texts: list[str] = []
    paths: list[tuple] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            texts.append(content)
            paths.append(("str", i))
        elif isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "text":
                    txt = block.get("text", "")
                    if isinstance(txt, str):
                        texts.append(txt)
                        paths.append(("block", i, j))
    return texts, paths


def _privacy_mask_messages(messages: list, guard: PrivacyGuard) -> list:
    """Замаскировать PII во всех text-частях messages одним guard.
    Concat-approach: все тексты склеиваются разделителем, маскируются ОДНИМ
    вызовом mask() (чтобы map был общий), разделяются обратно."""
    if not messages:
        return messages
    texts, paths = _privacy_collect_texts(messages)
    if not texts:
        return messages
    big = _PG_SEP.join(texts)
    masked_big = guard.mask(big)
    parts = masked_big.split(_PG_SEP)
    if len(parts) != len(texts):
        # Маскировщик сломал разделитель — это аномалия (NUL-байт в PII не ожидается).
        # Поднимаем исключение → внешний caller сделает fail-closed (152-ФЗ),
        # вместо тихого fallback на отправку оригинала в LLM.
        raise RuntimeError(
            f"[Privacy] separator broken (parts={len(parts)}, expected={len(texts)})"
        )
    out = list(messages)
    for path, masked in zip(paths, parts):
        if path[0] == "str":
            i = path[1]
            out[i] = {**out[i], "content": masked}
        elif path[0] == "block":
            _, i, j = path
            blocks = list(out[i]["content"])
            blocks[j] = {**blocks[j], "text": masked}
            out[i] = {**out[i], "content": blocks}
    return out


def generate_response(model: str, messages: list, extra: dict = None,
                      user_api_key: str = None) -> dict:
    """Вызов AI-модели.

    user_api_key — если передан, используется вместо сервисного ключа.
    Прокидывается через extra["_user_key"] в провайдер.

    extra может содержать:
      _purpose: str — для AiRequestLog (chat/orchestra/proposal/sites/agent)
      _user_id: int — для AiRequestLog
      _request_id: str — X-Request-ID для cross-log трассировки
      _privacy_skip: bool — отключить маскировку PII (для системных вызовов
                           вроде парсинга выписки, где LLM ДОЛЖНА видеть ПД)
    """
    cfg = resolve_model(model)
    if not cfg:
        log.error(f"[AI] Модель не найдена: {model}")
        return {"type": "text", "content": f"Модель не найдена: {model}"}

    handler = PROVIDERS.get(cfg["provider"])
    if not handler:
        log.error(f"[AI] Провайдер не найден: {cfg['provider']}")
        return {"type": "text", "content": f"Провайдер не найден: {cfg['provider']}"}

    # Если передан пользовательский ключ — подставляем в env временно через extra
    if user_api_key:
        log.info(f"[AI] {cfg['provider']}: используется пользовательский ключ")
        _extra = dict(extra or {}, _user_key=user_api_key)
    else:
        _extra = extra or {}
        db_key_map = {"openai": "openai", "anthropic": "anthropic",
                      "kling": "kling", "grok": "grok",
                      "gemini": "google", "perplexity": "perplexity"}
        db_provider = db_key_map.get(cfg["provider"])
        if db_provider:
            keys = _get_api_keys(db_provider)
            log.info(f"[AI] {cfg['provider']}: real_model={cfg['real_model']} db_keys={len(keys)}")
            if not keys:
                log.error(f"[AI] {cfg['provider']}: НЕТ ключей в БД!")

    # Метаданные для AiRequestLog (поглощаются через extra, не передаются handler'у)
    _purpose = (_extra or {}).get("_purpose") if _extra else None
    _user_id = (_extra or {}).get("_user_id") if _extra else None
    _request_id = (_extra or {}).get("_request_id") if _extra else None

    real = cfg["real_model"]
    start_ms = int(time.time() * 1000)

    # PII маскировка перед LLM (skip для image/video и явного opt-out).
    #
    # Fail-CLOSED: при ошибке маскировки запрос ОТКЛОНЯЕТСЯ, а не уходит в
    # LLM с реальной PII. Это требование 152-ФЗ — нельзя «деградировать»
    # до утечки персональных данных во внешние провайдеры (OpenAI, Anthropic,
    # Google) из-за бага/исключения в guard.
    #
    # Аварийный escape-hatch: ENV PRIVACY_FAIL_OPEN=1 переключает обратно
    # в fail-open (старое поведение). Использовать ТОЛЬКО если найден баг в
    # PrivacyGuard который блокирует всё, и нужно временно вернуть сервис.
    _skip_privacy = bool(_extra.get("_privacy_skip")) or cfg["provider"] in ("kling", "veo")
    _guard: PrivacyGuard | None = None
    if not _skip_privacy:
        try:
            _guard = PrivacyGuard()
            messages = _privacy_mask_messages(messages, _guard)
        except Exception as _pe:
            _fail_open = os.getenv("PRIVACY_FAIL_OPEN", "").lower() in ("1", "true", "yes")
            log.error(
                "[Privacy] mask FAILED — %s: %s (fail_open=%s)",
                type(_pe).__name__, _pe, _fail_open,
            )
            if not _fail_open:
                # Не отправляем PII в LLM. Возвращаем graceful error.
                return {
                    "type": "text",
                    "content": (
                        "Сервис временно недоступен (защита персональных "
                        "данных). Попробуйте через пару минут или обратитесь "
                        "в поддержку, если ошибка повторяется."
                    ),
                    "error": "privacy_guard_failure",
                }
            _guard = None

    try:
        if cfg["provider"] in ("kling", "veo"):
            result = handler(real, messages, _extra)
        elif user_api_key and cfg["provider"] in ("anthropic", "openai", "gemini", "grok"):
            result = handler(real, messages, user_key=user_api_key)
        else:
            result = handler(real, messages)

        # Unmask текстового ответа (если маскировали)
        if _guard is not None and isinstance(result, dict):
            _content = result.get("content")
            if isinstance(_content, str):
                try:
                    result["content"] = _guard.unmask_response(_content)
                except Exception as _ue:
                    log.warning(f"[Privacy] unmask failed: {_ue}")

        # Логирование успеха (fire-and-forget — не блокирует ответ)
        try:
            usage = result.get("usage", {}) if isinstance(result, dict) else {}
            _log_ai_request(
                provider=cfg["provider"], model=real, purpose=_purpose,
                user_id=_user_id,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cost_kop=int(usage.get("actual_cost_kop", 0) or 0),
                duration_ms=int(time.time() * 1000) - start_ms,
                success=True, user_key=bool(user_api_key),
                request_id=_request_id,
            )
        except Exception:
            pass
        return result
    except Exception as e:
        log.error(f"[AI] Ошибка {cfg['provider']} ({real}): {type(e).__name__}: {e}")
        # Логирование ошибки тоже
        try:
            _log_ai_request(
                provider=cfg["provider"], model=real, purpose=_purpose,
                user_id=_user_id,
                input_tokens=0, output_tokens=0, cost_kop=0,
                duration_ms=int(time.time() * 1000) - start_ms,
                success=False, error=type(e).__name__,
                user_key=bool(user_api_key), request_id=_request_id,
            )
        except Exception:
            pass
        _notify_admin(f"{cfg['provider']} ({real}): {e}")
        return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}
