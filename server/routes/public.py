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
    {"model_id":"gpt",             "label":"GPT-4o mini",     "cost_per_req":5,   "usd_per_req":0.0001,"markup":2.0},
    {"model_id":"gpt-4o",          "label":"GPT-4o",           "cost_per_req":20,  "usd_per_req":0.005, "markup":1.8},
    {"model_id":"claude",          "label":"Claude Haiku",     "cost_per_req":8,   "usd_per_req":0.0002,"markup":1.8},
    {"model_id":"claude-sonnet",   "label":"Claude Sonnet",    "cost_per_req":25,  "usd_per_req":0.006, "markup":1.8},
    {"model_id":"perplexity",      "label":"Perplexity Small", "cost_per_req":6,   "usd_per_req":0.0002,"markup":1.8},
    {"model_id":"perplexity-large","label":"Perplexity Large", "cost_per_req":15,  "usd_per_req":0.001, "markup":1.8},
    {"model_id":"nano",            "label":"Imagen",           "cost_per_req":3,   "usd_per_req":0.0001,"markup":2.0},
    {"model_id":"kling",           "label":"Kling v1",         "cost_per_req":200, "usd_per_req":0.14,  "markup":1.5},
    {"model_id":"kling-pro",       "label":"Kling Pro",        "cost_per_req":400, "usd_per_req":0.28,  "markup":1.5},
    {"model_id":"veo",             "label":"Veo 3",            "cost_per_req":300, "usd_per_req":0.20,  "markup":1.5},
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
    code = db.query(PromoCode).filter_by(code=body.code.upper(), is_active=True).first()
    if not code:
        raise HTTPException(404, "Промокод не найден или неактивен")
    if code.used_count >= code.max_uses:
        raise HTTPException(400, "Промокод исчерпан")
    # Check not already used by this user
    used = db.query(PromoUse).filter_by(code_id=code.id, user_id=user.id).first()
    if used:
        raise HTTPException(400, "Промокод уже использован вами")
    # Apply
    code.used_count += 1
    db.add(PromoUse(code_id=code.id, user_id=user.id))
    if code.bonus_tokens:
        db.query(User).filter_by(id=user.id).first().tokens_balance += code.bonus_tokens
        db.add(Transaction(user_id=user.id, type="bonus", tokens_delta=code.bonus_tokens,
                           description=f"Промокод: {code.code}"))
    db.commit()
    return {"discount_pct": code.discount_pct, "bonus_tokens": code.bonus_tokens,
            "message": f"Промокод применён: {'-'+str(code.discount_pct)+'%' if code.discount_pct else ''} {'+'+str(code.bonus_tokens)+' CH' if code.bonus_tokens else ''}"}
