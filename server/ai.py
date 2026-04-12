import os, random, time, base64, httpx, logging
from openai import OpenAI
import anthropic as AnthropicSDK
from dotenv import load_dotenv
load_dotenv()
log = logging.getLogger(__name__)

# ── helpers ──────────────────────────────────────────────────────────────────

def _get_api_keys(provider: str, sep=","):
    """Читает ключи из БД напрямую. Работает во всех воркерах без env."""
    from server.db import SessionLocal
    from server.models import ApiKey
    db = SessionLocal()
    try:
        # Kling ключ один и содержит запятую внутри: "ak_XXX,sk_YYY"
        if provider == "kling":
            rows = db.query(ApiKey).filter_by(provider="kling").all()
            return [r.key_value.strip() for r in rows if r.key_value.strip()]
        # Для остальных провайдеров: можно несколько ключей через запятую в одной записи
        rows = db.query(ApiKey).filter_by(provider=provider).all()
        result = []
        for r in rows:
            result.extend(k.strip() for k in r.key_value.split(sep) if k.strip())
        return result
    finally:
        db.close()

def _shuffle(lst): random.shuffle(lst); return lst


def _notify_admin(error_msg: str):
    """Отправляет ошибку в Telegram админу."""
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_ADMIN_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        text = f"⚠️ AI-CHE Error\n\nОшибка: {error_msg}"
        httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                   timeout=10)
    except:
        pass

# ── token cost map (tokens per 1 request unit) ───────────────────────────────
# used for balance deduction — adjust multipliers to your pricing
TOKEN_COST = {
    "gpt-4o-mini":            50,
    "gpt-4o":                100,
    "claude-haiku-4-5-20251001": 40,
    "claude-sonnet-4-6": 120,
    "claude-opus-4-6": 300,
    "sonar-small-chat":        30,
    "sonar-large-chat":        80,
    "nano-v1":                 10,
    "kling-v1":               500,
    "kling-v1-5":             800,
    "veo-3":                  600,
    "grok-3-mini":             30,
    "grok-3":                  80,
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
            client = OpenAI(api_key=key)
            for attempt in range(2):
                try:
                    resp = client.chat.completions.create(model=model, messages=formatted)
                    return {"type": "text", "content": resp.choices[0].message.content}
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

    KLING_MODEL_MAP = {"kling": "kling-v1", "kling-pro": "kling-v1-6"}
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


def _genai_veo(model: str, prompt: str, ar: str, duration: int) -> dict:
    """Синхронная обёртка над Google GenAI SDK для Veo."""
    from google import genai
    from google.genai import types
    import asyncio

    keys = _shuffle(_get_api_keys("google"))
    extra = {}
    for key in keys:
        try:
            client = genai.Client(api_key=key)

            # Асинхронный polling в синхронном контексте
            async def _run():
                operation = await client.aio.models.generate_videos(
                    model=model,
                    prompt=prompt,
                    config=types.GenerateVideosConfig(
                        aspect_ratio=ar,
                        duration_seconds=duration,
                    ),
                )
                # Poll до завершения (каждые 10 сек, макс 5 мин)
                for _ in range(30):
                    await asyncio.sleep(10)
                    operation = await client.aio.operations.get(
                        operation.name,
                        poll=types.GeneratedVideosList,
                    )
                    if operation.done:
                        break
                if not operation.done:
                    raise RuntimeError("Veo did not complete within the timeout")
                # Скачиваем результат
                videos = operation.result.generated_videos
                if videos:
                    downloaded = await client.aio.files.download(
                        file=videos[0].video
                    )
                    import base64
                    return base64.b64encode(downloaded.data).decode()
                raise RuntimeError("Veo returned no results")

            return asyncio.get_event_loop().run_until_complete(_run())
        except RuntimeError as e:
            last_error = e
            continue
    raise RuntimeError(f"Veo failed: {last_error}")


def veo_response(model: str, messages: list, extra: dict = None) -> dict:
    """
    extra params:
      prompt, aspect_ratio (16:9|9:16|1:1), duration_seconds (5-8),
      sample_count (1-4), enhance_prompt (bool)
    """
    extra = extra or {}
    keys = _get_api_keys("google")
    if not keys:
        _notify_admin("Veo: GOOGLE_API_KEYS пуст")
        return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}

    prompt = extra.get("prompt") or _last_text(messages) or ""
    ar = extra.get("aspect_ratio", "16:9")
    duration = int(extra.get("duration_seconds", 6))

    try:
        import httpx as _hx, asyncio

        # Fallback: старый REST подход с polling (если SDK не импортирован)
        from google import genai as _genai_sdk
        has_sdk = True
    except ImportError:
        has_sdk = False

    if has_sdk:
        try:
            return {"type": "text", "content": f"[Veo] Генерация запущена, модель: {model}. Результат будет отправлен при завершении."}
        except Exception:
            pass

    # Fallback: старый REST polling через Vertex API
    project_id = os.getenv("VEO_PROJECT_ID", "")
    if not project_id:
        return {"type": "text", "content": "Veo: VEO_PROJECT_ID не настроен"}

    keys = _shuffle(keys)
    for key in keys:
        try:
            # Запуск
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            resp = httpx.post(
                f"https://us-central1-aiplatform.googleapis.com/v1/projects/{project_id}/locations/us-central1/publishers/google/models/{model}:predict",
                json={"instances": [{"prompt": prompt}]},
                headers=headers, timeout=60
            )
            data = resp.json()
            predictions = data.get("predictions", [])
            if predictions and predictions[0].get("bytesBase64Encoded"):
                b64 = predictions[0]["bytesBase64Encoded"]
                return {"type": "video_base64", "content": b64}
            return {"type": "text", "content": str(data)}
        except Exception:
            if key == keys[-1]:
                _notify_admin(f"Veo: все ключи исчерпаны")
                return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}
    return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}


# ── NanoBanana ────────────────────────────────────────────────────────────────

def nanobanana_response(model: str, messages: list, extra: dict = None) -> dict:
    """Google Imagen 3 — генерация изображений через Google GenAI SDK."""
    keys = _shuffle(_get_api_keys("google"))
    extra = extra or {}
    if not keys:
        _notify_admin("Nano Banana: GOOGLE_API_KEYS пуст")
        return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}
    prompt = _last_text(messages)
    if not prompt:
        return {"type": "text", "content": "Опишите изображение в сообщении чата."}

    ar_map = {"1:1": "1:1", "16:9": "16:9", "9:16": "9:16", "4:3": "4:3", "3:4": "3:4"}
    ar = extra.get("aspect_ratio", "1:1")

    for key in keys:
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=key)

            response = client.models.generate_content(
                model="imagen-3.0-generate-002",  # или другая модель из БД
                contents=prompt,
                config=types.GenerateContentConfig(
                    image_config=types.ImageConfig(
                        aspect_ratio=ar_map.get(ar, "1:1")
                    ),
                ),
            )

            # Извлекаем inline_data из ответа
            for candidate in response.candidates or []:
                for part in candidate.content.parts or []:
                    if hasattr(part, "inline_data") and part.inline_data:
                        data_bytes = part.inline_data.data
                        import base64
                        b64 = base64.b64encode(data_bytes).decode()
                        mime = part.inline_data.mime_type or "image/png"
                        return {"type": "image", "content": f"data:{mime};base64,{b64}"}

            # Fallback: если модель не поддерживает inline_data, пробуем старый REST
            return _nanobanana_rest(keys[keys.index(key)+1:], prompt, ar, ar_map)

        except NotImplementedError:
            # SDK не поддерживает эту модель — пробуем REST
            return _nanobanana_rest(keys[keys.index(key)+1:], prompt, ar, ar_map)
        except Exception:
            if key == keys[-1]:
                return _nanobanana_rest([], prompt, ar, ar_map)

    return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}


def _nanobanana_rest(keys: list, prompt: str, ar: str, ar_map: dict) -> dict:
    """Fallback: старый REST вызов если SDK не сработал."""
    if not keys:
        return {"type": "text", "content": "Nano Banana: все ключи исчерпаны или API не поддерживается"}
    for key in keys:
        try:
            resp = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-002:predict?key={key}",
                json={"instances": [{"prompt": prompt}],
                      "parameters": {"sampleCount": 1, "aspectRatio": ar_map.get(ar, "1:1")}},
                timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            b64 = data["predictions"][0]["bytesBase64Encoded"]
            mime = data["predictions"][0].get("mimeType", "image/png")
            return {"type": "image", "content": f"data:{mime};base64,{b64}"}
        except:
            if key == keys[-1]:
                _notify_admin(f"Nano Banana: все ключи исчерпаны")
                return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}


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
    "gpt-4o":          {"provider": "openai",      "real_model": "gpt-4o"},
    "claude":          {"provider": "anthropic",   "real_model": "claude-sonnet-4-6"},
    "claude-sonnet":   {"provider": "anthropic",   "real_model": "claude-opus-4-6"},
    "gemini":          {"provider": "gemini",      "real_model": "gemini-1.5-flash"},
    "gemini-pro":      {"provider": "gemini",      "real_model": "gemini-1.5-pro"},
    "perplexity":      {"provider": "perplexity",  "real_model": "sonar-small-chat"},
    "perplexity-large":{"provider": "perplexity",  "real_model": "sonar"},
    "grok":            {"provider": "grok",        "real_model": "grok-3-mini"},
    "grok-large":      {"provider": "grok",        "real_model": "grok-3"},
    "nano":            {"provider": "nanobanana",  "real_model": "nano-v1"},
    "dalle":           {"provider": "openai_image","real_model": "dall-e-3"},
    "kling":           {"provider": "kling",       "real_model": "kling-v1"},
    "kling-pro":       {"provider": "kling",       "real_model": "kling-v1-6"},
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
    for key in keys:
        try:
            if base_url:
                text_parts = []
                with httpx.stream(
                    "POST",
                    f"{base_url.rstrip('/')}/v1/messages",
                    json={"model": model, "max_tokens": 8192, "stream": True,
                          "thinking": {"type": "disabled"},
                          "system": system if isinstance(system, str) else "Ты полезный ассистент.",
                          "messages": claude_msgs},
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    timeout=180,
                ) as r:
                    for line in r.iter_lines():
                        if line.startswith("data: "):
                            try:
                                d = json.loads(line[6:])
                                if d.get("type") == "content_block_delta":
                                    delta = d.get("delta", {})
                                    if delta.get("type") == "text_delta":
                                        text_parts.append(delta.get("text", ""))
                            except Exception:
                                pass
                log.info(f"[Anthropic] proxy OK model={model} chars={sum(len(p) for p in text_parts)}")
                if text_parts:
                    return {"type": "text", "content": "".join(text_parts)}
                raise RuntimeError("Пустой текстовый ответ от прокси")
            else:
                import anthropic as _ant
                resp = _ant.Anthropic(api_key=key).messages.create(
                    model=model, max_tokens=8192,
                    messages=claude_msgs,
                    system=system if isinstance(system, str) else "Ты полезный ассистент.",
                )
                return {"type":"text","content":resp.content[0].text}
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
            return {"type":"text","content":text}
        except:
            if key == keys[-1]:
                _notify_admin(f"Gemini: все ключи исчерпаны (модель {model})")
                return {"type":"text","content":"Сервис временно недоступен. Повторите попытку позже…"}

# ── GROK (xAI) ───────────────────────────────────────────────────────────────
def grok_response(model: str, messages: list, extra: dict = None) -> dict:
    keys = _shuffle(_get_api_keys("grok"))
    if not keys:
        _notify_admin("Grok: GROK_API_KEYS пуст")
        return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}
    from openai import OpenAI
    for key in keys:
        try:
            client = OpenAI(api_key=key, base_url="https://api.x.ai/v1")
            resp = client.chat.completions.create(model=model, messages=messages)
            return {"type": "text", "content": resp.choices[0].message.content}
        except:
            if key == keys[-1]:
                _notify_admin(f"Grok: все ключи исчерпаны (модель {model})")
                return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}

# ── PERPLEXITY ────────────────────────────────────────────────────────────────
def perplexity_response(model: str, messages: list, extra: dict = None) -> dict:
    keys = _shuffle(_get_api_keys("perplexity"))
    if not keys:
        _notify_admin("Perplexity: PERPLEXITY_API_KEYS пуст")
        return {"type":"text","content":"Сервис временно недоступен. Повторите попытку позже…"}
    from openai import OpenAI
    for key in keys:
        try:
            client = OpenAI(api_key=key, base_url="https://api.perplexity.ai")
            resp = client.chat.completions.create(model=model or "sonar-small-chat", messages=messages)
            return {"type":"text","content":resp.choices[0].message.content}
        except:
            if key == keys[-1]:
                _notify_admin(f"Perplexity: все ключи исчерпаны")
                return {"type":"text","content":"Сервис временно недоступен. Повторите попытку позже…"}

# ── OPENAI IMAGE (DALL-E) ─────────────────────────────────────────────────────
def openai_image_response(model: str, messages: list, extra: dict = None) -> dict:
    keys = _shuffle(_get_api_keys("openai"))
    if not keys:
        _notify_admin("DALL-E: OPENAI_API_KEYS пуст")
        return {"type":"text","content":"Сервис временно недоступен. Повторите попытку позже…"}
    prompt = _last_text(messages) or (extra or {}).get("prompt","")
    size   = (extra or {}).get("size","1024x1024")
    style  = (extra or {}).get("style","vivid")
    from openai import OpenAI
    for key in keys:
        try:
            client = OpenAI(api_key=key)
            resp = client.images.generate(model="dall-e-3", prompt=prompt, n=1, size=size, style=style)
            return {"type":"image","url":resp.data[0].url,"content":resp.data[0].url}
        except:
            if key == keys[-1]:
                _notify_admin(f"DALL-E: все ключи исчерпаны")
                return {"type":"text","content":"Сервис временно недоступен. Повторите попытку позже…"}


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


def generate_response(model: str, messages: list, extra: dict = None,
                      user_api_key: str = None) -> dict:
    """Вызов AI-модели.

    user_api_key — если передан, используется вместо сервисного ключа.
    Прокидывается через extra["_user_key"] в провайдер.
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

    real = cfg["real_model"]
    try:
        if cfg["provider"] in ("kling", "veo"):
            return handler(real, messages, _extra)
        # Для провайдеров поддерживающих user_key передаём через extra
        if user_api_key and cfg["provider"] in ("anthropic", "openai", "gemini", "grok"):
            return handler(real, messages, user_key=user_api_key)
        return handler(real, messages)
    except Exception as e:
        log.error(f"[AI] Ошибка {cfg['provider']} ({real}): {type(e).__name__}: {e}")
        _notify_admin(f"{cfg['provider']} ({real}): {e}")
        return {"type": "text", "content": "Сервис временно недоступен. Повторите попытку позже…"}
