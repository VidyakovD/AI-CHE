from openai import OpenAI
import os
import random
import time
from dotenv import load_dotenv
load_dotenv()

def get_openai_keys():
    keys = os.getenv("OPENAI_API_KEYS", "")
    return [k.strip() for k in keys.split(",") if k.strip()]

PROVIDERS = {
    "openai": lambda model, messages: openai_response(model, messages),
    "anthropic": lambda model, messages: "Claude пока не подключен",
    "perplexity": lambda model, messages: "Perplexity пока не подключен",
    "nanobanana": lambda model, messages: nanobanana_response(model, messages),
    "kling": lambda model, messages: kling_response(model, messages),
    "veo": lambda model, messages: veo_response(model, messages),
}

PROVIDER_FALLBACK = {
    "openai": ["openai"],
    "anthropic": ["anthropic", "openai"],
    "perplexity": ["perplexity", "openai"],
    "nanobanana": ["nanobanana", "openai"],
    "kling": ["kling", "openai"],
    "veo": ["veo", "openai"],
}

def generate_response(model, messages):
    model_config = resolve_model(model)

    if not model_config:
        return f"Модель не найдена: {model}"

    primary = model_config["provider"]
    real_model = model_config["real_model"]

    providers_chain = PROVIDER_FALLBACK.get(primary, [primary])

    last_error = None

    for provider in providers_chain:
        handler = PROVIDERS.get(provider)

        if not handler:
            continue

        try:
            return handler(real_model, messages)
        except Exception as e:
            print(f"[PROVIDER FAILED] {provider}:", e)
            last_error = e
            continue

    return f"Все провайдеры упали: {str(last_error)}"


MODEL_REGISTRY = {
    # OpenAI
    "gpt": {
        "provider": "openai",
        "real_model": "gpt-4o-mini"
    },

    # Claude
    "claude": {
        "provider": "anthropic",
        "real_model": "claude-3-haiku"
    },

    # Perplexity
    "perplexity": {
        "provider": "perplexity",
        "real_model": "sonar-small-chat"
    },

    # Nano
    "nano": {
        "provider": "nanobanana",
        "real_model": "nano-v1"
    },

    # Kling (видео)
    "kling": {
        "provider": "kling",
        "real_model": "kling-v1"
    },

    # Veo
    "veo": {
        "provider": "veo",
        "real_model": "veo-3"
    }
}

def resolve_model(model: str):
    return MODEL_REGISTRY.get(model)


def openai_response(model, messages):
    
    # преобразуем сообщения для vision
    formatted_messages = []

    for m in messages:
        content = m["content"]

        # если картинка
        if isinstance(content, str) and content.startswith("/uploads/"):
            formatted_messages.append({
                "role": m["role"],
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"http://127.0.0.1:8000{content}"
                        }
                    }
                ]
            })
        else:
            # ВСЕГДА делаем list, даже для текста
            formatted_messages.append({
                "role": m["role"],
                "content": [
                    {
                        "type": "text",
                        "text": content
                    }
                ]
            })
    keys = get_openai_keys()

    if not keys:
        return "Нет API ключей OpenAI"

    last_error = None

    random.shuffle(keys)

    for key in keys:
        try:
            client = OpenAI(api_key=key)

            for attempt in range(2):  # 2 попытки на ключ
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=formatted_messages,
                        timeout=10
                    )

                    return {
                        "type": "text",
                        "content": response.choices[0].message.content
                    }

                except Exception as e:
                    err_text = str(e).lower()

                    # ключ мертвый → сразу следующий ключ
                    if "401" in err_text or "invalid" in err_text:
                        print("[INVALID KEY]")
                        raise e

                    print(f"[RETRYABLE ERROR] attempt {attempt+1}:", e)

                    if attempt == 1:
                        raise e

                    time.sleep(1)  # пауза перед повтором

        except Exception as e:
            print(f"KEY FAILED: {key[:8]}...", e)
            last_error = e
            continue

    raise Exception(f"OpenAI failed: {str(last_error)}")

def get_nano_keys():
    keys = os.getenv("NANO_API_KEYS", "")
    return [k.strip() for k in keys.split(",") if k.strip()]

def nanobanana_response(model, messages):
    keys = get_nano_keys()

    if not keys:
        return f"[NanoBanana]\n\nОтвет на: {messages[-1]['content']}"

    last_error = None

    for key in keys:
        try:
            # ⚠️ пока без реального API
            # имитируем успешный ответ
            return f"[NanoBanana]\n\nОтвет на: {messages[-1]['content']}"

        except Exception as e:
            print(f"NANO KEY FAILED: {key[:6]}...", e)
            last_error = e
            continue

    return f"NanoBanana все ключи упали: {str(last_error)}"

def kling_response(model, messages):
    return "[Kling]\n\nВидео генерация пока не подключена"

def veo_response(model, messages):
    return "[Veo]\n\nВидео генерация пока не подключена"   