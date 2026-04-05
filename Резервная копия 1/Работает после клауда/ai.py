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
    extra params:
      prompt, negative_prompt, aspect_ratio (16:9|9:16|1:1),
      duration (5|10), cfg_scale (0-1), mode (std|pro),
      image_url (for img2video)
    """
    keys = _shuffle(_keys("KLING_API_KEYS"))
    extra = extra or {}

    if not keys:
        return {"type": "text", "content": "[Kling] Нет API ключей. Добавьте KLING_API_KEYS в .env"}

    prompt = extra.get("prompt") or _last_text(messages)
    payload = {
        "model": model,
        "prompt": prompt,
        "negative_prompt": extra.get("negative_prompt", ""),
        "aspect_ratio": extra.get("aspect_ratio", "16:9"),
        "duration": int(extra.get("duration", 5)),
        "cfg_scale": float(extra.get("cfg_scale", 0.5)),
        "mode": extra.get("mode", "std"),
    }
    if extra.get("image_url"):
        payload["image_url"] = extra["image_url"]

    last_error = None
    for key in keys:
        try:
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            resp = httpx.post(
                "https://api.klingai.com/v1/videos/text2video",
                json=payload, headers=headers, timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            task_id = data.get("data", {}).get("task_id")
            if task_id:
                return {"type": "video_task", "content": f"Задача создана: {task_id}\nПроверяйте статус через /kling/status/{task_id}"}
            return {"type": "text", "content": str(data)}
        except Exception as e:
            last_error = e
            continue
    return {"type": "text", "content": f"[Kling] Ошибка: {last_error}"}


# ── Veo ───────────────────────────────────────────────────────────────────────

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
    # OpenAI
    "gpt":          {"provider": "openai",      "real_model": "gpt-4o-mini"},
    "gpt-4o":       {"provider": "openai",      "real_model": "gpt-4o"},

    # Claude
    "claude":       {"provider": "anthropic",   "real_model": "claude-3-haiku-20240307"},
    "claude-sonnet":{"provider": "anthropic",   "real_model": "claude-3-5-sonnet-20241022"},

    # Perplexity
    "perplexity":   {"provider": "perplexity",  "real_model": "sonar-small-chat"},
    "perplexity-large": {"provider": "perplexity", "real_model": "sonar-large-chat"},

    # Nano
    "nano":         {"provider": "nanobanana",  "real_model": "nano-v1"},

    # Kling
    "kling":        {"provider": "kling",       "real_model": "kling-v1"},
    "kling-pro":    {"provider": "kling",       "real_model": "kling-v1-5"},

    # Veo
    "veo":          {"provider": "veo",         "real_model": "veo-3"},
}

PROVIDERS = {
    "openai":     openai_response,
    "anthropic":  claude_response,
    "perplexity": perplexity_response,
    "nanobanana": nanobanana_response,
    "kling":      kling_response,
    "veo":        veo_response,
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
