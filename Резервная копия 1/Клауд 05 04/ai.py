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
}

def get_token_cost(model: str) -> int:
    return TOKEN_COST.get(model, 50)


# ── image helper: url → base64 ────────────────────────────────────────────────

def _image_to_base64(image_url: str) -> tuple[str, str]:
    """Fetch image from local server, return (base64_data, media_type)."""
    try:
        resp = httpx.get(image_url, timeout=10)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return base64.b64encode(resp.content).decode(), ct
    except Exception as e:
        raise RuntimeError(f"Failed to fetch image: {e}")


# ── OpenAI ────────────────────────────────────────────────────────────────────

def openai_response(model: str, messages: list) -> dict:
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
            full_url = f"http://127.0.0.1:8000{file_url}"
            try:
                b64, mime = _image_to_base64(full_url)
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"}
                })
            except Exception as e:
                parts.append({"type": "text", "text": f"[Не удалось загрузить изображение: {e}]"})
            formatted.append({"role": m["role"], "content": parts})

        elif isinstance(content, str) and content.startswith("/uploads/"):
            full_url = f"http://127.0.0.1:8000{content}"
            try:
                b64, mime = _image_to_base64(full_url)
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
                        raise
                    if attempt == 1:
                        raise
                    time.sleep(1)
        except Exception as e:
            last_error = e
            continue
    raise Exception(f"OpenAI failed: {last_error}")


# ── Claude / Anthropic ────────────────────────────────────────────────────────

def claude_response(model: str, messages: list) -> dict:
    keys = _shuffle(_keys("ANTHROPIC_API_KEYS"))
    if not keys:
        return {"type": "text", "content": "Нет API ключей Anthropic"}

    system_msg = None
    formatted = []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"]
            continue
        content = m["content"]

        if isinstance(content, dict) and "file_url" in content:
            parts = []
            if content.get("text"):
                parts.append({"type": "text", "text": content["text"]})
            full_url = f"http://127.0.0.1:8000{content['file_url']}"
            try:
                b64, mime = _image_to_base64(full_url)
                parts.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64}
                })
            except Exception as e:
                parts.append({"type": "text", "text": f"[Не удалось загрузить изображение: {e}]"})
            formatted.append({"role": m["role"], "content": parts})

        elif isinstance(content, str) and content.startswith("/uploads/"):
            full_url = f"http://127.0.0.1:8000{content}"
            try:
                b64, mime = _image_to_base64(full_url)
                formatted.append({"role": m["role"], "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
                ]})
            except Exception as e:
                formatted.append({"role": m["role"], "content": str(e)})
        else:
            formatted.append({"role": m["role"], "content": content if isinstance(content, str) else str(content)})

    last_error = None
    for key in keys:
        try:
            client = AnthropicSDK.Anthropic(api_key=key)
            kwargs = {"model": model, "max_tokens": 4096, "messages": formatted}
            if system_msg:
                kwargs["system"] = system_msg
            resp = client.messages.create(**kwargs)
            text = "".join(b.text for b in resp.content if hasattr(b, "text"))
            return {"type": "text", "content": text}
        except Exception as e:
            last_error = e
            continue
    raise Exception(f"Anthropic failed: {last_error}")


# ── Perplexity ────────────────────────────────────────────────────────────────

def perplexity_response(model: str, messages: list) -> dict:
    keys = _shuffle(_keys("PERPLEXITY_API_KEYS"))
    if not keys:
        return {"type": "text", "content": "Нет API ключей Perplexity"}

    # Perplexity не поддерживает vision — убираем изображения
    clean = []
    for m in messages:
        content = m["content"]
        if isinstance(content, dict) and "file_url" in content:
            text = content.get("text", "")
            if text:
                clean.append({"role": m["role"], "content": text})
        elif isinstance(content, str) and content.startswith("/uploads/"):
            continue
        else:
            clean.append({"role": m["role"], "content": content if isinstance(content, str) else str(content)})

    last_error = None
    for key in keys:
        try:
            client = OpenAI(api_key=key, base_url="https://api.perplexity.ai")
            resp = client.chat.completions.create(model=model, messages=clean)
            return {"type": "text", "content": resp.choices[0].message.content}
        except Exception as e:
            last_error = e
            continue
    raise Exception(f"Perplexity failed: {last_error}")


# ── Kling ─────────────────────────────────────────────────────────────────────

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
    extra = extra or {}

    if not keys:
        return {"type": "text", "content": "[Veo] Нет API ключей. Добавьте VEO_API_KEYS в .env"}

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
                f"https://us-central1-aiplatform.googleapis.com/v1/projects/PROJECT_ID/locations/us-central1/publishers/google/models/{model}:predict",
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

def nanobanana_response(model: str, messages: list) -> dict:
    keys = _shuffle(_keys("NANO_API_KEYS"))
    last_text = _last_text(messages)
    if not keys:
        return {"type": "text", "content": f"[NanoBanana]\n\nОтвет на: {last_text}"}
    # TODO: реальный API когда будет доступен
    return {"type": "text", "content": f"[NanoBanana / {model}]\n\nОтвет на: {last_text}"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _last_text(messages: list) -> str:
    for m in reversed(messages):
        c = m.get("content", "")
        if isinstance(c, dict):
            return c.get("text", "")
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
    "nano":            {"provider": "nanobanana",  "real_model": "nano-v1"},
    "dalle":           {"provider": "openai_image","real_model": "dall-e-3"},
    "kling":           {"provider": "kling",       "real_model": "kling-v1"},
    "kling-pro":       {"provider": "kling",       "real_model": "kling-v1-6"},
    "veo":             {"provider": "veo",         "real_model": "veo-3"},
}

PROVIDERS = {
    "openai":       openai_response,
    "openai_image": openai_response,
    "anthropic":    anthropic_response,
    "gemini":       gemini_response,
    "perplexity":   perplexity_response,
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
