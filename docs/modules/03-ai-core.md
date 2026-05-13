# Модуль 03 — AI Core

> **Что это:** единая точка вызова всех AI-провайдеров (OpenAI, Anthropic, Perplexity, Google, Grok), MODEL_REGISTRY, прокси-маршрутизация, AI-аналитика. Open когда: добавляешь модель / провайдера, чинишь прокси, дебажишь stream, смотришь cost.

## TL;DR

- **Главный модуль:** [server/ai.py](server/ai.py) (1475 строк) — `generate_response(model, messages, ...)` — общий API для всех провайдеров.
- **5 провайдеров:** OpenAI · Anthropic · Perplexity · Grok (xai) · Google AI Studio.
- **Прокси:** Xray на `127.0.0.1:10809` для 4 провайдеров; Perplexity напрямую с РФ.
- **Аналитика:** AiRequestLog (таблица `ai_request_logs`) — каждый вызов логируется (provider/model/purpose/tokens/cost/duration). `/admin/ai-stats?days=N`.
- **Helpers:** `_ai_proxy(provider)`, `_openai_client_kwargs(provider)`, `_SecretFilter` (маскирует ключи в логах).

## Провайдеры и модели

| Провайдер | Модели | Прокси |
|---|---|---|
| OpenAI | gpt-4o, gpt-image-1, dall-e-3, **whisper-1**, **tts-1** | через Xray |
| Anthropic | claude-sonnet-4-6, claude-opus-4-1, claude-haiku-4 (streaming через AsyncAnthropic) | через Xray |
| Perplexity | sonar / sonar-pro / sonar-reasoning-pro | **напрямую** (`PERPLEXITY_HTTPS_PROXY=` override) |
| Grok (xai) | grok-2-latest | через Xray |
| Google AI Studio | Imagen 4, Veo 2/3, Gemini | через Xray |

⚠ **Perplexity:** старая `sonar-small-chat` снята с поддержки, **не использовать** (см. `858c222`).
⚠ **МАХ-провайдер:** не путать с MAX-каналом ботов — это разные вещи.

## MODEL_REGISTRY

В [server/ai.py](server/ai.py) — словарь моделей с метаданными: `provider`, `pricing` (per-1k tokens), `context_window`, `supports_streaming`, `purpose` (chat / image / video / voice).

## Прокси-логика

```python
# server/ai.py
def _ai_proxy(provider: str) -> str | None:
    # 1. Provider-specific override (например PERPLEXITY_HTTPS_PROXY=)
    # 2. Fallback AI_HTTPS_PROXY (общий)
    # 3. None
```

**Особенность:** пустая `PROVIDER_HTTPS_PROXY=` (например `PERPLEXITY_HTTPS_PROXY=`) считается **override "не использовать прокси"**, не fallback (`7d3e31f`).

Helper для OpenAI SDK: `_openai_client_kwargs(provider)` возвращает `{"http_client": httpx.AsyncClient(proxy=...)}`.

## generate_response — главная функция

```python
async def generate_response(
    model: str,
    messages: list[dict],
    system: str | None = None,
    user_id: int | None = None,    # для AiRequestLog
    purpose: str = "chat",         # для аналитики
    json_mode: bool = False,
    max_tokens: int | None = None,
    timeout: int = 600,            # 10 мин для больших Sonnet-запросов
    use_user_key: bool = False,    # свои API-ключи юзера (-80%)
) -> dict:  # {"content", "usage": {"input_tokens", "output_tokens", "cost_usd"}}
```

После каждого вызова **хук в AiRequestLog** ([server/privacy_guard.py](server/privacy_guard.py)).

## AiRequestLog (таблица `ai_request_logs`)

| Поле | Что |
|---|---|
| user_id, provider, model, purpose | базовое |
| input_tokens, output_tokens | usage |
| cost_usd, cost_kop | стоимость |
| duration_ms | время |
| error | если был fail |
| created_at | timestamp |

**Endpoint:** `/admin/ai-stats?days=N` — totals + by_model + top_users.

## API-ключи

- В БД таблица `api_keys` (поле `EncryptedString`), не в env, не в коде.
- TTL-кэш 60s на чтение из БД.
- **Свои API-ключи юзера** через `user_apikeys` таблицу → -80% скидка через `ai.user_key_discount_pct=20`.

## Безопасность

- ✅ `_SecretFilter` на root-handler — маскирует api-keys в exception/traceback
- ✅ **Prompt-injection защита в tool_run_llm** (агенты) — обёртка `<user_data>` теги + system-guard (`04ded59`)
- ✅ Timeout 600s на больших Sonnet-запросах (не infinite)
- ✅ `/admin/reencrypt-secrets` — ротация JWT_SECRET без потери EncryptedString-полей (`5cf647b`)

## PrivacyGuard (PII в LLM)

Перед отправкой в LLM маскируем PII (ИНН/КПП/ОГРН/СНИЛС/email/phone/card-Luhn), unmask на ответе:

```python
from server.privacy_guard import with_pii_protection
safe, guard = with_pii_protection(text)
ans = generate_response(...)
final = guard.unmask_response(ans["content"])
```

Подробнее — в [18-privacy-compliance.md](18-privacy-compliance.md).

## Гочча

- **`AI_HTTPS_PROXY` падает = все AI-фичи кроме Perplexity**. Аварийный план в [20-infra-deploy.md](20-infra-deploy.md).
- **Курс 95 ₽/$** как буфер на колебания (для пересчёта real_cost в копейки).
- **Google API key** был в `scripts/check_google_keys.py` (удалён, ⚠ ротировать).

## Тесты

- `tests/test_ai_request_log.py` — AI-аналитика
- `tests/test_smoke_builders.py` — smoke что generate_response работает

## Зависимости

- [02-billing](02-billing-payments.md) — каждый вызов через biller
- [04-chat](04-chat.md) — /message
- [06-solutions](06-solutions.md) — orchestra stages
- [07-proposals](07-proposals.md), [08-presentations](08-presentations.md), [09-sites](09-sites.md) — генерация
- [10-agents](10-agents-workflows.md) — tool_run_llm с инъекция-защитой
- [18-privacy](18-privacy-compliance.md) — PII-маскировка
