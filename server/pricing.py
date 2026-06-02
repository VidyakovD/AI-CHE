"""
Динамические цены из БД (таблица pricing_config) с TTL-кэшем.

Заменяет hardcoded константы вроде SITE_CREATE_FIX_COST. Админ может
поменять цену через `/admin/pricing` без редеплоя.

Использование:
    from server.pricing import get_price
    cost_kop = get_price("site.standard", default=150_000)

Кэш: 60 секунд. После UPDATE через админку — `invalidate_pricing_cache()`.
"""
from __future__ import annotations
import logging
import time
from server.db import db_session
from server.models import PricingConfig

log = logging.getLogger(__name__)

_TTL_SEC = 60
_cache: dict[str, tuple[float, int]] = {}


# Дефолты (в копейках). Используются при миссинге в БД и при first-run seed.
DEFAULTS: dict[str, tuple[int, str]] = {
    # (default_kop, label)
    "site.standard": (150_000, "Создание сайта — Стандарт (Sonnet)"),
    "site.premium":  (199_000, "Создание сайта — Премиум (Opus)"),
    "site.iter":     (    500, "Доработка сайта (одна итерация)"),
    "site.spec":     (      0, "Обсуждение ТЗ сайта"),
    "site.edit_block": (   500, "AI-правка блока сайта"),
    "presentation.standard": (5_000, "Презентация — Стандарт"),
    "presentation.premium":  (10_000, "Презентация — Премиум"),
    "kp.standard":           (5_000, "КП — Стандарт"),
    "kp.premium":            (10_000, "КП — Премиум"),
    # Коммерческие предложения: новый ключ-неймспейс proposal.*
    # (kp.* остался для backward-compat — pricing-админка может смотреть оба)
    "proposal.create":       (5_000, "КП: первая генерация (₽ × 100)"),
    "proposal.edit":         (  500, "КП: перегенерация / AI-правка секции"),
    "solution.standard":     (5_000, "Бизнес-решение — Стандарт"),
    "solution.premium":      (10_000, "Бизнес-решение — Премиум"),
    # Хранилище файлов юзеров (лидмагниты, медиа)
    "storage.per_100mb_month": (5_000, "Хранилище файлов: 50 ₽/мес за каждые 100 МБ"),
    "storage.upload_per_mb":   (    0, "Разовая плата за загрузку (₽ за МБ, 0 = бесплатно)"),
    # Создание ботов
    "bot.scratch_create":      (      0, "Создание бота с нуля (Canvas) — бесплатно"),
    "bot.template_create":     (      0, "Создание бота из шаблона — бесплатно"),
    "bot.ai_create_min":       (100_000, "AI-сборка бота: минимум 1000 ₽ + по токенам Claude"),
    "bot.ai_improve_min":      (      0, "AI-доработка: минимум (0 = без минимума, только real × margin)"),
    # Маржа на реальные AI-вызовы (диалоги бота с клиентами).
    # cost_charged = real_provider_cost * margin / 100
    # 300 = ×3 (300%) — стандартная B2B маржа на API
    "ai.reply_margin_pct":     (    300, "Маржа на реальные диалоги бота (300% = ×3)"),
    # Маржа на AI-доработку workflow / sites edit-block / прочие правки.
    # 500 = ×5 — выше чем диалоги т.к. это разовая работа, не volume.
    "ai.improve_margin_pct":   (    500, "Маржа на AI-правки (500% = ×5)"),
    # Скидка при использовании своих API-ключей юзера
    # 20 = 20% от обычной цены за инфраструктуру (хостинг, scheduler, БД)
    "ai.user_key_discount_pct": (    20, "% от обычной цены при использовании своих API-ключей"),
    # ── ИИ Агенты (модуль 23) — pay-per-message + pay-per-module-invoke ──────
    # Pay-per-run (50-300 ₽) утверждён 2026-05-15, для агентов адаптируем:
    # обычный chat 50 коп. (0.50 ₽) / сообщение, вызов модуля 100 коп. (1 ₽).
    # Onboarding (5 первых сообщений) — бесплатно чтобы юзер успел оценить.
    "agents.message":        (   50, "Сообщение личному ИИ-агенту (0.50 ₽)"),
    "agents.module_invoke":  (  100, "Вызов модуля агента (1 ₽)"),
    "agents.onboarding_free_messages": (5, "Сколько сообщений в онбординге бесплатно"),
    # Защита от абуза: один агент НЕ должен таскать с собой 48 ролей в каждом
    # промпте — это раздувает токены и стоимость. 12 — комфортный потолок:
    # хватает на основной набор (smm, копирайтер, аналитик, бухгалтер, юрист,
    # маркетолог, hr, поддержка, продажник, ассистент, проверяющий, переводчик).
    "agents.max_enabled_modules": (12, "Макс. одновременно подключённых модулей на агента"),
    # ── Knowledge Hub / RAG-индексация ───────────────────────────────────────
    # Embedding-вызов (OpenAI text-embedding-3-small, $0.02 / 1M токенов)
    # с маржой ×3 округлённо = 1 ₽ / МБ исходного файла.
    #   - PDF 1 МБ ≈ 100 коп = 1 ₽
    #   - DOCX 5 МБ ≈ 500 коп = 5 ₽
    #   - 100 МБ (max лимит) ≈ 10000 коп = 100 ₽
    # Без этой цены юзер мог бы загрузить 2 ГБ и потратить $40 нашего
    # OpenAI-бюджета бесплатно. Списание идёт только если cost ≥ embed_min_charge
    # (по умолчанию 1 ₽) — иначе мелочёвка бесплатно.
    "knowledge.embed_per_mb":    (100, "Индексация 1 МБ в RAG (1 ₽/МБ)"),
    "knowledge.embed_min_charge": (100, "Минимум для списания за embed (≤1 ₽ — бесплатно)"),
    # Кастомные домены для сайтов (CNAME + Let's Encrypt). Сам сертификат
    # бесплатен от LE, но certbot/nginx — наша инфраструктура. Лимит ставим
    # чтобы юзер не запросил 100 доменов и не положил nginx reload-ами.
    "sites.max_custom_domains_per_user": (5, "Макс. custom-доменов на юзера"),
    # Платный хостинг сайта (Шаг 5): разовое списание при первой публикации.
    # 990 ₽ — стандартная цена за «вечный» хостинг страницы на нашем сервере.
    # При повторной публикации (juniorы редактируют сайт) — не списываем.
    "sites.host_fix": (99_000, "Хостинг опубликованного сайта (разово, 990 ₽)"),
    # ── Multi-surface trial (Internal API /identify auto-create) ───────────
    # Стартовый кредит при первом identify через vk_user_id / tg_user_id /
    # max_user_id (без email). Юзеры через email-регистрацию идут через
    # routes/auth.py verify-email и получают welcome-bonus там — не дублируем.
    "multi_surface.trial_credits_kop": (50_000, "Trial-кредит при auto-create через TG/VK/MAX (500 ₽)"),
    "multi_surface.trial_days": (14, "Длительность trial в днях"),
    # ── VK MiniApp specific ────────────────────────────────────────────────
    # Локальные дефолты для VK MiniApp backend'а (aichevk.ru). Читаются через
    # /internal/v1/pricing → ключи "vkma.*". Соответствуют его прежним
    # message_send (0.10 кр) / autopost (0.50 кр), нормализованные в копейки.
    # Меняются через админку aiche.ru без редеплоя VK MiniApp.
    "vkma.message_send": (10, "VK: одно сообщение от агента в рассылке (10 коп = 0.10 кр)"),
    "vkma.autopost":     (50, "VK: автопост на стену сообщества (50 коп = 0.50 кр)"),
}


def calc_agent_cost_kop_float(model: str, input_tokens: int, output_tokens: int,
                                base_min_kop: int, alt_model: str | None = None) -> float:
    """Точная float-версия calc_agent_cost_kop. Возвращает копейки (может быть дробным).

    Используется в connector'ах с accumulator-биллингом (server.billing.
    deduct_with_accumulator), чтобы дробные копейки не терялись на коротких
    запросах. Минимум base_min_kop не применяется здесь — accumulator копит
    мелочь сам. Caller сам решает применять ли min.
    """
    if not model:
        return float(base_min_kop)
    margin_pct = max(100, _get_price_uncached("ai.reply_margin_pct", default=300))
    try:
        from server.db import db_session
        from server.models import ModelPricing
        with db_session() as db:
            def _lookup(mid: str) -> float | None:
                if not mid:
                    return None
                p = db.query(ModelPricing).filter_by(model_id=mid).first()
                if not p:
                    return None
                if p.ch_per_1k_input > 0 or p.ch_per_1k_output > 0:
                    return ((input_tokens / 1000.0) * float(p.ch_per_1k_input) +
                            (output_tokens / 1000.0) * float(p.ch_per_1k_output))
                if p.cost_per_req:
                    return float(p.cost_per_req)
                return None
            real = _lookup(model)
            if real is None and alt_model:
                real = _lookup(alt_model)
    except Exception as e:
        log.warning(f"[pricing.calc_agent_cost_float] {model}: {e}")
        return float(base_min_kop)
    if real is None or real <= 0:
        return float(base_min_kop)
    # margin_pct: real уже в копейках, margin учтена в ModelPricing если
    # цены пересчитаны через usd_rate × ×3. Но если pricing настроена
    # вручную (без recalc), всё ещё применяем ai.reply_margin_pct/100.
    # Чтобы избежать двойной маржи — после recalc ai.reply_margin_pct=100
    # будет означать «маржа уже в pricing». Пока оставляем ×3.
    return max(real * margin_pct / 100.0, float(base_min_kop))


def calc_agent_cost_kop(model: str, input_tokens: int, output_tokens: int,
                        base_min_kop: int, alt_model: str | None = None) -> int:
    """Стоимость LLM-вызова от лица ИИ-агента: real_cost × margin, минимум base_min_kop.

    Используется в:
      - send_message личного агента (минимум agents.message=50 коп)
      - invoke_module модуля (минимум agents.module_invoke=100 коп + skill_delta)
      - cron-runtime, webhook-trigger, tg/max relay

    Без этого helper'а агент всегда списывал фикс-цену независимо от объёма
    запроса. Теперь дорогие промпты (большой profile/memory + длинный ответ)
    стоят реально дороже, дешёвые ack-ответы — по минимуму.

    margin берётся из pricing_config['ai.reply_margin_pct'] (по умолч. 300 = ×3).
    base_min_kop — нижняя граница (чтобы каждый запрос имел осмысленную цену
    при коротких токенах и не уходил в 0).

    alt_model — fallback alias если real_model не в ModelPricing (как для
    Perplexity sonar → perplexity).
    """
    if not model:
        return base_min_kop
    margin_pct = max(100, _get_price_uncached("ai.reply_margin_pct", default=300))
    try:
        from server.db import db_session
        from server.models import ModelPricing
        with db_session() as db:
            def _lookup(mid: str) -> float | None:
                if not mid:
                    return None
                p = db.query(ModelPricing).filter_by(model_id=mid).first()
                if not p:
                    return None
                if p.ch_per_1k_input > 0 or p.ch_per_1k_output > 0:
                    return ((input_tokens / 1000.0) * float(p.ch_per_1k_input) +
                            (output_tokens / 1000.0) * float(p.ch_per_1k_output))
                if p.cost_per_req:
                    return float(p.cost_per_req)
                return None
            real = _lookup(model)
            if real is None and alt_model:
                real = _lookup(alt_model)
    except Exception as e:
        log.warning(f"[pricing.calc_agent_cost] {model}: {e}")
        return base_min_kop
    if real is None or real <= 0:
        return base_min_kop
    # margin_pct = 300 → real × 3. base_min — нижняя граница (защита от
    # 1-токенного «ОК» который стоит 0.5 коп — мы хотим минимум agent.message).
    return max(int(round(real * margin_pct / 100.0)), base_min_kop)


def _get_price_uncached(key: str, default: int | None = None) -> int:
    """get_price без кэша — для использования внутри calc_agent_cost_kop
    (избегаем вложенного TTL-кэш state на холодную)."""
    try:
        with db_session() as db:
            row = db.query(PricingConfig).filter_by(key=key).first()
            if row is not None:
                return int(row.value_kop)
    except Exception:
        pass
    if default is not None:
        return int(default)
    if key in DEFAULTS:
        return DEFAULTS[key][0]
    return 0


def get_price(key: str, default: int | None = None) -> int:
    """
    Текущая цена из БД (или дефолт). Кэш 60 секунд.
    Если default не передан и записи в БД нет — берётся DEFAULTS[key][0].
    """
    now = time.time()
    cached = _cache.get(key)
    if cached and (now - cached[0]) < _TTL_SEC:
        return cached[1]
    value: int | None = None
    try:
        with db_session() as db:
            row = db.query(PricingConfig).filter_by(key=key).first()
            if row is not None:
                value = int(row.value_kop)
    except Exception as e:
        log.warning(f"[pricing] failed to read {key}: {e}")
    if value is None:
        if default is not None:
            value = int(default)
        elif key in DEFAULTS:
            value = DEFAULTS[key][0]
        else:
            value = 0
    _cache[key] = (now, value)
    return value


def invalidate_pricing_cache(key: str | None = None) -> None:
    if key:
        _cache.pop(key, None)
    else:
        _cache.clear()


def seed_pricing_defaults() -> None:
    """
    Создать в БД записи с дефолтными ценами если их ещё нет.
    Вызывается при старте main.py.
    """
    try:
        with db_session() as db:
            existing = {r.key for r in db.query(PricingConfig).all()}
            added = 0
            for key, (kop, label) in DEFAULTS.items():
                if key in existing:
                    continue
                db.add(PricingConfig(key=key, value_kop=kop, label=label))
                added += 1
            if added:
                db.commit()
                log.info(f"[pricing] seeded {added} default prices")
    except Exception as e:
        log.warning(f"[pricing] seed failed: {e}")


def list_all_pricing() -> list[dict]:
    """Все цены для админки (упорядочены по key)."""
    out = []
    with db_session() as db:
        rows = db.query(PricingConfig).order_by(PricingConfig.key).all()
        existing = {r.key for r in rows}
        for r in rows:
            out.append({
                "key": r.key,
                "value_kop": int(r.value_kop),
                "value_rub": round(int(r.value_kop) / 100, 2),
                "label": r.label or DEFAULTS.get(r.key, (0, ""))[1],
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            })
        # Добавляем дефолтные (ещё не сохранённые) — чтобы в админке было видно полный список
        for key, (kop, label) in DEFAULTS.items():
            if key in existing:
                continue
            out.append({
                "key": key, "value_kop": kop,
                "value_rub": round(kop / 100, 2),
                "label": label, "updated_at": None,
            })
    return out


def update_price(key: str, value_kop: int, label: str | None = None) -> bool:
    """Обновить цену через админку. Возвращает True если изменено.
    Лимит value_kop: не более 100_000_000 коп (1 000 000 ₽) — защита от
    опечатки или взлома админки которая привела бы к мгновенной разрядке
    балансов всех юзеров (например storage-tick × огромная цена).
    """
    if value_kop < 0 or value_kop > 100_000_000:
        return False
    with db_session() as db:
        row = db.query(PricingConfig).filter_by(key=key).first()
        if row is None:
            db.add(PricingConfig(
                key=key, value_kop=int(value_kop),
                label=label or DEFAULTS.get(key, (0, ""))[1],
            ))
        else:
            row.value_kop = int(value_kop)
            if label is not None:
                row.label = label
        db.commit()
    invalidate_pricing_cache(key)
    return True
