"""USD-себестоимость моделей провайдеров (на 2026-05).

Источники цен:
  - OpenAI: https://openai.com/api/pricing/
  - Anthropic: https://www.anthropic.com/pricing#anthropic-api
  - Perplexity: https://docs.perplexity.ai/guides/pricing
  - xAI (Grok): https://x.ai/api
  - Google Imagen/Veo: https://ai.google.dev/pricing

Все per-token цены — USD за 1 МЛН токенов.
Per-request цены — USD за один вызов (картинки, видео, аудио).

Используется в server/cron/recalc_pricing.py:
  final_kop_per_1k = usd_per_1m / 1000 × usd_rate × margin × 100
  (1M→1k, RUB×100=коп)

Пример: gpt-4o-mini in=0.15 USD/1M, rate=92, margin=3 →
  0.15 / 1000 × 92 × 3 × 100 = 4.14 коп/1k = 0.0414 ₽/1k

На 100 input токенов — 0.00414 ₽ (не округляется до 0.01 благодаря
accumulator в server/billing.py).
"""

# Маржа над real_cost. Юзер задал ×3. Можно менять через pricing_config
# в будущем, но пока — константа кода.
DEFAULT_MARGIN = 3.0

# Per-token: { model_id: {"in": USD/1M_input, "out": USD/1M_output} }
PER_TOKEN_USD = {
    # OpenAI Chat
    "gpt-4o":            {"in": 2.50,  "out": 10.00},
    "gpt-4o-mini":       {"in": 0.15,  "out":  0.60},
    "gpt-4-turbo":       {"in": 10.00, "out": 30.00},  # legacy fallback
    # Anthropic Claude (актуальные 2026)
    "claude-haiku-4-5-20251001": {"in": 1.00, "out":  5.00},
    "claude-sonnet-4-6":         {"in": 3.00, "out": 15.00},
    "claude-opus-4-1-20250805":  {"in": 15.00, "out": 75.00},
    "claude-opus-4-20250514":    {"in": 15.00, "out": 75.00},
    # Perplexity Sonar
    "sonar":                {"in": 1.00, "out": 1.00},
    "sonar-pro":            {"in": 3.00, "out": 15.00},
    "sonar-reasoning-pro":  {"in": 5.00, "out": 25.00},
    # xAI Grok
    "grok-3-mini":   {"in": 0.30,  "out": 0.50},
    "grok-3":        {"in": 3.00,  "out": 15.00},
    # OpenAI Embeddings (для RAG — Knowledge Hub)
    "text-embedding-3-small": {"in": 0.02, "out": 0.00},
    "text-embedding-3-large": {"in": 0.13, "out": 0.00},
}

# Aliases — указывают на real_model выше, используются как fallback в
# calculate_cost (chat.py) при поиске по req.model. Каждый alias получит
# свою ModelPricing-запись при recalc.
ALIAS_TO_REAL = {
    "claude":          "claude-sonnet-4-6",
    "claude-sonnet":   "claude-sonnet-4-6",
    "claude-haiku":    "claude-haiku-4-5-20251001",
    "claude-haiku-4":  "claude-haiku-4-5-20251001",
    "claude-haiku-4-5":"claude-haiku-4-5-20251001",
    "claude-opus":     "claude-opus-4-1-20250805",
    "claude-opus-4-1": "claude-opus-4-1-20250805",
    "gpt":             "gpt-4o-mini",
    "grok":            "grok-3-mini",
    "grok-large":      "grok-3",
    "perplexity":      "sonar",
    "perplexity-pro":  "sonar-pro",
    "perplexity-large":"sonar-pro",
}

# Per-request media: { model_id: USD за 1 генерацию }
PER_REQUEST_USD = {
    # OpenAI images (DALL-E 3 HD, gpt-image-1 high quality)
    "dall-e-3":     0.08,
    "gpt-image-1":  0.19,
    # Google Imagen 4
    "imagen-4.0-fast-generate-001":  0.02,
    "imagen-4.0-generate-001":       0.04,
    "imagen-4.0-ultra-generate-001": 0.06,
    "nano-v1":      0.06,  # alias на imagen-4.0-ultra
    "nano":         0.02,  # alias на imagen-4.0-fast
    # Google Veo (5-сек ролик)
    "veo-3.0-fast-generate-preview": 0.30,
    "veo-3.0-generate-001":          0.75,
    "veo-3.0-fast-generate-001":     0.30,
    "veo-3.1-fast-generate-preview": 0.40,
    "veo-3.1-generate-preview":      0.60,
    "veo-2.0-generate-001":          0.20,
    "veo-3":                         0.30,  # legacy alias
    "veo":                           0.30,  # legacy alias
    # Kling — приблизительно (нет открытого тарифа)
    "kling":     0.25,
    "kling-pro": 0.50,
    # Whisper — $0.006/мин, среднее 1.5 мин на запрос
    "whisper-1": 0.01,
    # OpenAI TTS — $0.015/1k chars, в среднем ~500 chars на запрос
    "tts-1":     0.0075,
}


def calc_per_token_kop(model_id: str, usd_rate: float,
                        margin: float = DEFAULT_MARGIN) -> tuple[float, float] | None:
    """Возвращает (cost_kop_per_1k_input, cost_kop_per_1k_output) для модели.

    Float — точные дробные копейки. Применяются в ModelPricing.ch_per_1k_*.
    None если модель не в словаре PER_TOKEN_USD.
    """
    real_id = ALIAS_TO_REAL.get(model_id, model_id)
    usd = PER_TOKEN_USD.get(real_id)
    if not usd:
        return None
    # USD/1M → кoп/1k:  usd × rate × margin / 1000 × 100 = usd × rate × margin / 10
    in_kop_per_1k = usd["in"] * usd_rate * margin / 10.0
    out_kop_per_1k = usd["out"] * usd_rate * margin / 10.0
    return in_kop_per_1k, out_kop_per_1k


def calc_per_request_kop(model_id: str, usd_rate: float,
                          margin: float = DEFAULT_MARGIN) -> int | None:
    """Цена за вызов в копейках (целое для image/video — недокопейки бессмысленны)."""
    usd = PER_REQUEST_USD.get(model_id)
    if usd is None:
        return None
    return max(1, int(round(usd * usd_rate * margin * 100)))


def all_model_ids() -> list[str]:
    """Все модели для которых мы знаем себестоимость (per_token + per_request +
    aliases). Используется в recalc для прохода по всем."""
    out = set(PER_TOKEN_USD.keys()) | set(PER_REQUEST_USD.keys()) | set(ALIAS_TO_REAL.keys())
    return sorted(out)
