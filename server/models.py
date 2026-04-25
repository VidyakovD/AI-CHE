from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeDecorator
from server.db import Base
from server.secrets_crypto import encrypt as _enc, decrypt as _dec
from datetime import datetime


class EncryptedString(TypeDecorator):
    """Прозрачное шифрование секретов: чтение → plaintext, запись → enc:v1:..."""
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or value == "":
            return value
        return _enc(value)

    def process_result_value(self, value, dialect):
        if value is None or value == "":
            return value
        return _dec(value)


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
    is_banned        = Column(Boolean, default=False)        # заблокирован по оферте (п. 10.1)
    agreed_to_terms  = Column(Boolean, default=False)
    referral_code    = Column(String, unique=True, nullable=True)
    referred_by      = Column(String, nullable=True)
    oauth_provider   = Column(String, nullable=True)  # google / vk / None (email)
    oauth_sub        = Column(String, nullable=True)  # ID юзера у провайдера
    low_balance_threshold  = Column(Integer, default=100)   # порог уведомления (0 — отключено)
    low_balance_alerted_at = Column(DateTime, nullable=True)  # когда последний раз слали
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
    # UNIQUE защищает от double-spend между /payment/confirm и /payment/webhook.
    yookassa_payment_id= Column(String, nullable=True, unique=True)
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
    # Индекс для быстрой проверки «этот payment уже зачислен?» в webhook
    yookassa_payment_id = Column(String, nullable=True, index=True)
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
    """Стоимость модели — поддерживает per-request и per-token тарификацию."""
    __tablename__ = "model_pricing"

    id           = Column(Integer, primary_key=True)
    model_id     = Column(String, unique=True, nullable=False)
    label        = Column(String, nullable=False)
    # Per-request (legacy, если per-token не заданы):
    cost_per_req = Column(Integer, default=10)
    usd_per_req  = Column(Float, default=0.001)
    markup       = Column(Float, default=3.0)
    # Per-token (новая схема):
    ch_per_1k_input  = Column(Float, default=0.0)   # CH за 1000 input токенов
    ch_per_1k_output = Column(Float, default=0.0)   # CH за 1000 output токенов
    min_ch_per_req   = Column(Integer, default=1)   # минимум CH за запрос
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UsageLog(Base):
    """Логирование каждого вызова AI для статистики."""
    __tablename__ = "usage_logs"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=True)
    model        = Column(String, nullable=False, index=True)
    input_tokens = Column(Integer, default=0)
    output_tokens= Column(Integer, default=0)
    cached_tokens= Column(Integer, default=0)
    ch_charged   = Column(Integer, default=0)
    used_own_key = Column(Boolean, default=False)
    created_at   = Column(DateTime, default=datetime.utcnow, index=True)


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


class FeatureFlag(Base):
    """Флаги включения/выключения модулей системы."""
    __tablename__ = "feature_flags"

    id          = Column(Integer, primary_key=True)
    key         = Column(String, unique=True, nullable=False, index=True)
    enabled     = Column(Boolean, default=True)
    label       = Column(String, nullable=False)       # название для UI
    description = Column(String, nullable=True)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Site Creation ─────────────────────────────────────────────────────────────

class SupportRequest(Base):
    """Обращения пользователей (возврат, удаление данных, жалобы)."""
    __tablename__ = "support_requests"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    type          = Column(String, nullable=False)       # refund / delete_data / complaint
    description   = Column(Text, nullable=True)
    status        = Column(String, default="open")       # open / resolved / rejected
    admin_response= Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="support_requests")


class SiteProject(Base):
    __tablename__ = "site_projects"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=True)
    name         = Column(String, nullable=False)
    status       = Column(String, default="draft")   # draft / has_spec / has_code / done
    spec_text    = Column(Text, nullable=True)
    code_html    = Column(Text, nullable=True)
    price_tokens = Column(Integer, default=0)
    creation_mode = Column(String, default="create_together")  # "have_spec" | "create_together"
    conversation_phase = Column(String, default="idle")  # idle / gathering_spec / spec_ready / collecting_images / generating_code / done
    chat_history = Column(Text, nullable=True)    # JSON: conversation during spec creation
    image_paths  = Column(Text, nullable=True)    # JSON array of uploaded image/logo paths
    hosted_path  = Column(String, nullable=True)  # e.g. "sites/123/" for hosted URL
    attached_bot_id = Column(Integer, ForeignKey("chatbots.id"), nullable=True)  # виджет чат-бота
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="site_projects")


class SiteTemplate(Base):
    """Шаблон для генерации ТЗ и кода сайта."""
    __tablename__ = "site_templates"

    id           = Column(Integer, primary_key=True)
    title        = Column(String, nullable=False)
    description  = Column(String, nullable=True)
    spec_prompt  = Column(Text, nullable=False)       # промпт для формирования ТЗ
    code_prompt  = Column(Text, nullable=False)       # промпт для генерации кода
    input_fields = Column(Text, nullable=True)         # JSON: список полей для ввода
    price_tokens = Column(Integer, default=0)
    is_active    = Column(Boolean, default=True)
    sort_order   = Column(Integer, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)


# ── Presentations & Commercial Proposals ──────────────────────────────────────

class PresentationProject(Base):
    __tablename__ = "presentation_projects"

    id                = Column(Integer, primary_key=True, index=True)
    user_id           = Column(Integer, ForeignKey("users.id"), nullable=True)
    name              = Column(String, nullable=False)
    template_id       = Column(Integer, nullable=True)
    input_data        = Column(Text, nullable=True)          # JSON введённых данных
    generated_content = Column(Text, nullable=True)
    status            = Column(String, default="draft")      # draft / generated / done
    price_tokens      = Column(Integer, default=0)
    image_paths       = Column(Text, nullable=True)          # JSON массив URL картинок
    attached_bot_id   = Column(Integer, ForeignKey("chatbots.id"), nullable=True)  # виджет чат-бота
    created_at        = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", backref="presentation_projects")


class PresentationTemplate(Base):
    """Шаблон для генерации презентаций/КП."""
    __tablename__ = "presentation_templates"

    id           = Column(Integer, primary_key=True)
    title        = Column(String, nullable=False)
    description  = Column(String, nullable=True)
    header_html  = Column(Text, nullable=True)           # шапка оформления
    pricing_json = Column(Text, nullable=True)           # данные о ценах/позициях
    spec_prompt  = Column(Text, nullable=False)           # промпт для генерации
    style_css    = Column(Text, nullable=True)            # CSS стили
    input_fields = Column(Text, nullable=True)            # JSON: список полей
    is_active    = Column(Boolean, default=True)
    sort_order   = Column(Integer, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)


# ── Company Profile (for Presentations & KP) ──────────────────────────────────

class CompanyProfile(Base):
    """Профиль компании пользователя — используется при генерации КП и презентаций."""
    __tablename__ = "company_profiles"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    company_name  = Column(String, nullable=True)
    description   = Column(Text, nullable=True)      # Чем занимается компания
    services      = Column(Text, nullable=True)       # Услуги/товары
    prices        = Column(Text, nullable=True)       # Прайс-лист (свободный текст/таблица)
    style_notes   = Column(Text, nullable=True)       # Стиль оформления, тон, цвета
    contacts      = Column(Text, nullable=True)       # Телефон, email, сайт
    extra         = Column(Text, nullable=True)       # Любые доп. данные (JSON)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="company_profile")


# ── Agent Constructor ──────────────────────────────────────────────────────────

class AgentConfig(Base):
    """Конфигурация ИИ-агента пользователя."""
    __tablename__ = "agent_configs"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    name          = Column(String, default="Мой агент")
    enabled_blocks= Column(Text, nullable=True)   # JSON: список включённых блоков
    channels      = Column(Text, nullable=True)   # JSON: {tg_token, tg_chat_id, discord_token, ...}
    settings      = Column(Text, nullable=True)   # JSON: доп. настройки
    status        = Column(String, default="draft")  # draft / active / paused
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="agent_configs")


# ── Persistent Chatbots ──────────────────────────────────────────────────────

class WorkflowStore(Base):
    """Key-value хранилище для воркфлоу (static data)."""
    __tablename__ = "workflow_store"

    id         = Column(Integer, primary_key=True)
    bot_id     = Column(Integer, index=True, nullable=False)
    key        = Column(String, index=True, nullable=False)
    value      = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class KnowledgeFile(Base):
    """Файл в базе знаний бота: metadata + summary + facts для RAG."""
    __tablename__ = "knowledge_files"

    id           = Column(Integer, primary_key=True)
    bot_id       = Column(Integer, index=True, nullable=False)
    name         = Column(String, nullable=False)
    path         = Column(String, nullable=False)     # /uploads/...
    mime         = Column(String, nullable=True)
    size         = Column(Integer, default=0)
    description  = Column(String, nullable=True)      # одна строка
    tags         = Column(String, nullable=True)      # через запятую
    summary      = Column(Text, nullable=True)        # 2-4 предложения
    facts        = Column(Text, nullable=True)        # через "; "
    content_text = Column(Text, nullable=True)        # полный текст (для fulltext search)
    created_at   = Column(DateTime, default=datetime.utcnow)


class AdminAuditLog(Base):
    """Лог критичных действий админа: баланс, цены, баны, ключи, промокоды."""
    __tablename__ = "admin_audit_log"

    id         = Column(Integer, primary_key=True, index=True)
    admin_id   = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    action     = Column(String, nullable=False, index=True)   # напр. "adjust_balance"
    target_type= Column(String, nullable=True)                # "user" | "apikey" | "promo" | ...
    target_id  = Column(String, nullable=True)                # id объекта (строка чтобы поддержать любые)
    details    = Column(Text, nullable=True)                  # JSON с параметрами (delta, reason, ...)
    ip         = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ImapCredential(Base):
    """Credentials для IMAP (email trigger)."""
    __tablename__ = "imap_credentials"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    label      = Column(String, default="Main")
    host       = Column(String, nullable=False)      # imap.yandex.ru
    port       = Column(Integer, default=993)
    username   = Column(String, nullable=False)
    password   = Column(String, nullable=False)      # шифруется через server.secrets_crypto
    use_ssl    = Column(Boolean, default=True)
    last_uid   = Column(Integer, default=0)          # последний обработанный UID
    created_at = Column(DateTime, default=datetime.utcnow)


class ChatBot(Base):
    """Постоянный бот — слушает входящие и отвечает через AI 24/7."""
    __tablename__ = "chatbots"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False)
    name            = Column(String, default="Мой бот")
    model           = Column(String, default="gpt")
    system_prompt   = Column(Text, nullable=True)
    # Telegram
    tg_token        = Column(EncryptedString, nullable=True)
    tg_webhook_set  = Column(Boolean, default=False)
    # VK
    vk_token        = Column(EncryptedString, nullable=True)
    vk_group_id     = Column(String, nullable=True)
    vk_secret       = Column(EncryptedString, nullable=True)
    vk_confirmation = Column(String, nullable=True)
    vk_confirmed    = Column(Boolean, default=False)
    # Авито
    avito_client_id = Column(String, nullable=True)
    avito_client_secret = Column(EncryptedString, nullable=True)
    avito_user_id   = Column(String, nullable=True)
    # MAX (мессенджер VK group, https://max.ru)
    max_token         = Column(EncryptedString, nullable=True)
    max_webhook_set   = Column(Boolean, default=False)
    # Виджет
    widget_enabled  = Column(Boolean, default=False)
    widget_secret   = Column(EncryptedString, nullable=True)
    # Воркфлоу (JSON граф нод/связей из конструктора)
    workflow_json   = Column(Text, nullable=True)
    # Лимиты
    max_replies_day = Column(Integer, default=100)
    cost_per_reply  = Column(Integer, default=5)
    replies_today   = Column(Integer, default=0)
    replies_reset_at= Column(DateTime, nullable=True)
    # Статус
    status          = Column(String, default="off")  # off / active / paused
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="chatbots")


class UserApiKey(Base):
    """Собственные API-ключи пользователя для AI-провайдеров."""
    __tablename__ = "user_api_keys"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    provider   = Column(String, nullable=False)   # openai | anthropic | gemini | grok
    api_key    = Column(String, nullable=False)   # ключ пользователя
    label      = Column(String, nullable=True)    # название (необязательно)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", backref="api_keys_own")
