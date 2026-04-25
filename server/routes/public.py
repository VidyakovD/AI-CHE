from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import os, asyncio, logging
from datetime import datetime, timedelta

from server.routes.deps import get_db, current_user
from server.db import SessionLocal
from server.models import (
    FeatureFlag, ModelPricing, TokenPackage, PricingSetting,
    FaqItem, PromoCode, PromoUse, ExchangeRate, User, Transaction,
)
from server.security import require_admin

log = logging.getLogger(__name__)

router = APIRouter(tags=["public"])

# ═══════════════════════════════════════════════════════════════════════════════
# DEFAULT SEED DATA
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_MODEL_PRICING = [
    # Per-token цены в CH за 1000 токенов (1 CH ≈ 0.55₽; курс 100₽/$; маржа ×3)
    # Цены — в КОПЕЙКАХ за 1k токенов (1 ₽ = 100 коп). Формула: USD/1k × курс × маржа.
    # gpt-4o-mini: $0.00015/$0.0006 per 1K → ~1/3.5 коп
    {"model_id":"gpt",             "label":"GPT-4o mini",
     "ch_per_1k_input":1, "ch_per_1k_output":3.5, "min_ch_per_req":10},
    # gpt-4o: $0.0025/$0.01 per 1K → 14/55 коп
    {"model_id":"gpt-4o",          "label":"GPT-4o",
     "ch_per_1k_input":14, "ch_per_1k_output":55, "min_ch_per_req":20},
    # claude-haiku: $0.001/$0.005 per 1K → 6/28 коп
    {"model_id":"claude",          "label":"Claude Haiku",
     "ch_per_1k_input":6, "ch_per_1k_output":28, "min_ch_per_req":10},
    # claude-sonnet: $0.003/$0.015 per 1K → 17/83 коп
    {"model_id":"claude-sonnet",   "label":"Claude Sonnet",
     "ch_per_1k_input":17, "ch_per_1k_output":83, "min_ch_per_req":20},
    # perplexity sonar: $0.001/$0.001 per 1K → 6/6 коп
    {"model_id":"perplexity",      "label":"Perplexity Sonar",
     "ch_per_1k_input":6, "ch_per_1k_output":6, "min_ch_per_req":20},
    # perplexity sonar pro: $0.003/$0.015 per 1K → 17/83 коп
    {"model_id":"perplexity-large","label":"Perplexity Sonar Pro",
     "ch_per_1k_input":17, "ch_per_1k_output":83, "min_ch_per_req":30},
    # grok-3-mini: $0.0003/$0.0005 per 1K → 2/3 коп
    {"model_id":"grok",            "label":"Grok 3 mini",
     "ch_per_1k_input":2, "ch_per_1k_output":3, "min_ch_per_req":10},
    # grok-3: $0.002/$0.01 per 1K → 11/55 коп
    {"model_id":"grok-large",      "label":"Grok 3",
     "ch_per_1k_input":11, "ch_per_1k_output":55, "min_ch_per_req":20},
    # Media — per-request (копейки)
    {"model_id":"dalle",           "label":"DALL-E 3",         "cost_per_req":400,  "min_ch_per_req":400},
    # gpt-image-1: $0.04 / image standard → ~4 ₽; берём 5 ₽ = 500 коп с запаса
    {"model_id":"gpt-image",       "label":"GPT Картинки",     "cost_per_req":500,  "min_ch_per_req":500},
    {"model_id":"nano",            "label":"Imagen",           "cost_per_req":100,  "min_ch_per_req":100},
    {"model_id":"kling",           "label":"Kling v1",         "cost_per_req":2000, "min_ch_per_req":2000},
    {"model_id":"kling-pro",       "label":"Kling Pro",        "cost_per_req":4000, "min_ch_per_req":4000},
    {"model_id":"veo",             "label":"Veo 3",            "cost_per_req":3000, "min_ch_per_req":3000},
]

DEFAULT_FEATURES = [
    {"key": "video_gen",  "label": "Генерация видео (Kling / Veo)",      "description": "Показывать модели Kling и Veo в списке моделей", "enabled": True},
    {"key": "agents",     "label": "AI Агенты",                           "description": "Раздел создания и запуска AI агентов",           "enabled": True},
    {"key": "workflows",  "label": "Воркфлоу",                            "description": "Конструктор автоматических цепочек задач",       "enabled": True},
    {"key": "chatbots",   "label": "Чат-боты",                            "description": "Настройка и деплой пользовательских чат-ботов",  "enabled": True},
    {"key": "solutions",  "label": "Готовые решения",                     "description": "Каталог готовых AI решений и бизнес-шаблонов",   "enabled": True},
    {"key": "nano",       "label": "Imagen",                              "description": "Модель Imagen в списке моделей",                   "enabled": True},
    {"key": "dalle",      "label": "DALL-E (генерация изображений)",      "description": "Модель DALL-E в списке моделей",                 "enabled": True},
    {"key": "sites",      "label": "Создание сайтов",                     "description": "Модуль создания сайтов с ИИ",                   "enabled": True},
    {"key": "presentations", "label": "Презентации и КП",                 "description": "Генерация презентаций и коммерческих предложений", "enabled": True},
]


# ═══════════════════════════════════════════════════════════════════════════════
# SEED HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _seed_pricing(db: Session):
    """Заполняем дефолтные цены если таблица пустая."""
    if db.query(ModelPricing).count() == 0:
        for p in DEFAULT_MODEL_PRICING:
            db.add(ModelPricing(**p))
    if db.query(PricingSetting).count() == 0:
        for k, v, d in [
            ("usd_to_rub",     "90",   "Курс доллара к рублю"),
            ("ch_to_rub",      "0.10", "Стоимость 1 CH в рублях"),
            ("support_url",    "",     "Ссылка поддержки"),
            ("tg_bot_token",   "",     "Токен Telegram бота (для уведомлений об ошибках)"),
            ("tg_admin_chat_id","",     "Chat ID админа в Telegram (куда приходят уведомления)"),
            ("anthropic_base_url","",   "Базовый URL для Anthropic API (если используете прокси, напр. https://api.aws-us-east-3.com)"),
            ("error_webhook_url","",    "URL куда пересылать ошибки сервиса (n8n error-handler или другой webhook)"),
        ]:
            db.add(PricingSetting(key=k, value=v, description=d))
    if db.query(TokenPackage).count() == 0:
        for name, tokens, price in [
            ("Старт",   10_000,  49),
            ("Базовый", 50_000, 199),
            ("Большой",200_000, 699),
        ]:
            db.add(TokenPackage(name=name, tokens=tokens, price_rub=price))
    if db.query(FaqItem).count() == 0:
        faqs = [
            ("Что такое токены CH?",
             "CH (Che) — внутренняя валюта AI Студии Че. Каждый запрос к модели списывает определённое количество CH. CH входят в подписку или докупаются отдельно."),
            ("Как выбрать подходящую модель?",
             "GPT-4o mini и Claude Haiku — быстрые и экономичные для обычных задач. GPT-4o и Claude Sonnet — для сложного анализа и длинных текстов. Perplexity — для поиска актуальной информации. Kling и Veo — генерация видео."),
            ("Можно ли вернуть неиспользованные токены?",
             "Токены, входящие в подписку, не возвращаются. Докупленные токены действуют бессрочно."),
            ("Как работают готовые решения?",
             "Готовые решения — это настроенные сценарии с заготовленными промптами. Вы можете изменить промпт под свои нужды перед запуском. Стоимость списывается в CH."),
            ("Что такое реферальная программа?",
             "Поделитесь своим кодом — когда друг зарегистрируется по нему, вы оба получите бонусные CH."),
        ]
        for i, (q, a) in enumerate(faqs):
            db.add(FaqItem(question=q, answer=a, sort_order=i))
    db.commit()


def _seed_features(db: Session):
    for f in DEFAULT_FEATURES:
        if not db.query(FeatureFlag).filter_by(key=f["key"]).first():
            db.add(FeatureFlag(**f))
    db.commit()


async def startup_public(db: Session):
    """Seeds pricing/features and starts the exchange rate background task.
    Call from main.py startup."""
    _seed_pricing(db)
    _seed_features(db)
    asyncio.create_task(update_exchange_rate())


# ═══════════════════════════════════════════════════════════════════════════════
# EXCHANGE RATE
# ═══════════════════════════════════════════════════════════════════════════════

async def update_exchange_rate():
    """Обновляем курс USD/RUB каждые 12 часов через ЦБ РФ API."""
    while True:
        try:
            import httpx
            r = httpx.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=10)
            data = r.json()
            usd_rate = data["Valute"]["USD"]["Value"]
            db = SessionLocal()
            try:
                rec = db.query(ExchangeRate).filter_by(currency="USD").first()
                if rec:
                    rec.rate_rub = usd_rate
                    rec.updated_at = datetime.utcnow()
                else:
                    db.add(ExchangeRate(currency="USD", rate_rub=usd_rate))
                db.commit()
            finally:
                db.close()
        except Exception as e:
            pass  # Используем кэшированный курс
        await asyncio.sleep(43200)  # 12 часов


def get_usd_rate(db: Session) -> float:
    rec = db.query(ExchangeRate).filter_by(currency="USD").first()
    if not rec:
        log.warning("Exchange rate not set — falling back to 90.0")
        return 90.0
    return rec.rate_rub


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN COST CALCULATION
# ═══════════════════════════════════════════════════════════════════════════════

# Формула для языковых моделей: (цена_$ × 2 × курс_₽) / 0.4 = CH за запрос
# Фиксированные цены:
MODEL_USD_COST = {
    "gpt":             0.0001,   # GPT-4o mini ~$0.0001 per message
    "gpt-4o":          0.005,    # GPT-4o
    "claude":          0.0002,   # Claude Haiku
    "claude-sonnet":   0.006,    # Claude Sonnet
    "gemini":          0.00005,  # Gemini Flash
    "perplexity":      0.0002,
    "perplexity-large":0.001,
    # Фиксированные (не зависят от курса)
    "kling":           None,     # 200 CH fixed
    "kling-pro":       None,     # 400 CH fixed
    "veo":             None,     # 120 CH fixed
    "nano":            None,     # 10 CH fixed
}
FIXED_COSTS = {"kling":200,"kling-pro":400,"veo":120,"nano":10,"dalle":40}


def calc_tokens(model: str, usd_rate: float) -> int:
    fixed = FIXED_COSTS.get(model)
    if fixed: return fixed
    usd = MODEL_USD_COST.get(model, 0.001)
    if usd is None: return 200
    return max(1, round((usd * 2 * usd_rate) / 0.4))


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/features")
def get_features(db: Session = Depends(get_db)):
    """Возвращает словарь {key: enabled} для фронтенда."""
    flags = db.query(FeatureFlag).all()
    return {f.key: f.enabled for f in flags}


@router.get("/pricing/models")
def get_model_pricing(db: Session = Depends(get_db)):
    items = db.query(ModelPricing).order_by(ModelPricing.model_id).all()
    return [{"model_id":p.model_id,"label":p.label,"cost_per_req":p.cost_per_req,
             "usd_per_req":p.usd_per_req,"markup":p.markup} for p in items]


@router.get("/pricing/packages")
def get_packages(db: Session = Depends(get_db)):
    pkgs = db.query(TokenPackage).filter_by(is_active=True).order_by(TokenPackage.sort_order).all()
    return [{"id":p.id,"name":p.name,"tokens":p.tokens,"price_rub":p.price_rub} for p in pkgs]


@router.get("/pricing/settings")
def get_pricing_settings(db: Session = Depends(get_db)):
    items = db.query(PricingSetting).all()
    return {p.key: p.value for p in items}


@router.get("/faq")
def get_faq(db: Session = Depends(get_db)):
    items = db.query(FaqItem).filter_by(is_active=True).order_by(FaqItem.sort_order).all()
    return [{"id":f.id,"question":f.question,"answer":f.answer} for f in items]


@router.get("/pricing/exchange-rate")
def get_rate(db: Session = Depends(get_db)):
    rec = db.query(ExchangeRate).filter_by(currency="USD").first()
    return {"usd_to_rub": rec.rate_rub if rec else None}


@router.get("/pricing/token-costs")
def get_token_costs(db: Session = Depends(get_db)):
    rate = get_usd_rate(db)
    return {m: calc_tokens(m, rate) for m in MODEL_USD_COST}


# ═══════════════════════════════════════════════════════════════════════════════
# PROMO CODES
# ═══════════════════════════════════════════════════════════════════════════════

class PromoApplyBody(BaseModel):
    code: str


@router.post("/promo/apply")
def apply_promo(body: PromoApplyBody, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    from sqlalchemy import update as sa_update
    code = db.query(PromoCode).filter_by(code=body.code.upper(), is_active=True).first()
    if not code:
        raise HTTPException(404, "Промокод не найден или неактивен")
    # Check not already used by this user
    used = db.query(PromoUse).filter_by(code_id=code.id, user_id=user.id).first()
    if used:
        raise HTTPException(400, "Промокод уже использован вами")
    # Атомарный increment c защитой от race condition (двое параллельно не превысят max_uses)
    res = db.execute(
        sa_update(PromoCode)
        .where(PromoCode.id == code.id, PromoCode.used_count < code.max_uses)
        .values(used_count=PromoCode.used_count + 1)
    )
    if (res.rowcount or 0) == 0:
        raise HTTPException(400, "Промокод исчерпан")
    db.add(PromoUse(code_id=code.id, user_id=user.id))
    if code.bonus_tokens:
        from server.billing import credit_atomic
        credit_atomic(db, user.id, code.bonus_tokens)
        db.add(Transaction(user_id=user.id, type="bonus", tokens_delta=code.bonus_tokens,
                           description=f"Промокод: {code.code}"))
    db.commit()
    return {"discount_pct": code.discount_pct, "bonus_tokens": code.bonus_tokens,
            "message": f"Промокод применён: {'-'+str(code.discount_pct)+'%' if code.discount_pct else ''} {'+'+str(code.bonus_tokens)+' CH' if code.bonus_tokens else ''}"}
