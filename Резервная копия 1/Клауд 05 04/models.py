from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from db import Base
from datetime import datetime


class User(Base):
    __tablename__ = "users"

    id               = Column(Integer, primary_key=True, index=True)
    email            = Column(String, unique=True, index=True, nullable=False)
    password_hash    = Column(String, nullable=False)
    name             = Column(String, nullable=True)
    avatar_url       = Column(String, nullable=True)
    tokens_balance   = Column(Integer, default=0)
    is_active        = Column(Boolean, default=True)
    is_verified      = Column(Boolean, default=False)       # email verified
    agreed_to_terms  = Column(Boolean, default=False)
    referral_code    = Column(String, unique=True, nullable=True)
    referred_by      = Column(String, nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)

    messages      = relationship("Message",      back_populates="user")
    subscriptions = relationship("Subscription", back_populates="user")
    transactions  = relationship("Transaction",  back_populates="user")
    verify_tokens = relationship("VerifyToken",  back_populates="user")


class VerifyToken(Base):
    """Email verification & password-reset tokens."""
    __tablename__ = "verify_tokens"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    token      = Column(String, unique=True, index=True, nullable=False)
    purpose    = Column(String, nullable=False)   # "verify_email" | "reset_password"
    used       = Column(Boolean, default=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="verify_tokens")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id                 = Column(Integer, primary_key=True, index=True)
    user_id            = Column(Integer, ForeignKey("users.id"), nullable=False)
    plan               = Column(String, nullable=False)
    tokens_total       = Column(Integer, nullable=False)
    tokens_used        = Column(Integer, default=0)
    price_rub          = Column(Float, nullable=False)
    status             = Column(String, default="active")   # active / expired / cancelled
    yookassa_payment_id= Column(String, nullable=True)
    started_at         = Column(DateTime, default=datetime.utcnow)
    expires_at         = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="subscriptions")


class Transaction(Base):
    __tablename__ = "transactions"

    id                  = Column(Integer, primary_key=True, index=True)
    user_id             = Column(Integer, ForeignKey("users.id"), nullable=False)
    type                = Column(String, nullable=False)      # payment / usage / refund / bonus
    amount_rub          = Column(Float, nullable=True)
    tokens_delta        = Column(Integer, nullable=False)
    description         = Column(String, nullable=True)
    model               = Column(String, nullable=True)
    yookassa_payment_id = Column(String, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="transactions")


class Message(Base):
    __tablename__ = "messages"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=True)
    chat_id     = Column(String, index=True)
    role        = Column(String)
    content     = Column(Text)
    model       = Column(String)
    title       = Column(String, nullable=True)
    tokens_used = Column(Integer, default=0)
    created_at  = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="messages")


# ── Solutions (Готовые решения) ───────────────────────────────────────────────

class SolutionCategory(Base):
    """Категория: 'Промпты' или 'Бизнес-решения'."""
    __tablename__ = "solution_categories"

    id          = Column(Integer, primary_key=True, index=True)
    slug        = Column(String, unique=True, nullable=False)   # "prompts" | "business"
    title       = Column(String, nullable=False)
    sort_order  = Column(Integer, default=0)

    solutions   = relationship("Solution", back_populates="category")


class Solution(Base):
    """Одно готовое решение / промпт-пак."""
    __tablename__ = "solutions"

    id           = Column(Integer, primary_key=True, index=True)
    category_id  = Column(Integer, ForeignKey("solution_categories.id"), nullable=False)
    title        = Column(String, nullable=False)
    description  = Column(Text, nullable=True)
    image_url    = Column(String, nullable=True)
    price_tokens = Column(Integer, default=0)       # стоимость запуска в токенах
    is_active    = Column(Boolean, default=True)
    sort_order   = Column(Integer, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)

    category = relationship("SolutionCategory", back_populates="solutions")
    steps    = relationship("SolutionStep", back_populates="solution",
                            order_by="SolutionStep.step_number")


class SolutionStep(Base):
    """Один шаг цепочки внутри решения."""
    __tablename__ = "solution_steps"

    id            = Column(Integer, primary_key=True, index=True)
    solution_id   = Column(Integer, ForeignKey("solutions.id"), nullable=False)
    step_number   = Column(Integer, nullable=False)
    title         = Column(String, nullable=True)         # заголовок шага для UI
    model         = Column(String, nullable=False)         # какую модель использовать
    system_prompt = Column(Text, nullable=True)            # системный промпт
    user_prompt   = Column(Text, nullable=True)            # шаблон запроса (может содержать {input} {prev_result})
    wait_for_user = Column(Boolean, default=False)         # ждать ввода от пользователя?
    user_hint     = Column(String, nullable=True)          # подсказка что ввести
    extra_params  = Column(Text, nullable=True)            # JSON доп. параметры (для Kling/Veo)

    solution = relationship("Solution", back_populates="steps")


class SolutionRun(Base):
    """Запущенная сессия решения пользователем."""
    __tablename__ = "solution_runs"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=True)
    solution_id  = Column(Integer, ForeignKey("solutions.id"), nullable=False)
    chat_id      = Column(String, nullable=False)           # чат куда пишем результаты
    current_step = Column(Integer, default=0)
    status       = Column(String, default="running")        # running | waiting_input | done | error
    context      = Column(Text, nullable=True)              # JSON накопленный контекст шагов
    created_at   = Column(DateTime, default=datetime.utcnow)


# ── API Keys Management ───────────────────────────────────────────────────────

class ApiKey(Base):
    """Хранение API ключей с статусом."""
    __tablename__ = "api_keys"

    id         = Column(Integer, primary_key=True, index=True)
    provider   = Column(String, nullable=False, index=True)  # openai/anthropic/etc
    key_value  = Column(String, nullable=False)
    label      = Column(String, nullable=True)               # метка (необязательно)
    status     = Column(String, default="unknown")           # ok / error / unknown
    last_error = Column(String, nullable=True)
    last_check = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── Pricing & Settings ────────────────────────────────────────────────────────

class PricingSetting(Base):
    """Глобальные настройки ценообразования."""
    __tablename__ = "pricing_settings"

    id          = Column(Integer, primary_key=True)
    key         = Column(String, unique=True, nullable=False)
    value       = Column(String, nullable=False)
    description = Column(String, nullable=True)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ModelPricing(Base):
    """Стоимость запроса к каждой модели в CH-токенах."""
    __tablename__ = "model_pricing"

    id           = Column(Integer, primary_key=True)
    model_id     = Column(String, unique=True, nullable=False)  # "gpt", "claude", etc.
    label        = Column(String, nullable=False)
    cost_per_req = Column(Integer, default=10)   # CH за один запрос
    usd_per_req  = Column(Float, default=0.001)  # реальная себестоимость в $
    markup       = Column(Float, default=1.5)    # наценка × (итого = usd × markup × курс / ch_rub)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TokenPackage(Base):
    """Пакеты токенов для докупки."""
    __tablename__ = "token_packages"

    id           = Column(Integer, primary_key=True)
    name         = Column(String, nullable=False)
    tokens       = Column(Integer, nullable=False)
    price_rub    = Column(Float, nullable=False)
    is_active    = Column(Boolean, default=True)
    sort_order   = Column(Integer, default=0)


class FaqItem(Base):
    """Частые вопросы."""
    __tablename__ = "faq_items"

    id         = Column(Integer, primary_key=True)
    question   = Column(String, nullable=False)
    answer     = Column(Text, nullable=False)
    sort_order = Column(Integer, default=0)
    is_active  = Column(Boolean, default=True)


# ── Promo codes ───────────────────────────────────────────────────────────────

class PromoCode(Base):
    __tablename__ = "promo_codes"

    id          = Column(Integer, primary_key=True)
    code        = Column(String, unique=True, nullable=False, index=True)
    discount_pct= Column(Integer, default=10)   # % скидка (10 = 10%)
    bonus_tokens= Column(Integer, default=0)     # или бонусные токены
    max_uses    = Column(Integer, default=100)
    used_count  = Column(Integer, default=0)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


class PromoUse(Base):
    __tablename__ = "promo_uses"

    id         = Column(Integer, primary_key=True)
    code_id    = Column(Integer, nullable=False)
    user_id    = Column(Integer, nullable=False)
    used_at    = Column(DateTime, default=datetime.utcnow)


# ── Exchange rate cache ───────────────────────────────────────────────────────

class ExchangeRate(Base):
    __tablename__ = "exchange_rates"

    id         = Column(Integer, primary_key=True)
    currency   = Column(String, default="USD")     # USD
    rate_rub   = Column(Float, default=90.0)       # 1 USD = rate_rub RUB
    updated_at = Column(DateTime, default=datetime.utcnow)
