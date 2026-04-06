import os, random, time, base64, httpx
from openai import OpenAI
import anthropic as AnthropicSDK
from dotenv import load_dotenv
load_dotenv()

# ── helpers ──────────────────────────────────────────────────────────────────

def _keys(env): return [k.strip() for k in os.getenv(env, "").split(",") if k.strip()]

def _shuffle(lst): random.shuffle(lst); return lst

# ── token cost map (tokens per 1 request unit) ───────────────────────────────
# used for balance deduction — adjust multipliers to your pricing
TOKEN_COST = {
    "gpt-4o-mini":            50,
    "gpt-4o":                100,
    "claude-3-haiku-20240307": 40,
    "claude-3-5-sonnet-20241022": 120,
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

def _image_to_base64(file_url: str) -> tuple[str, str]:
    """Read image from disk by /uploads/... path, return (base64_data, media_type)."""
    import mimetypes
    local_path = os.path.join(_BASE_DIR, file_url.lstrip("/"))
    try:
        with open(local_path, "rb") as f:
            data = f.read()
        mime = mimetypes.guess_type(local_path)[0] or "image/jpeg"
        return base64.b64encode(data).decode(), mime
    except Exception as e:
        raise RuntimeError(f"Failed to read image: {e}")


# ── OpenAI ────────────────────────────────────────────────────────────────────

def openai_response(model: str, messages: list, extra: dict = None) -> dict:
    keys = _shuffle(_keys("OPENAI_API_KEYS"))
    if not keys:
        return {"type": "text", "content": "Нет API ключей OpenAI"}

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


def kling_response(model: str, messages: list, extra: dict = None) -> dict:
    """
    Полная реализация Kling API.
    generation_mode: text2video | image2video | image2video_frames | motion_control | avatar
    """
    keys = _shuffle(_keys("KLING_API_KEYS"))
    extra = extra or {}

    if not keys:
        return {"type": "text", "content": "[Kling] Нет API ключей. Добавьте KLING_API_KEYS в .env"}

    KLING_MODEL_MAP = {"kling": "kling-v1-6", "kling-pro": "kling-v1-6"}
    prompt    = extra.get("prompt") or _last_text(messages) or ""
    neg       = extra.get("negative_prompt", "")
    aspect    = extra.get("aspect_ratio", "16:9")
    duration  = int(extra.get("duration", 5))
    mode      = extra.get("mode", "std")
    cfg       = float(extra.get("cfg_scale", 0.5))
    gen_mode  = extra.get("generation_mode", "text2video")
    api_model = KLING_MODEL_MAP.get(model, "kling-v1-6")

    # Camera control
    cam_type  = extra.get("camera_type", "")
    cam_val   = float(extra.get("camera_value", 0))
    camera    = {"type": cam_type, cam_type: cam_val} if cam_type and cam_val != 0 else None

    h = {"Authorization": f"Bearer {keys[0]}", "Content-Type": "application/json"}
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
        return {"type": "text", "content": f"[Kling] Ошибка: {e}"}


def veo_response(model: str, messages: list, extra: dict = None) -> dict:
    """
    extra params:
      prompt, aspect_ratio (16:9|9:16|1:1), duration_seconds (5-8),
      sample_count (1-4), enhance_prompt (bool)
    """
    keys = _shuffle(_keys("VEO_API_KEYS"))
    project_id = os.getenv("VEO_PROJECT_ID", "")
    extra = extra or {}

    if not keys:
        return {"type": "text", "content": "[Veo] Нет API ключей. Добавьте ключ в Админке → API Ключи."}
    if not project_id:
        return {"type": "text", "content": "[Veo] Не задан Project ID. Добавьте ключ типа 'Veo Project ID' в Админке → API Ключи."}

    prompt = extra.get("prompt") or _last_text(messages)
    payload = {
        "instances": [{"prompt": prompt}],
        "parameters": {
            "aspectRatio": extra.get("aspect_ratio", "16:9"),
            "durationSeconds": int(extra.get("duration_seconds", 6)),
            "sampleCount": int(extra.get("sample_count", 1)),
            "enhancePrompt": extra.get("enhance_prompt", True),
        }
    }

    last_error = None
    for key in keys:
        try:
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            resp = httpx.post(
                f"https://us-central1-aiplatform.googleapis.com/v1/projects/{project_id}/locations/us-central1/publishers/google/models/{model}:predict",
                json=payload, headers=headers, timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            predictions = data.get("predictions", [])
            if predictions and predictions[0].get("bytesBase64Encoded"):
                b64 = predictions[0]["bytesBase64Encoded"]
                return {"type": "video_base64", "content": b64}
            return {"type": "text", "content": str(data)}
        except Exception as e:
            last_error = e
            continue
    return {"type": "text", "content": f"[Veo] Ошибка: {last_error}"}


# ── NanoBanana ────────────────────────────────────────────────────────────────

def nanobanana_response(model: str, messages: list, extra: dict = None) -> dict:
    """Google Imagen 3 — генерация изображений."""
    keys = _shuffle(_keys("NANO_API_KEYS"))
    extra = extra or {}
    if not keys:
        return {"type": "text", "content": "Imagen: добавьте Google API ключ в Админке → API Ключи."}
    prompt = _last_text(messages)
    if not prompt and not extra.get("image_url"):
        return {"type": "text", "content": "Опишите изображение или загрузите фото-референс."}

    params = {"sampleCount": int(extra.get("sample_count", 1))}
    # Aspect ratio mapping
    ar_map = {"1:1": "1:1", "16:9": "16:9", "9:16": "9:16", "4:3": "4:3", "3:4": "3:4"}
    ar = extra.get("aspect_ratio", "1:1")
    if ar in ar_map:
        params["aspectRatio"] = ar_map[ar]

    url = f"https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-002:predict?key={keys[0]}"
    payload = {"instances": [{"prompt": prompt or ""}], "parameters": params}
    try:
        resp = httpx.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        b64 = data["predictions"][0]["bytesBase64Encoded"]
        mime = data["predictions"][0].get("mimeType", "image/png")
        return {"type": "image", "content": f"data:{mime};base64,{b64}"}
    except Exception as e:
        return {"type": "text", "content": f"Imagen ошибка: {e}"}


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
    "claude":          {"provider": "anthropic",   "real_model": "claude-3-haiku-20240307"},
    "claude-sonnet":   {"provider": "anthropic",   "real_model": "claude-sonnet-4-5"},
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
def anthropic_response(model: str, messages: list, extra: dict = None) -> dict:
    keys = _shuffle(_keys("ANTHROPIC_API_KEYS"))
    if not keys:
        return {"type":"text","content":"[Claude] Нет API ключей"}
    system = next((m["content"] for m in messages if m["role"]=="system"), "Ты полезный ассистент.")
    user_msgs = [m for m in messages if m["role"]!="system"]
    try:
        import anthropic as _ant
        client = _ant.Anthropic(api_key=keys[0])
        resp = client.messages.create(
            model=model, max_tokens=1024, system=system,
            messages=[{"role":m["role"],"content":m["content"]} for m in user_msgs]
        )
        return {"type":"text","content":resp.content[0].text}
    except Exception as e:
        return {"type":"text","content":f"[Claude] Ошибка: {e}"}

# ── GEMINI ────────────────────────────────────────────────────────────────────
def gemini_response(model: str, messages: list, extra: dict = None) -> dict:
    keys = _shuffle(_keys("GEMINI_API_KEYS"))
    if not keys:
        return {"type":"text","content":"[Gemini] Нет API ключей"}
    prompt = _last_text(messages)
    try:
        import httpx
        resp = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={keys[0]}",
            json={"contents":[{"parts":[{"text":prompt}]}]},
            timeout=30
        )
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return {"type":"text","content":text}
    except Exception as e:
        return {"type":"text","content":f"[Gemini] Ошибка: {e}"}

# ── GROK (xAI) ───────────────────────────────────────────────────────────────
def grok_response(model: str, messages: list, extra: dict = None) -> dict:
    keys = _shuffle(_keys("GROK_API_KEYS"))
    if not keys:
        return {"type": "text", "content": "[Grok] Нет API ключей"}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=keys[0], base_url="https://api.x.ai/v1")
        resp = client.chat.completions.create(model=model, messages=messages)
        return {"type": "text", "content": resp.choices[0].message.content}
    except Exception as e:
        return {"type": "text", "content": f"[Grok] Ошибка: {e}"}

# ── PERPLEXITY ────────────────────────────────────────────────────────────────
def perplexity_response(model: str, messages: list, extra: dict = None) -> dict:
    keys = _shuffle(_keys("PERPLEXITY_API_KEYS"))
    if not keys:
        return {"type":"text","content":"[Perplexity] Нет API ключей"}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=keys[0], base_url="https://api.perplexity.ai")
        resp = client.chat.completions.create(model=model or "sonar-small-chat", messages=messages)
        return {"type":"text","content":resp.choices[0].message.content}
    except Exception as e:
        return {"type":"text","content":f"[Perplexity] Ошибка: {e}"}

# ── OPENAI IMAGE (DALL-E) ─────────────────────────────────────────────────────
def openai_image_response(model: str, messages: list, extra: dict = None) -> dict:
    keys = _shuffle(_keys("OPENAI_API_KEYS"))
    if not keys:
        return {"type":"text","content":"[DALL-E] Нет API ключей"}
    prompt = _last_text(messages) or (extra or {}).get("prompt","")
    size   = (extra or {}).get("size","1024x1024")
    style  = (extra or {}).get("style","vivid")
    try:
        from openai import OpenAI
        client = OpenAI(api_key=keys[0])
        resp = client.images.generate(model="dall-e-3", prompt=prompt, n=1, size=size, style=style)
        return {"type":"image","url":resp.data[0].url,"content":resp.data[0].url}
    except Exception as e:
        return {"type":"text","content":f"[DALL-E] Ошибка: {e}"}


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


def generate_response(model: str, messages: list, extra: dict = None) -> dict:
    cfg = resolve_model(model)
    if not cfg:
        return {"type": "text", "content": f"Модель не найдена: {model}"}

    handler = PROVIDERS.get(cfg["provider"])
    if not handler:
        return {"type": "text", "content": f"Провайдер не найден: {cfg['provider']}"}

    real = cfg["real_model"]
    try:
        if cfg["provider"] in ("kling", "veo"):
            return handler(real, messages, extra or {})
        return handler(real, messages)
    except Exception as e:
        return {"type": "text", "content": f"Ошибка: {e}"}
