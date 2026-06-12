from sqlalchemy import Column, Integer, BigInteger, String, Text, Float, Boolean, DateTime, ForeignKey, UniqueConstraint, Index
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
    # Согласие на маркетинговую рассылку — отдельно от agreed_to_terms.
    # 152-ФЗ требует, чтобы согласие на обработку ПДн для рассылки было
    # выраженным (предзаполнять нельзя). Юзер может отозвать → False.
    marketing_consent    = Column(Boolean, default=False)
    marketing_consent_at = Column(DateTime, nullable=True)
    referral_code    = Column(String, unique=True, nullable=True)
    referred_by      = Column(String, nullable=True)
    oauth_provider   = Column(String, nullable=True)  # google / vk / None (email)
    oauth_sub        = Column(String, nullable=True)  # ID юзера у провайдера
    low_balance_threshold  = Column(Integer, default=100)   # порог уведомления (0 — отключено)
    low_balance_alerted_at = Column(DateTime, nullable=True)  # когда последний раз слали
    # Однократность приветственного и реферального бонуса (atomic-gates).
    welcome_bonus_claimed_at      = Column(DateTime, nullable=True)
    referral_signup_bonus_paid_at = Column(DateTime, nullable=True)
    # Лимит дочерних ботов через AI-конструктор (защита от runaway).
    max_auto_bots    = Column(Integer, default=5)
    # Уведомления о входе с нового IP (security alert).
    last_login_ip    = Column(String, nullable=True)
    last_login_at    = Column(DateTime, nullable=True)
    # Refresh-token rotation single-use: JSON-список активных jti (до 10).
    # Управляется server.auth.register/revoke_refresh_jti — украденный
    # refresh не сработает после законной ротации, jti удаляется при rotate.
    refresh_jtis     = Column(Text, nullable=True)
    # Привязка Telegram-аккаунта (для TG-бота управления + push)
    tg_user_id       = Column(String, unique=True, nullable=True, index=True)
    tg_username      = Column(String, nullable=True)
    tg_link_code     = Column(String, nullable=True, index=True)        # одноразовый код привязки
    tg_link_expires  = Column(DateTime, nullable=True)
    tg_notify_proposals = Column(Boolean, default=True)   # уведомлять о новых КП
    tg_notify_records   = Column(Boolean, default=True)   # о новых заявках
    tg_notify_errors    = Column(Boolean, default=True)   # об ошибках/refund'ах
    # Привязка MAX-аккаунта (российская альтернатива Telegram). Симметрично TG:
    # отдельный bot создаётся в @MasterBot (MAX-эквивалент BotFather), токен
    # лежит в env MAX_MGMT_BOT_TOKEN. Юзер привязывает через /link или deep-link.
    max_user_id      = Column(String, unique=True, nullable=True, index=True)
    max_username     = Column(String, nullable=True)
    max_link_code    = Column(String, nullable=True, index=True)
    max_link_expires = Column(DateTime, nullable=True)
    # ── Personal mgmt-боты (модель «юзер подключает свой бот», 2026-05-22) ──
    # Каждый юзер создаёт свой бот в @BotFather → токен сохраняется здесь
    # (EncryptedString, шифруется HKDF от JWT_SECRET). Webhook идёт на наш
    # /webhook/personal-tg/<sha256(JWT+token)[:24]> → находим юзера по hash.
    # Заменяет (постепенно) link-code flow с общим TG_MGMT_BOT_TOKEN.
    #
    # personal_tg_chat_id — куда слать ответы Че. Заполняется при первом
    # /start юзера своему боту (берём `from.id` из update).
    personal_tg_bot_token    = Column(EncryptedString(1024), nullable=True)
    personal_tg_bot_username = Column(String, nullable=True)
    personal_tg_bot_token_hash = Column(String, unique=True, nullable=True, index=True)
    personal_tg_chat_id      = Column(String, nullable=True)
    personal_tg_webhook_set  = Column(Boolean, default=False)
    # MAX-аналог
    personal_max_bot_token    = Column(EncryptedString(1024), nullable=True)
    personal_max_bot_username = Column(String, nullable=True)
    personal_max_bot_token_hash = Column(String, unique=True, nullable=True, index=True)
    personal_max_user_id      = Column(String, nullable=True)
    personal_max_webhook_set  = Column(Boolean, default=False)
    # VK Community bot — для юзеров кто общается с Че через сообщения в группу ВК.
    # access_token берётся в настройках сообщества с правами `messages`+`manage`.
    # Webhook ставится через groups.setCallbackSettings на /webhook/personal-vk/<hash>.
    # group_id нужен для отправки ответов через messages.send.
    # confirmation_code нужен на первом VK callback (ВК присылает type='confirmation',
    # ждёт от нас в ответ строку из groups.getCallbackConfirmationCode).
    personal_vk_bot_token        = Column(EncryptedString(1024), nullable=True)
    personal_vk_group_id         = Column(String, nullable=True)         # положительное число
    personal_vk_group_name       = Column(String, nullable=True)
    personal_vk_bot_token_hash   = Column(String, unique=True, nullable=True, index=True)
    personal_vk_confirmation     = Column(String, nullable=True)         # код для ответа на VK confirmation callback
    personal_vk_webhook_set      = Column(Boolean, default=False)
    # peer_id юзера в личке VK community-bot. Заполняется при первом message_new
    # событии от юзера → даёт возможность бэк-пушить в VK (autoresponder cron).
    # Без него community-bot не знает куда слать первое уведомление.
    personal_vk_chat_peer_id     = Column(String, nullable=True)
    # In-app колокольчик уведомлений: всё новее этого таймстампа = непрочитано.
    # Устанавливается клиентом через POST /user/notifications/seen.
    notifications_last_seen_at = Column(DateTime, nullable=True)
    # Welcome-tour: показывали ли юзеру 4-шаговый онбординг при первом входе.
    onboarding_completed = Column(Boolean, default=False)
    # 2FA через TOTP (Google Authenticator / Authy / 1Password).
    # totp_secret хранится в EncryptedString — на лету шифруется/расшифровывается.
    # Только админам — обычным юзерам не предлагаем 2FA пока что.
    totp_secret  = Column(EncryptedString, nullable=True)
    totp_enabled = Column(Boolean, default=False)
    # ── Multi-surface identity (2026-06-01) ──────────────────────────────────
    # Единый аккаунт на 4 поверхности: web + VK MiniApp + TG bot + MAX bot.
    # См. server/internal_api.py.
    #
    # phone — нормализованный +7XXXXXXXXXX. Используется для auto-link при
    # первом контакте через TG/MAX-бот (юзер делает share-contact). UNIQUE на
    # стороне приложения (проверка в /internal/v1/identify), индекс БД для
    # быстрого lookup.
    phone        = Column(String(20), nullable=True, index=True)
    # vk_user_id — прямая колонка для быстрого индекса от VK MiniApp.
    # Дублирует oauth_sub когда oauth_provider='vk'. BigInteger потому что
    # VK ID может быть до 10^10.
    vk_user_id   = Column(BigInteger, nullable=True, index=True)
    # Единый trial для всех поверхностей. NULL = trial кончился / не было.
    trial_ends_at = Column(DateTime, nullable=True)
    # @aiche_bot — модель чата по умолчанию. NULL = первая в CHAT_MODELS.
    tg_default_chat_model = Column(String, nullable=True)
    # 152-ФЗ — согласие на обработку ПД. NULL = не дал. Используется VK MiniApp
    # surface (ConsentGate panel показывает чекбокс при первом входе).
    pd_consent_at = Column(DateTime, nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)

    messages      = relationship("Message",      back_populates="user")
    subscriptions = relationship("Subscription", back_populates="user")
    transactions  = relationship("Transaction",  back_populates="user")
    verify_tokens = relationship("VerifyToken",  back_populates="user")


class VerifyToken(Base):
    """Email verification & password-reset tokens."""
    __tablename__ = "verify_tokens"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token      = Column(String, unique=True, index=True, nullable=False)
    purpose    = Column(String, nullable=False)   # "verify_email" | "reset_password"
    used       = Column(Boolean, default=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="verify_tokens")


class OAuthState(Base):
    """
    CSRF-state параметр для OAuth флоу. Создаётся в /start, потребляется в /callback.
    Отдельная таблица т.к. user_id неизвестен до завершения флоу.

    code_verifier — для PKCE (нужно VK ID). У Google не используется (None).
    Хранится только до consume_state, потом строка помечается used=True.
    """
    __tablename__ = "oauth_states"

    id            = Column(Integer, primary_key=True, index=True)
    state         = Column(String, unique=True, index=True, nullable=False)
    provider      = Column(String, nullable=False)   # "google" | "vk"
    # EncryptedString: PKCE verifier — секрет, если БД утечёт во время активной
    # OAuth-сессии (~5 мин), атакующий мог бы перехватить token-exchange.
    # Старые plain-text записи остаются читаемыми (secrets_crypto.decrypt
    # пропускает значения без `enc:`-префикса).
    code_verifier = Column(EncryptedString(256), nullable=True)
    used          = Column(Boolean, default=False)
    expires_at    = Column(DateTime, nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)


class AssistantFeedback(Base):
    """
    Запись каждого вопроса к контекстному помощнику + автоклассификация
    через AI на типы: question / confusion / complaint / idea / praise.

    Используется владельцем платформы (или Claude в чате с разработчиком)
    для понимания болей юзеров: какие вопросы повторяются, что непонятно,
    какие функции просят добавить.

    Похожие вопросы кластеризуются по embedding'у — top-N жалоб одного типа
    показываются в admin-панели как один кейс с count и примерами.
    """
    __tablename__ = "assistant_feedback"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    section         = Column(String, nullable=True, index=True)
    message         = Column(Text, nullable=False)              # вопрос юзера
    ai_answer       = Column(Text, nullable=True)               # краткий ответ помощника (обрезанный)
    classification  = Column(String, default="question", index=True)  # question | confusion | complaint | idea | praise
    confidence      = Column(Integer, default=0)                # 0..100, уверенность классификатора
    user_mark       = Column(String, nullable=True)             # None | up | down | idea (явная оценка юзера)
    embedding_json  = Column(Text, nullable=True)               # для кластеризации похожих
    is_resolved     = Column(Boolean, default=False, index=True)
    resolved_note   = Column(Text, nullable=True)               # пометка владельца «исправлено в коммите X»
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)


class QrLoginSession(Base):
    """
    QR-логин: один экран показывает QR с токеном, другой (мобильный) сканирует
    его и подтверждает вход. Десктоп polls'ит/слушает WS до approval.

    status: pending → approved (юзер подтвердил с мобилки) или cancelled.
    После approved токены отдаются ОДИН раз через /qr-login/poll и сессия
    помечается consumed=True.
    """
    __tablename__ = "qr_login_sessions"

    id            = Column(Integer, primary_key=True, index=True)
    token         = Column(String, unique=True, index=True, nullable=False)
    status        = Column(String, default="pending")  # pending | approved | cancelled
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=True)
    consumed      = Column(Boolean, default=False)  # десктоп уже забрал токены
    init_ip       = Column(String, nullable=True)
    init_ua       = Column(String, nullable=True)
    approve_ip    = Column(String, nullable=True)
    approve_ua    = Column(String, nullable=True)
    expires_at    = Column(DateTime, nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)
    approved_at   = Column(DateTime, nullable=True)


class PricingConfig(Base):
    """
    Динамические цены, изменяемые через админку без редеплоя.
    Ключи: site.standard, site.premium, site.iter, site.spec
    Значения в копейках.
    """
    __tablename__ = "pricing_config"

    key        = Column(String, primary_key=True)         # "site.standard"
    value_kop  = Column(Integer, nullable=False)
    label      = Column(String, nullable=True)            # "Создание сайта (Sonnet)"
    updated_at = Column(DateTime, default=datetime.utcnow,
                        onupdate=datetime.utcnow)


class StoredAsset(Base):
    """
    Файлы юзеров, хранящиеся на сервере (лидмагниты PDF, картинки, видео).

    Тариф: фикс ₽/месяц за каждые 100 МБ. Биллинг считает суммарный объём
    активных файлов юзера и списывает в начале месяца. См. server/pricing.py:
    storage.per_100mb_month — базовая ставка.

    Файлы доступны через /uploads/ если public_token=None или
    /assets/{public_token}/file.ext — короткий URL для лидмагнита в боте.
    """
    __tablename__ = "stored_assets"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    bot_id        = Column(Integer, ForeignKey("chatbots.id", ondelete="SET NULL"),
                            nullable=True, index=True)
    name          = Column(String, nullable=False)        # человеческое имя файла
    path          = Column(String, nullable=False)        # /uploads/assets/<uuid>.<ext>
    mime_type     = Column(String, nullable=True)
    size_bytes    = Column(Integer, nullable=False, default=0)
    public_token  = Column(String, unique=True, index=True, nullable=True)
    purpose       = Column(String, nullable=True)         # "lead_magnet" | "general" | ...
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow, index=True)
    last_billed_at = Column(DateTime, nullable=True)      # последняя дата списания за storage


class Subscription(Base):
    __tablename__ = "subscriptions"

    id                 = Column(Integer, primary_key=True, index=True)
    user_id            = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
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
    user_id             = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type                = Column(String, nullable=False)      # payment / usage / refund / bonus
    amount_rub          = Column(Float, nullable=True)
    tokens_delta        = Column(Integer, nullable=False)
    description         = Column(String, nullable=True)
    model               = Column(String, nullable=True)
    # UNIQUE — защита от двойного зачисления при гонке webhook + confirm-tokens.
    # На SQLite/PG индекс уже создаётся через LIGHTWEIGHT_INDEXES
    # (uq_transactions_yookassa_id, partial WHERE NOT NULL). Здесь
    # `unique=True` отражает реальный constraint и делает поведение
    # SQLAlchemy предсказуемым (IntegrityError на дубле).
    yookassa_payment_id = Column(String, nullable=True, unique=True)
    created_at          = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="transactions")


class Message(Base):
    __tablename__ = "messages"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
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
    """Одно готовое решение / промпт-пак.

    Два режима выполнения:
      1. Legacy «plain»: SolutionStep'ы выполняются последовательно, каждый
         шаг = один LLM-вызов (см. routes/solutions._execute_step).
      2. Multi-agent «orchestra»: orchestra_json содержит граф stage'ов
         (web_search / browse_url / llm / parallel_llm / synthesize) — несколько
         специализированных агентов работают параллельно/последовательно
         (см. server/solutions_orchestra.py).
    """
    __tablename__ = "solutions"

    id           = Column(Integer, primary_key=True, index=True)
    category_id  = Column(Integer, ForeignKey("solution_categories.id"), nullable=False)
    title        = Column(String, nullable=False)
    description  = Column(Text, nullable=True)
    image_url    = Column(String, nullable=True)
    price_tokens = Column(Integer, default=0)       # стоимость запуска в токенах
    is_active    = Column(Boolean, default=True)
    sort_order   = Column(Integer, default=0)
    # Multi-agent оркестрация. Если задан — выполняется через
    # solutions_orchestra.run_orchestra. Иначе — legacy SolutionStep flow.
    # Структура JSON: {"stages": [{...}, ...], "input_hint": "...",
    #                  "default_model": "claude-sonnet"}
    orchestra_json = Column(Text, nullable=True)
    # Подкатегория для фильтрации в UI: research / marketing / legal /
    # finance / strategy / hr / sales. Свободный enum (string), индексируем
    # для быстрого WHERE.
    subcategory  = Column(String, nullable=True, index=True)
    # Теги CSV: "perplexity,deep,новинка,хит" — используется для бейджей и
    # дополнительной фильтрации в UI.
    tags         = Column(String, nullable=True)
    # Топ-1 строка над общей сеткой («⭐ Топ новинки»).
    is_featured  = Column(Boolean, default=False, index=True)
    # Краткое описание (1 строка) под названием в карточке. У description
    # обычно длинный текст — short_summary даёт 6-10 слов для UI.
    short_summary = Column(String, nullable=True)
    # Схема полей ввода — JSON-массив объектов:
    #   [{"name":"product","label":"Продукт","type":"text","required":true,
    #     "hint":"...", "placeholder":"...", "options":[...] (для select)}, ...]
    # Если задан — UI рендерит набор полей вместо одной textarea, бэкенд
    # подставляет значения через {field_name} в промптах stage'ов и steps.
    # Если null — fallback на эвристику парсинга hint (одна textarea).
    input_schema_json = Column(Text, nullable=True)
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
    """Запущенная сессия решения пользователем.

    Поддерживает два режима:
      - legacy plain: current_step указывает на текущий SolutionStep, status:
        running | waiting_input | done | error
      - multi-agent orchestra: stages_state хранит JSON с состоянием каждого
        stage'а (status/output/cost/error/timestamps), final_output — итог.
    """
    __tablename__ = "solution_runs"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    solution_id  = Column(Integer, ForeignKey("solutions.id"), nullable=False)
    chat_id      = Column(String, nullable=False)           # чат куда пишем результаты
    current_step = Column(Integer, default=0)
    status       = Column(String, default="running")        # running | waiting_input | done | error
    context      = Column(Text, nullable=True)              # JSON накопленный контекст шагов
    # Multi-agent режим: дамп состояния всех stage'ов для UI/SSE-стрима.
    # Формат: {"stages": {<id>: {"status","output","cost_kop","error",
    #                            "started_at","finished_at","label"}}, ...}
    stages_state = Column(Text, nullable=True)
    final_output = Column(Text, nullable=True)              # markdown итогового отчёта
    pdf_path     = Column(String, nullable=True)            # путь сгенерированного PDF
    total_cost_kop = Column(Integer, default=0)             # суммарно списано
    user_input   = Column(Text, nullable=True)              # исходный {input} для повтора
    # Загруженные файлы (PDF договора / скриншоты / Excel и т.п.) — JSON массив
    # объектов {file_url, name, mime, kind:"doc"|"image"|"sheet"|"other", size}.
    # Используется stage'ами file_extract / vision_describe.
    attachments_json = Column(Text, nullable=True)
    # Шаринг: unguessable token для публичной ссылки на результат.
    public_token = Column(String, unique=True, nullable=True, index=True)
    # Реакция юзера на финальный отчёт (👍/👎/💡 + опц. комментарий).
    # Помогает выявить плохие пайплайны для админ-агрегации.
    user_mark    = Column(String, nullable=True)   # up | down | idea | None
    user_comment = Column(Text, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)


class ApiToken(Base):
    """Public API токен для SaaS-интеграций (machine-to-machine).

    Юзер генерирует в кабинете → встраивает в свой external service →
    вызывает endpoints через `Authorization: Bearer ai_che_<token>`.
    Списания за вызовы идут с баланса юзера (тот же tokens_balance).

    Scope-флаги: какие группы endpoints доступны для этого токена. Если
    проставлены NULL/all — доступны все. Иначе CSV строка из:
    "proposals,knowledge,solutions,bots". Удобно если юзер хочет токен
    только для интеграции одного модуля.

    Хранение: prefix (видимый часть, ai_che_xxxxx) + sha256 от секрета.
    Секрет показываем юзеру ОДИН РАЗ при создании, потом только prefix.
    """
    __tablename__ = "api_tokens"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    name          = Column(String, nullable=False)   # «my-saas-integration»
    prefix        = Column(String, nullable=False, unique=True, index=True)
    # SHA-256 hex от полного секрета. Сравнение через hmac.compare_digest.
    secret_hash   = Column(String, nullable=False)
    scopes        = Column(String, nullable=True)    # CSV или NULL=all
    last_used_at  = Column(DateTime, nullable=True)
    requests_count = Column(Integer, default=0)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)


class CrmConnection(Base):
    """CRM-интеграция юзера (Bitrix24, amoCRM, generic webhook).

    При срабатывании события `record.created` (новая заявка из чат-бота)
    мы автоматически постим лид в CRM юзера. Это закрывает 80% случаев
    «синхронизация заявок с моей CRM» без необходимости разработчика.

    Bitrix24:
      - Юзер заходит в свою Bitrix24 → Разработчикам → Webhooks → Создать
        входящий webhook → выбрать crm.lead.add разрешение → копирует URL
      - Вставляет URL в нашу CRM-форму (provider="bitrix24")
      - Мы при record.created делаем POST на URL с CRM-полями

    amoCRM:
      - Сложнее (OAuth2). Простой путь: использовать generic webhook где
        юзер сам пишет URL принимающий JSON. amoCRM имеет «Вебхуки» в
        настройках интеграции.

    field_mapping_json — JSON {"наше_поле": "поле_crm"}, например:
      {"customer_name": "TITLE", "customer_phone": "PHONE",
       "bot_name": "SOURCE_DESCRIPTION"}
    Если NULL → используем дефолтный mapping из crm.py.

    secret — для HMAC-подписи (если CRM поддерживает) — опциональный.
    """
    __tablename__ = "crm_connections"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    provider     = Column(String, nullable=False)  # bitrix24 / amocrm / webhook
    name         = Column(String, nullable=True)   # «Главная воронка»
    webhook_url  = Column(EncryptedString, nullable=False)
    # HMAC secret для подписи outbound payload — генерируется при создании
    # generic-webhook и показывается юзеру 1 раз, чтобы он мог настроить
    # receiver-валидацию. NULL допустим (legacy без подписи).
    webhook_secret = Column(EncryptedString, nullable=True)
    field_mapping_json = Column(Text, nullable=True)
    is_active    = Column(Boolean, default=True, index=True)
    last_status  = Column(Integer, nullable=True)
    last_called_at = Column(DateTime, nullable=True)
    last_error   = Column(String, nullable=True)
    fail_count   = Column(Integer, default=0)
    total_calls  = Column(Integer, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)


class IdempotencyRecord(Base):
    """Cross-worker idempotency для /message и других чувствительных к
    дублированию endpoint'ов.

    Раньше кэш был in-memory (per-process) — при multi-worker uvicorn
    двойной клик мог попасть на разные воркеры и пройти оба → двойное
    списание. Теперь UNIQUE(user_id, key) в БД гарантирует one-and-only-one.

    Cleanup: запись живёт 5 минут (stored_at + TTL). После — удаляется
    в scheduler.py loop. Записи длиннее этого = stale, игнорируются.

    Защита от размера: response_json TEXT, лимит ~50 КБ на caller'е.
    """
    __tablename__ = "idempotency_records"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    key          = Column(String, nullable=False, index=True)
    response_json = Column(Text, nullable=True)            # сериализованный response
    created_at   = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint('user_id', 'key', name='uq_idempotency_user_key'),
    )


class OrchestraSchedule(Base):
    """Пользовательское расписание автоматического запуска orchestra-решения.

    Сценарий: юзер хочет «каждый понедельник в 09:00 запусти конкурентный
    анализ моей ниши и пришли отчёт на email». Сохраняет user_input +
    attachments + cron-expression. scheduler.py раз в минуту проверяет
    next_run_at и стартует orchestra.

    Frequency формат — simple presets (НЕ полный cron):
      - 'daily_09'    → каждый день 09:00
      - 'weekly_mon_09' → каждый понедельник 09:00
      - 'weekly_fri_18' → каждую пятницу 18:00
      - 'monthly_1_09'  → 1-го числа 09:00
      - 'monthly_15_09' → 15-го числа 09:00

    После запуска:
      - last_run_at = now
      - next_run_at = вычисляется по frequency
      - запускается run_orchestra(...) в фоне
      - email + push владельцу когда отчёт готов (через стандартные хуки)
      - run учитывается в audit-логе как обычный orchestra-старт
    """
    __tablename__ = "orchestra_schedules"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    solution_id = Column(Integer, ForeignKey("solutions.id", ondelete="CASCADE"),
                          nullable=False)
    name        = Column(String, nullable=True)            # юзерское название
    user_input  = Column(Text, nullable=False)             # будет передан как input
    attachments_json = Column(Text, nullable=True)         # JSON массив attachments
    frequency   = Column(String, nullable=False)           # daily_09 / weekly_mon_09 / ...
    is_active   = Column(Boolean, default=True, index=True)
    last_run_at = Column(DateTime, nullable=True)
    last_run_id = Column(Integer, nullable=True)           # ID последнего SolutionRun
    next_run_at = Column(DateTime, nullable=True, index=True)
    total_runs  = Column(Integer, default=0)
    created_at  = Column(DateTime, default=datetime.utcnow)


class ApiWebhook(Base):
    """Webhook для Public API — юзер регистрирует свой URL и подписывается
    на события. Когда событие происходит, мы делаем POST на URL с JSON-payload
    + HMAC-подписью (заголовок X-Aiche-Signature).

    События (events CSV):
      - proposal.opened       — клиент открыл КП по public-link
      - proposal.sent         — отправлено клиенту по email
      - record.created        — новая заявка из бота
      - solution.done         — orchestra-отчёт готов
      - site.done             — сайт сгенерирован
      - site.failed           — генерация сайта провалилась

    Подпись: `X-Aiche-Signature: sha256=<hex>` где hex = HMAC-SHA256
    body-bytes по secret. Юзер сохраняет secret при создании, верифицирует
    каждый incoming POST.

    Retry: при 5xx или таймауте автоматически 3 попытки с backoff (10s/60s/5m).
    После 3 фейлов подряд `fail_count >= 10` → автоматически is_active=False.
    """
    __tablename__ = "api_webhooks"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    url           = Column(String, nullable=False)            # https://your.com/hook
    # HMAC-секрет — шифруем через EncryptedString (HKDF от JWT_SECRET).
    # decrypt() handles legacy plaintext: новые записи enc:v1:..., старые
    # читаются как есть. Запустить /admin/reencrypt-secrets чтобы зашифровать
    # существующие plaintext-значения.
    secret        = Column(EncryptedString, nullable=False)   # 32-hex для HMAC
    events        = Column(String, nullable=False)            # CSV событий
    description   = Column(String, nullable=True)             # «my-bitrix-sync»
    is_active     = Column(Boolean, default=True, index=True)
    last_status   = Column(Integer, nullable=True)            # HTTP code последнего вызова
    last_called_at = Column(DateTime, nullable=True)
    last_error    = Column(String, nullable=True)             # текст последней ошибки
    fail_count    = Column(Integer, default=0)                # подряд провалов
    total_calls   = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)


class PushSubscription(Base):
    """Web Push (VAPID) — подписка браузера юзера на push-уведомления.

    Браузер регистрирует через service worker и шлёт PushSubscription JSON
    на /user/push/subscribe. Мы храним endpoint+ключи и шлём через pywebpush
    при событиях (новая заявка из бота / клиент открыл КП / низкий баланс).

    У одного юзера может быть несколько подписок (разные устройства).
    Если push приводит к 410 Gone — удаляем подписку как устаревшую.
    """
    __tablename__ = "push_subscriptions"

    id        = Column(Integer, primary_key=True, index=True)
    user_id   = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    endpoint  = Column(String, nullable=False, unique=True)
    p256dh    = Column(String, nullable=False)   # ECDH public key (не секрет, для шифрования push)
    # auth secret — общий ключ браузера и сервера для аутентификации push.
    # Шифруется через EncryptedString. Legacy plaintext продолжит читаться.
    auth      = Column(EncryptedString, nullable=False)
    user_agent = Column(String, nullable=True)   # для UI «удалить с этого устройства»
    created_at = Column(DateTime, default=datetime.utcnow)


class SolutionRunTemplate(Base):
    """Сохранённый запуск как шаблон.

    Юзер запустил «Аудит лендинга» с конкретным URL/скриншотом, доволен
    результатом — сохранил «как шаблон» с именем «Мой основной сайт».
    Через неделю одним кликом запускает заново (например после правок
    лендинга) — input + attachments берутся из шаблона.

    Привязан к solution_id — нельзя применить шаблон одного решения
    к другому (разная orchestra-структура).
    """
    __tablename__ = "solution_run_templates"

    id             = Column(Integer, primary_key=True, index=True)
    user_id        = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    solution_id    = Column(Integer, ForeignKey("solutions.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    name           = Column(String, nullable=False)
    user_input     = Column(Text, nullable=True)
    attachments_json = Column(Text, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)


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


class UserCostAccumulator(Base):
    """Накопитель дробных копеек юзера.

    Цены моделей теперь = real_USD × курс × ×3, что даёт дробные копейки
    (например 0.0042 коп/1k токенов). Минимум списания в БД — 1 коп
    (Integer). Чтобы не терять микро-стоимости коротких запросов, дробная
    часть копится здесь и при накоплении ≥ 1 коп переносится в основной
    баланс.

    Пример:
      - Запрос 1: real cost = 0.6 коп → списываем 0 коп, acc=0.6
      - Запрос 2: real cost = 0.7 коп → списываем 1 коп, acc=0.3 (0.6+0.7=1.3)
      - Запрос 3: real cost = 0.8 коп → списываем 1 коп, acc=0.1

    Каждый юзер — одна строка (UNIQUE на user_id).
    """
    __tablename__ = "user_cost_accumulators"

    id          = Column(Integer, primary_key=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                          nullable=False, unique=True, index=True)
    fractional_kop = Column(Float, default=0.0)   # 0.0 ≤ x < 1.0
    last_updated   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UsageLog(Base):
    """Логирование каждого вызова AI для статистики."""
    __tablename__ = "usage_logs"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
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
    # FK с CASCADE — orphan'ы при удалении промокода/юзера не остаются.
    # Существующая БД через LIGHTWEIGHT_INDEXES уже имеет uq_promo_uses_code_user
    # UNIQUE constraint — Column-уровень здесь только декларация для ORM.
    code_id    = Column(Integer, ForeignKey("promo_codes.id", ondelete="CASCADE"), nullable=False)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    used_at    = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("code_id", "user_id", name="uq_promo_uses_code_user_model"),
    )


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
    user_id       = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
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
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    name         = Column(String, nullable=False)
    status       = Column(String, default="draft")   # draft / has_spec / has_code / done
    spec_text    = Column(Text, nullable=True)
    code_html    = Column(Text, nullable=True)
    price_tokens = Column(Integer, default=0)
    creation_mode = Column(String, default="create_together")  # "have_spec" | "create_together"
    conversation_phase = Column(String, default="idle")  # idle / gathering_spec / spec_ready / collecting_images / generating_code / done
    chat_history = Column(Text, nullable=True)    # JSON: conversation during spec creation
    image_paths  = Column(Text, nullable=True)    # JSON array of uploaded image/logo paths
    hosted_path  = Column(String, nullable=True)  # e.g. "sites/abc123.../" for hosted URL
    # Unguessable публичный токен — раньше hosted URL содержал sequential
    # project.id, что позволяло перебором найти и скачать любой чужой
    # опубликованный сайт. Теперь URL вида /sites/hosted/{public_token}/.
    # Generates on first /host call (см. routes/sites.py).
    public_token = Column(String, unique=True, nullable=True, index=True)
    attached_bot_id = Column(Integer, ForeignKey("chatbots.id"), nullable=True)  # виджет чат-бота
    # Фоновая генерация: статус и прогресс live-генерации (фронт polling'ит)
    gen_status   = Column(String, nullable=True)  # idle | running | done | failed
    gen_started_at = Column(DateTime, nullable=True)
    gen_progress = Column(String, nullable=True)  # «Улучшаю ТЗ…» / «Claude думает (1/3)…» / «Готово»
    gen_error    = Column(Text, nullable=True)    # текст ошибки если failed
    enhanced_spec = Column(Text, nullable=True)   # обогащённое ТЗ из GPT-4o (для дебага)
    # Платный хостинг (Шаг 5): юзер платит разово при первой публикации.
    paid_hosting_at  = Column(DateTime, nullable=True)
    paid_hosting_kop = Column(Integer, default=0)
    # SEO meta — embed'ятся в hosted index.html
    seo_title        = Column(String, nullable=True)
    seo_description  = Column(String, nullable=True)
    seo_og_image     = Column(String, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="site_projects")


class SiteChatWidget(Base):
    """Чат-виджет на опубликованном сайте.

    Два режима (юзер выбирает в /sites.html при подключении):
      - mode='relay': клиентские сообщения через сайт идут в личный TG/MAX
        юзера, он отвечает сам как живой человек.
      - mode='ai': подключён AI-агент с предобучением (FAQ, описание услуг,
        стиль ответов). Каждое сообщение клиента вызывает LLM →
        real_cost × ai.reply_margin_pct. Превью цены показывается юзеру.

    Виджет встраивается в hosted сайт как float-button + плавающее окно чата.
    Конкретно URL виджета: /sites/widget/{public_token}/embed.js — отдаёт
    JS-код который пользователи сайта load'ят через <script src=...></script>.
    """
    __tablename__ = "site_chat_widgets"

    id              = Column(Integer, primary_key=True, index=True)
    site_id         = Column(Integer, ForeignKey("site_projects.id", ondelete="CASCADE"),
                              nullable=False, index=True, unique=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    mode            = Column(String, nullable=False)  # relay / ai
    is_active       = Column(Boolean, default=True)
    # Конфигурация:
    #   - для relay: {tg_chat_id, max_user_id, welcome_text, hours}
    #   - для ai:    {welcome_text, system_prompt, faq, agent_module_slug}
    config_json     = Column(Text, nullable=True)
    # Связь с AgentModule если mode='ai' — слаг автоматически "site_chatbot_{id}"
    # это позволяет юзеру видеть бота как модуль в ИИ-Агенте и редактировать.
    agent_module_id = Column(Integer, ForeignKey("agent_modules.id", ondelete="SET NULL"),
                              nullable=True, index=True)
    # Статистика
    total_messages  = Column(Integer, default=0)
    last_message_at = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    site = relationship("SiteProject", backref="chat_widget")


class SiteChatMessage(Base):
    """Сообщение чата сайта (от посетителя или от ответчика — юзер/AI).

    visitor_id — уникальный токен браузера посетителя (cookie/localStorage),
    чтобы группировать диалоги. Без авторизации — мы не знаем кто это.
    role='user' = посетитель, 'assistant' = ответ (юзер через TG-relay
    или AI-агент), 'system' = служебные сообщения.
    """
    __tablename__ = "site_chat_messages"

    id          = Column(Integer, primary_key=True, index=True)
    widget_id   = Column(Integer, ForeignKey("site_chat_widgets.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    visitor_id  = Column(String, nullable=False, index=True)  # 16-char token
    role        = Column(String, nullable=False)  # user / assistant / system
    content     = Column(Text, nullable=False)
    cost_kop    = Column(Integer, default=0)
    created_at  = Column(DateTime, default=datetime.utcnow, index=True)


class SiteCustomDomain(Base):
    """Кастомный домен юзера для опубликованного сайта.

    Юзер привязывает свой `example.com` к сайту:
      1. Создаёт запись → получает verification_token (для TXT-record).
      2. Прописывает у регистратора:
         - CNAME @ → aiche.ru (главная запись для трафика)
         - TXT _aiche-verify.example.com → <verification_token>
      3. Дёргает /verify → мы dig'аем DNS, при успехе сертификируем
         через certbot + добавляем nginx-config + reload.
      4. Сайт доступен через https://example.com.

    Поля статуса:
      verification_status: pending | verified | failed
      ssl_status:          none | issuing | active | renewing | error
    """
    __tablename__ = "site_custom_domains"

    id                  = Column(Integer, primary_key=True, index=True)
    site_id             = Column(Integer, ForeignKey("site_projects.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    user_id             = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    domain              = Column(String, nullable=False, unique=True, index=True)  # example.com
    verification_token  = Column(String, nullable=False)                          # для TXT-record
    verification_status = Column(String, default="pending")                       # pending/verified/failed
    verified_at         = Column(DateTime, nullable=True)
    last_check_at       = Column(DateTime, nullable=True)
    last_check_error    = Column(Text, nullable=True)
    ssl_status          = Column(String, default="none")                          # none/issuing/active/renewing/error
    ssl_issued_at       = Column(DateTime, nullable=True)
    ssl_expires_at      = Column(DateTime, nullable=True)
    nginx_config_path   = Column(String, nullable=True)                           # /etc/nginx/conf.d/custom/<domain>.conf
    created_at          = Column(DateTime, default=datetime.utcnow)

    site = relationship("SiteProject", backref="custom_domains")


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
    user_id           = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    name              = Column(String, nullable=False)
    template_id       = Column(Integer, nullable=True)
    input_data        = Column(Text, nullable=True)          # JSON введённых данных (legacy)
    generated_content = Column(Text, nullable=True)          # legacy: текстовый markdown / HTML
    status            = Column(String, default="draft")      # draft / generated / done / error
    price_tokens      = Column(Integer, default=0)           # списано (копейки)
    image_paths       = Column(Text, nullable=True)          # JSON массив URL картинок
    attached_bot_id   = Column(Integer, ForeignKey("chatbots.id"), nullable=True)
    # ── Новые поля для переработанного модуля презентаций ──
    topic             = Column(String, nullable=True)         # о чём презентация
    audience          = Column(String, nullable=True)         # для кого (инвесторы, клиенты, команда)
    slide_count       = Column(Integer, default=10)           # сколько слайдов хочет юзер (5..30)
    extra_info        = Column(Text, nullable=True)           # доп. контекст (цифры, факты, тезисы)
    color_scheme      = Column(String, nullable=True, default="dark")  # legacy: dark|light|brand|corp
    style_preset      = Column(String, nullable=True, default="business")
    # ── Кастомные цвета (ручной выбор юзера, приоритетнее color_scheme) ──
    bg_color          = Column(String, nullable=True)         # #1C1C1C
    text_color        = Column(String, nullable=True)         # #f0e6d8
    accent_color      = Column(String, nullable=True)         # #ff8c42
    title_color       = Column(String, nullable=True)         # #ffb347
    # ── Стиль клиента (URL сайта для парсинга стилистики) ──
    client_site_url   = Column(String, nullable=True)         # https://client.com
    client_site_ctx   = Column(Text, nullable=True)           # извлечённый текст с сайта
    # ── Custom charts/диаграммы (юзер задаёт явно, не через extra_info) ──
    custom_charts     = Column(Text, nullable=True)           # JSON массив графиков
    # Сгенерированная структура (JSON со слайдами)
    slides_json       = Column(Text, nullable=True)
    pptx_path         = Column(String, nullable=True)
    html_preview      = Column(String, nullable=True)
    pdf_path          = Column(String, nullable=True)         # PDF экспорт
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
    user_id       = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
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
    user_id       = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
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
    """Файл в базе знаний агента или чат-бота.

    Модель универсальная: owner_type='bot'|'agent', owner_id — id владельца.
    user_id обязателен (для проверки доступа и storage-биллинга).

    Текст файла режется на чанки (KnowledgeChunk), каждый чанк имеет embedding
    для семантического поиска. summary/facts — короткое описание для UI и
    как fallback на TF-search до индексации embedding'ов (или для legacy).
    """
    __tablename__ = "knowledge_files"

    id           = Column(Integer, primary_key=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    owner_type   = Column(String, default="bot", index=True)   # "bot" | "agent"
    owner_id     = Column(Integer, index=True, nullable=True)  # bot.id ИЛИ agent_config.id
    bot_id       = Column(Integer, index=True, nullable=True)  # legacy — оставлен для совместимости
    name         = Column(String, nullable=False)
    path         = Column(String, nullable=False)              # /uploads/...
    mime         = Column(String, nullable=True)
    size         = Column(Integer, default=0)
    description  = Column(String, nullable=True)               # одна строка
    tags         = Column(String, nullable=True)               # через запятую
    summary      = Column(Text, nullable=True)                 # 2-4 предложения для UI
    facts        = Column(Text, nullable=True)                 # через "; "
    content_text = Column(Text, nullable=True)                 # полный текст (legacy)
    chunk_count  = Column(Integer, default=0)                  # сколько чанков создано
    indexing_status = Column(String, default="pending")        # pending | indexing | ready | failed
    indexing_error  = Column(Text, nullable=True)
    enabled      = Column(Boolean, default=True)               # участвует ли в retrieve
    # Knowledge Hub categorization (Агенты v2). Авто-классифицируется при загрузке
    # одним вызовом Haiku. Каждая роль агента запрашивает только нужные категории.
    # Допустимые: pricing | legal | finance | brand | regulation | contacts | other
    category     = Column(String, default="other", index=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    # Storage-биллинг: scheduler.storage_billing_tick списывает плату за
    # KB-файлы аналогично StoredAsset (50 ₽/мес за 100 МБ).
    last_billed_at = Column(DateTime, nullable=True)


class KnowledgeChunk(Base):
    """Один кусок текста из KnowledgeFile с embedding-вектором.

    text — собственно текст для подмешивания в prompt.
    embedding_json — JSON-список float[1536] (text-embedding-3-small) или
                     float[3072] (text-embedding-3-large), для cosine search.
    token_count — приблизительная оценка через tiktoken (для биллинга).
    """
    __tablename__ = "knowledge_chunks"

    id            = Column(Integer, primary_key=True)
    kb_file_id    = Column(Integer, ForeignKey("knowledge_files.id", ondelete="CASCADE"), index=True, nullable=False)
    chunk_index   = Column(Integer, default=0)
    text          = Column(Text, nullable=False)
    embedding_json= Column(Text, nullable=True)
    token_count   = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)


class AdminAuditLog(Base):
    """Лог критичных действий админа: баланс, цены, баны, ключи, промокоды."""
    __tablename__ = "admin_audit_log"

    id         = Column(Integer, primary_key=True, index=True)
    admin_id   = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    action     = Column(String, nullable=False, index=True)   # напр. "adjust_balance"
    target_type= Column(String, nullable=True)                # "user" | "apikey" | "promo" | ...
    target_id  = Column(String, nullable=True)                # id объекта (строка чтобы поддержать любые)
    details    = Column(Text, nullable=True)                  # JSON с параметрами (delta, reason, ...)
    ip         = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ImapCredential(Base):
    """Credentials для IMAP (email trigger).

    ⚠️  SECURITY NOTE — password handling:
    Поле `password` — обычный String, НЕ EncryptedString. Шифрование
    выполняется ВРУЧНУЮ в routes/user.py (через server.secrets_crypto.encrypt)
    при сохранении и в email_imap.py / mailbox_runtime.py (через decrypt) при
    чтении. ЗНАЧЕНИЕ В БД УЖЕ В ФОРМАТЕ `enc:v1:<base64>`.

    НЕ МЕНЯТЬ на `Column(EncryptedString)` без миграции данных — TypeDecorator
    зашифрует уже зашифрованное значение, и расшифровка вернёт мусор → юзеры
    потеряют доступ к IMAP. План миграции: backup → bulk decrypt → change
    column type → bulk save (TypeDecorator зашифрует) → verify roundtrip.
    """
    __tablename__ = "imap_credentials"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    label      = Column(String, default="Main")
    host       = Column(String, nullable=False)      # imap.yandex.ru
    port       = Column(Integer, default=993)
    username   = Column(String, nullable=False)
    password   = Column(String, nullable=False)      # ⚠️ manual encrypt — see class docstring
    use_ssl    = Column(Boolean, default=True)
    last_uid   = Column(Integer, default=0)          # последний обработанный UID
    created_at = Column(DateTime, default=datetime.utcnow)


class FinanceTransaction(Base):
    """Транзакция личных финансов юзера (для модуля finance).

    Источники: CSV-импорт банковской выписки (Tinkoff/Sber/Alfa/generic)
    или ручной ввод. Хранится в КОПЕЙКАХ (amount_kop) как и balance юзера.

    category — auto-классифицирована при импорте (keyword-based) или
    переопределена юзером. Стандартные категории — см. _CATEGORIES в
    server/finance_csv.py.

    Без PII по умолчанию — описание из банк-выписки может содержать
    мерчанта/реквизиты, PrivacyGuard замаскирует при подмешивании
    в LLM context.

    Уникальность: по (user_id, date, amount_kop, description_hash) —
    не даём дублирующий импорт CSV (юзер может загрузить тот же файл
    дважды). description_hash — md5 от raw_description.
    """
    __tablename__ = "finance_transactions"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    source          = Column(String, default="manual")  # manual / csv:tinkoff / csv:sber / csv:alfa / csv:generic
    date            = Column(DateTime, nullable=False, index=True)
    amount_kop      = Column(Integer, nullable=False)   # +доход / -расход в копейках
    currency        = Column(String, default="RUB")
    description     = Column(Text, nullable=True)        # raw из выписки
    merchant        = Column(String, nullable=True)      # извлечённый мерчант (опц.)
    category        = Column(String, nullable=True, index=True)  # food/transport/...
    is_manual_cat   = Column(Boolean, default=False)     # юзер переопределил категорию вручную
    description_hash= Column(String, nullable=True, index=True)  # md5 для дедупликации
    created_at      = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "date", "amount_kop", "description_hash",
                         name="uq_finance_tx_dedupe"),
    )


class UserMailbox(Base):
    """Личный почтовый ящик юзера, подключённый к модулю Почта (Loom).

    Отдельная сущность от ImapCredential — у того контекст «триггер для
    бота» (нужен last_uid, привязка к workflow). Здесь — «модуль читает
    мою почту по запросу»: last_synced_at для отображения «синхронизирован
    минуту назад», без сохранения last_uid.

    provider: yandex / gmail / mailru / other (для подсказок host/port в UI).
    password — app-password (Yandex даёт в id.yandex.ru/security/app-passwords;
    Gmail — в myaccount.google.com/apppasswords при включённой 2FA).
    EncryptedString — HKDF от JWT_SECRET, прозрачно для приложения.

    Один юзер может подключить несколько ящиков (личный + рабочий + общий
    info@), модуль mail увидит все при invoke.
    """
    __tablename__ = "user_mailboxes"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    provider        = Column(String, default="other")  # yandex/gmail/mailru/other
    label           = Column(String, nullable=True)    # «Личный», «Рабочий» — для UI
    email           = Column(String, nullable=False)   # me@yandex.ru — также username для IMAP
    host            = Column(String, nullable=False)   # imap.yandex.ru
    port            = Column(Integer, default=993)
    password        = Column(EncryptedString(1024), nullable=False)  # app-password
    # SMTP-настройки для отправки писем модулем mail. Если NULL — будут
    # выведены автоматически из host (yandex.ru/gmail.com/mail.ru). Тот же
    # password (app-password) обычно работает и для SMTP.
    smtp_host       = Column(String, nullable=True)    # smtp.yandex.ru
    smtp_port       = Column(Integer, nullable=True)   # 465 (SSL) / 587 (STARTTLS)
    is_active       = Column(Boolean, default=True)
    last_synced_at  = Column(DateTime, nullable=True)
    last_error      = Column(Text, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "email", name="uq_user_mailbox_email"),
    )


class ActionLog(Base):
    """Аудит-лог всех значимых действий пользователя/системы.

    Назначение: дать AI-ассистенту в новом чате контекст «что вообще
    происходило в проде» через выгрузку GET /admin/actions.txt. Также
    полезно для разбора бажных кейсов («у юзера X не списались деньги»).

    Что логируется (action):
      - auth.register / auth.login / auth.verify / auth.oauth
      - payment.webhook / payment.confirm / payment.referral_bonus
      - ai.chat / ai.image / ai.video — каждый AI-вызов с моделью+стоимостью
      - site.generate_start / site.generate_done / site.generate_failed
      - bot.create / bot.delete / bot.from_template / bot.ai_create / bot.ai_improve
      - record.created — новая заявка/бронь от чат-бота
      - admin.* — действия в админке
      - error.* — необработанные исключения

    level: info / warn / error / critical
    success: True/False (для быстрых SQL-фильтров «покажи все ошибки»)
    details: JSON с произвольными метаданными (цена, модель, токены, msg)
    """
    __tablename__ = "action_logs"

    id          = Column(Integer, primary_key=True, index=True)
    ts          = Column(DateTime, default=datetime.utcnow, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    action      = Column(String, index=True, nullable=False)         # «site.generate_done»
    target_type = Column(String, nullable=True, index=True)           # «site_project» / «bot» / «payment»
    target_id   = Column(String, nullable=True)                       # str чтобы поддержать любые
    level       = Column(String, default="info", index=True)          # info / warn / error / critical
    success     = Column(Boolean, default=True, index=True)
    details     = Column(Text, nullable=True)                         # JSON
    ip          = Column(String, nullable=True)
    request_id  = Column(String, nullable=True, index=True)           # X-Request-ID для связи с другими логами
    error       = Column(Text, nullable=True)                         # текст исключения если success=False


class AiRequestLog(Base):
    """Отдельный лог AI-вызовов для аналитики (адаптация Dolibarr `airequestlog`).

    Зачем рядом с Transaction/ActionLog: эти таблицы решают другие задачи —
    Transaction = биллинг, ActionLog = аудит общих действий. AiRequestLog даёт
    быстрые ответы на специфические AI-вопросы:
      - Какая модель самая дорогая за месяц?
      - У какого юзера самый длинный promptt?
      - Сколько токенов выжигает orchestra-pipeline X?
      - Сколько % запросов failed (по провайдеру/модели)?

    Записывается из server/ai.py:generate_response() fire-and-forget
    (исключения глотаются, биллинг не блокируется).
    """
    __tablename__ = "ai_request_logs"

    id           = Column(Integer, primary_key=True, index=True)
    ts           = Column(DateTime, default=datetime.utcnow, index=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                          index=True, nullable=True)
    provider     = Column(String, index=True, nullable=False)         # openai / anthropic / perplexity / ...
    model        = Column(String, index=True, nullable=False)         # gpt-4o / claude-sonnet-4-6 / sonar-pro
    purpose      = Column(String, nullable=True, index=True)          # chat / orchestra / proposal / sites / agent / etc.
    input_tokens = Column(Integer, default=0)
    output_tokens= Column(Integer, default=0)
    cost_kop     = Column(Integer, default=0, index=True)              # реальная стоимость в копейках (× margin не применён)
    duration_ms  = Column(Integer, default=0)                          # сколько занял HTTP-вызов
    success      = Column(Boolean, default=True, index=True)
    error        = Column(String, nullable=True)                       # type(e).__name__ при failure
    user_key     = Column(Boolean, default=False)                      # юзер использовал свой API-ключ?
    request_id   = Column(String, nullable=True, index=True)           # X-Request-ID для связи с logs


class BotRecord(Base):
    """Универсальная таблица заявок/броней/заказов/опросов из чат-бота.

    Один шаблон бота = один record_type:
      - lead       — лидогенерация (имя, телефон, ниша, бюджет)
      - booking    — запись на услугу (имя, телефон, услуга, дата, время)
      - order      — заказ из мини-магазина (товары, адрес, телефон)
      - quiz       — результат квиза/воронки (ответы, сегмент)
      - ticket     — заявка в поддержку из FAQ-бота
      - subscriber — подписчик контент-бота

    Любые специфичные поля шаблона лежат в payload (JSON-словарь).
    Так одной таблицей покрываем все 6 шаблонов и любые будущие.

    UI «Записи» в карточке бота показывает таблицу с фильтром по record_type.
    """
    __tablename__ = "bot_records"

    id              = Column(Integer, primary_key=True, index=True)
    bot_id          = Column(Integer, ForeignKey("chatbots.id"), index=True, nullable=False)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    chat_id         = Column(String, index=True, nullable=True)   # с какого чата пришло
    record_type     = Column(String, nullable=False, index=True)
    customer_name   = Column(String, nullable=True)
    customer_phone  = Column(String, nullable=True)
    customer_email  = Column(String, nullable=True)
    payload         = Column(Text, nullable=True)                 # JSON {service, slot_at, ...}
    status          = Column(String, default="new", index=True)   # new / processed / cancelled
    notes           = Column(Text, nullable=True)                 # внутренние заметки владельца
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BotConversationTurn(Base):
    """Один тёрн диалога в чат-боте — для multi-turn контекста.

    Раньше история жила в RAM (`_conversations` dict в chatbot_engine.py),
    что ломалось при рестарте сервера и не работало на N uvicorn workers
    (каждый процесс имел свою копию контекста). Теперь — SQLite, переживает
    рестарт, общая для всех воркеров.

    Очистка: периодический job в scheduler удаляет тёрны старше 30 дней.
    """
    __tablename__ = "bot_conversation_turns"

    id          = Column(Integer, primary_key=True, index=True)
    bot_id      = Column(Integer, index=True, nullable=False)
    chat_id     = Column(String, index=True, nullable=False)
    role        = Column(String, nullable=False)   # "user" | "assistant" | "system"
    content     = Column(Text, nullable=False)
    created_at  = Column(DateTime, default=datetime.utcnow, index=True)


class ChatBot(Base):
    """Постоянный бот — слушает входящие и отвечает через AI 24/7."""
    __tablename__ = "chatbots"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
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
    # WhatsApp через Wazzup24 (российский официальный посредник).
    # wazzup_api_key — выдаётся в личном кабинете Wazzup24,
    # wazzup_channel_id — идентификатор подключённого WA-канала.
    wazzup_api_key    = Column(EncryptedString, nullable=True)
    wazzup_channel_id = Column(String, nullable=True)
    # Виджет
    widget_enabled  = Column(Boolean, default=False)
    widget_secret   = Column(EncryptedString, nullable=True)
    # Список доменов через запятую («example.com, app.example.com»).
    # Пусто = принимаем любой Origin (back-compat). При непустом — WS-коннект
    # с чужого Origin отбивается с code=4003.
    widget_allowed_origins = Column(Text, nullable=True)
    # Воркфлоу (JSON граф нод/связей из конструктора)
    workflow_json   = Column(Text, nullable=True)
    # Лимиты
    max_replies_day = Column(Integer, default=100)
    cost_per_reply  = Column(Integer, default=5)
    replies_today   = Column(Integer, default=0)
    replies_reset_at= Column(DateTime, nullable=True)
    # Конструктор ботов: parent_bot_id — бот-конструктор, через который этот
    # бот был создан в TG/MAX. auto_generated=True для AI-сгенеренных ботов.
    parent_bot_id   = Column(Integer, nullable=True)
    auto_generated  = Column(Boolean, default=False)
    # Статус
    status          = Column(String, default="off")  # off / active / paused
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="chatbots")


class BotPriceItem(Base):
    """
    Прайс-лист бота. Записи автоматически inject'ятся в system_prompt
    AI-вызовов бота — когда клиент спрашивает «сколько стоит», AI находит
    в прайсе нужную позицию и отвечает с ценой.

    Поля:
    - name: «Маникюр классический» — что продаём
    - price_kop: 250000 (= 2500 ₽) — цена в копейках
    - price_text: «от 2 500 ₽» — текстовая форма (для «договорных», «от/до»)
    - category: «Услуги», «Товары», «Тарифы» — для группировки
    - description: подробности (длительность, что входит, нюансы)
    - sort_order: порядок отображения
    """
    __tablename__ = "bot_price_items"

    id          = Column(Integer, primary_key=True, index=True)
    bot_id      = Column(Integer, ForeignKey("chatbots.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    name        = Column(String, nullable=False)
    price_kop   = Column(Integer, nullable=True)         # null = «по запросу»
    price_text  = Column(String, nullable=True)          # «от 1500 ₽», «договорная»
    category    = Column(String, nullable=True)
    description = Column(String, nullable=True)
    sort_order  = Column(Integer, default=0)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    # Vector embedding (text-embedding-3-small, 1536 dim) — JSON-encoded
    # list of floats. Используется в vector search при ответе на «сколько стоит».
    # null = ещё не посчитан, fallback на substring search.
    embedding_json = Column(Text, nullable=True)


class UserApiKey(Base):
    """Собственные API-ключи пользователя для AI-провайдеров.

    Ключ хранится зашифрованным (Fernet с HKDF от JWT_SECRET).
    Используется в server.chatbot_engine._deduct_bot_usage для определения
    скидки + в server.ai.generate_response чтобы передать ключ юзера в
    провайдера вместо нашего."""
    __tablename__ = "user_api_keys"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider   = Column(String, nullable=False)   # openai | anthropic | gemini | grok
    api_key    = Column(EncryptedString, nullable=False)
    label      = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", backref="api_keys_own")


# ── КП-конструктор: бренды + проекты + контекст клиента ────────────────────
# Отделено от PresentationProject (где жили КП и презентации в одной таблице
# с doc_type), потому что КП нужны:
#   - брендинг (лого, цвета, шрифт, реквизиты) → шаблон оформления
#   - контекст клиента (запрос, сайт) → персонализация
#   - привязка к боту с прайсом
#   - email-orchestration (auto-reply через trigger_imap)


class ProposalBrand(Base):
    """Шаблон фирменного оформления КП. У юзера может быть несколько
    (для разных компаний/линеек продуктов).

    Используется при генерации: лого + цвета + шрифт + контакты пишутся в
    HTML/PDF одинаково, а наполнение генерится под клиента."""
    __tablename__ = "proposal_brands"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    name            = Column(String, nullable=False)         # «Основной», «Линейка X»
    company_name    = Column(String, nullable=True)
    # Лого: URL загруженной картинки (через /upload или /assets)
    logo_url        = Column(String, nullable=True)
    # Цвета HEX (#RRGGBB), валидируются на стороне роута
    primary_color   = Column(String, nullable=True, default="#ff8c42")
    secondary_color = Column(String, nullable=True, default="#1C1C1C")
    accent_color    = Column(String, nullable=True, default="#ffb347")
    font_family     = Column(String, nullable=True, default="Inter")
    # Преcет header/footer стиля: «minimal» | «classic» | «bold»
    style_preset    = Column(String, nullable=True, default="minimal")
    # Реквизиты для подвала
    contacts        = Column(Text, nullable=True)
    inn             = Column(String, nullable=True)
    address         = Column(Text, nullable=True)
    # Подпись (URL загруженной картинки факсимиле, опционально)
    signature_url   = Column(String, nullable=True)
    # ── Расширенная персонализация (используется в Claude prompt v3) ──
    tagline         = Column(String, nullable=True)        # «Промышленные решения с 2010 года»
    usp_list        = Column(Text, nullable=True)          # УТП по строке: «10 лет на рынке\n200+ проектов\n…»
    guarantees      = Column(Text, nullable=True)          # «Гарантия 2 года\nДоговор\n…»
    tone            = Column(String, nullable=True, default="business")
    # business | friendly | premium | tech (для подбора лексики)
    intro_phrase    = Column(String, nullable=True)        # Шаблон обращения к клиенту
    cta_phrase      = Column(String, nullable=True)        # Призыв к действию по умолчанию
    # Дефолтный — используется если в проекте КП не выбран бренд
    is_default      = Column(Boolean, default=False)
    created_at      = Column(DateTime, default=datetime.utcnow)


class ProposalVersion(Base):
    """История версий сгенерированного КП. Создаётся каждый раз при
    регенерации / правке секции / правке HTML — чтобы юзер мог откатиться,
    если новая версия хуже предыдущей. Храним N последних (cleanup в scheduler).
    """
    __tablename__ = "proposal_versions"

    id              = Column(Integer, primary_key=True, index=True)
    proposal_id     = Column(Integer, ForeignKey("proposal_projects.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    html            = Column(Text, nullable=True)
    pdf_path        = Column(String, nullable=True)
    note            = Column(String, nullable=True)        # «генерация», «правка блока», «ручная»
    cost_kop        = Column(Integer, default=0)
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)


class ProposalSignature(Base):
    """Электронная подпись клиента под КП — рисуется canvas'ом на публичной
    странице /p/{token}. Audit-trail: имя+email+ip+user_agent+время+SHA-256.

    После подписания:
      - в PDF добавляется блок «Подписано: <имя>, <дата>, <ip>»
      - crm_stage переходит в "won" (опционально, через UI владельца)
      - событие proposal.signed диспатчится в webhooks
      - push владельцу
      - email владельцу

    Hash считается от: proposal_id + signer_email + signed_at + signature_data_url +
    ip — гарантирует что любая подмена не пройдёт верификацию.
    """
    __tablename__ = "proposal_signatures"

    id              = Column(Integer, primary_key=True, index=True)
    proposal_id     = Column(Integer, ForeignKey("proposal_projects.id", ondelete="CASCADE"),
                             unique=True, nullable=False, index=True)
    signer_name     = Column(String, nullable=False)             # ФИО клиента
    signer_email    = Column(String, nullable=True)              # опционально
    signer_phone    = Column(String, nullable=True)
    signer_position = Column(String, nullable=True)              # должность
    signature_data  = Column(Text, nullable=False)               # data:image/png;base64,...
    ip              = Column(String, nullable=True)
    user_agent      = Column(String, nullable=True)
    sig_hash        = Column(String, nullable=False, index=True) # sha256 hex для верификации
    # SHA-256 от generated_pdf (или generated_html) на момент подписи.
    # Защита от post-signature mutation: если владелец поменял текст КП после
    # подписи, current content_hash != stored → подпись считается «отозвана»
    # для нового документа, юзер видит уведомление.
    content_hash    = Column(String, nullable=True, index=True)
    signed_at       = Column(DateTime, default=datetime.utcnow, nullable=False)


class ProposalPriceList(Base):
    """Прайс-лист для КП. У юзера может быть несколько (для разных линеек,
    отделов, языков). Привязан напрямую к user_id (не к боту) — раньше
    прайс тянулся из ChatBot.BotPriceItem, но это неудобно: не у каждого
    есть бот, плюс у бота прайс для разговоров, а тут нужен «оформительский»
    список для КП."""
    __tablename__ = "proposal_price_lists"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    name        = Column(String, nullable=False)
    description = Column(String, nullable=True)
    is_default  = Column(Boolean, default=False)
    created_at  = Column(DateTime, default=datetime.utcnow)


class ProposalPriceItem(Base):
    """Позиция прайса. Аналогична BotPriceItem (тот для чат-ботов), но
    проще — без embedding'ов (для КП хватает простой текстовой подачи
    в Claude prompt)."""
    __tablename__ = "proposal_price_items"

    id          = Column(Integer, primary_key=True, index=True)
    price_list_id = Column(Integer, ForeignKey("proposal_price_lists.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    name        = Column(String, nullable=False)
    price_kop   = Column(Integer, nullable=True)         # null = «по запросу»
    price_text  = Column(String, nullable=True)          # «от 2 500 ₽», «договорная»
    category    = Column(String, nullable=True)
    description = Column(String, nullable=True)
    sort_order  = Column(Integer, default=0)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


class ProposalProject(Base):
    """Проект КП (коммерческого предложения).

    Отдельно от PresentationProject. Хранит контекст клиента + сгенерённый
    HTML/PDF. Может быть привязан к ProposalBrand (оформление) и ChatBot
    (для подтягивания BotPriceItem)."""
    __tablename__ = "proposal_projects"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    name            = Column(String, nullable=False)
    status          = Column(String, default="draft")        # draft | done | error
    brand_id        = Column(Integer, ForeignKey("proposal_brands.id", ondelete="SET NULL"),
                             nullable=True)
    bot_id          = Column(Integer, ForeignKey("chatbots.id", ondelete="SET NULL"),
                             nullable=True)
    price_list_id   = Column(Integer, ForeignKey("proposal_price_lists.id", ondelete="SET NULL"),
                             nullable=True)
    # Контекст клиента
    client_name     = Column(String, nullable=True)
    client_email    = Column(String, nullable=True)
    client_request  = Column(Text, nullable=True)            # текст письма/заявки
    client_site_url = Column(String, nullable=True)          # URL для парсинга
    client_site_ctx = Column(Text, nullable=True)            # извлечённый контекст
    # Доп. инструкции от юзера (оффер, сроки, акции)
    extra_notes     = Column(Text, nullable=True)
    # Результаты
    generated_html  = Column(Text, nullable=True)            # HTML с применённым брендом
    generated_pdf   = Column(String, nullable=True)          # путь к PDF /uploads/proposals/...
    price_kop       = Column(Integer, default=0)             # сколько списано
    # Email-orchestration: если КП был сгенерён автоматически из IMAP-письма
    auto_generated  = Column(Boolean, default=False)
    source_email_id = Column(String, nullable=True, index=True)  # message-id входящего письма (на которое отвечаем)
    outbox_message_id = Column(String, nullable=True, index=True)  # message-id нашего исходящего письма с КП (для threading)
    sent_at         = Column(DateTime, nullable=True)        # когда отправили ответ
    # CRM: lifecycle статус, опц. поверх status (draft|done|error|refunded)
    crm_stage       = Column(String, default="new", index=True)
    # «new» (создан) | «sent» (отправлен) | «opened» (клиент открыл PDF/публичную ссылку)
    # | «replied» (есть ответ от клиента) | «won» (закрыта сделка) | «lost» (отказ)
    opened_at       = Column(DateTime, nullable=True)        # когда клиент впервые открыл публичную ссылку
    open_count      = Column(Integer, default=0)             # сколько раз клиент открывал публичную ссылку
    replied_at      = Column(DateTime, nullable=True)
    won_at          = Column(DateTime, nullable=True)
    lost_at         = Column(DateTime, nullable=True)
    public_token    = Column(String, unique=True, nullable=True, index=True)  # для публичной ссылки
    # Стиль шапки PDF/HTML: classic | banner | centered | minimal
    # Юзер выбирает при создании КП. Default — classic (как было раньше).
    header_layout   = Column(String, default="classic")
    # Auto-followup: cron-задача каждые 6 часов находит КП где sent_at старше
    # FOLLOWUP_DELAY_DAYS дней + opened_at IS NULL + не было followup → шлёт
    # клиенту вежливое напоминание. Юзер может отключить per-проект.
    auto_followup_enabled = Column(Boolean, default=True)
    followup_sent_at      = Column(DateTime, nullable=True)    # когда мы сами напомнили
    created_at      = Column(DateTime, default=datetime.utcnow)


# ── Creators (контент-планирование) ───────────────────────────────────────────

class CreatorBrand(Base):
    """Бренд/компания, для которой строим контент-план.

    Юзер может вести несколько брендов (агентство ведёт 5 клиентов).
    Не путать с CompanyProfile — тот для КП/презентаций, один на юзера.
    """
    __tablename__ = "creator_brands"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name            = Column(String, nullable=False)
    niche           = Column(String, nullable=True)               # e-commerce / услуги / IT / ...
    product         = Column(Text, nullable=True)                  # что продаёте
    audience        = Column(Text, nullable=True)                  # ЦА: возраст/пол/география/интересы
    tone            = Column(String, nullable=True)                # friendly / expert / premium / provocative / neutral
    topics_json     = Column(Text, nullable=True)                  # JSON-array из 3-5 ключевых тем
    stopwords       = Column(Text, nullable=True)                  # стоп-слова
    logo_url        = Column(String, nullable=True)                # /uploads/...
    # Freemium-счётчик
    free_posts_used_this_month = Column(Integer, default=0)
    free_posts_reset_at        = Column(DateTime, nullable=True)   # 1 число следующего месяца
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="creator_brands")


class ContentCalendar(Base):
    """Контент-план на период (обычно месяц). Один активный календарь на бренд."""
    __tablename__ = "content_calendars"

    id              = Column(Integer, primary_key=True, index=True)
    brand_id        = Column(Integer, ForeignKey("creator_brands.id", ondelete="CASCADE"), nullable=False, index=True)
    period_start    = Column(DateTime, nullable=False)
    period_end      = Column(DateTime, nullable=False)
    status          = Column(String, default="active", index=True)  # draft / active / archived
    generated_at    = Column(DateTime, default=datetime.utcnow)

    brand = relationship("CreatorBrand", backref="calendars")


class ContentItem(Base):
    """Отдельный пост в контент-плане.

    status:
      planned   — запланирован, ничего не подготовлено
      preparing — scheduler готовит (текст/картинку)
      ready     — готов, ждёт публикации или одобрения
      published — опубликован
      skipped   — пропущен юзером

    is_news: True = «актуальный» (готовим в день постинга через Perplexity),
             False = evergreen (готовим заранее через Sonnet).
    """
    __tablename__ = "content_items"

    id              = Column(Integer, primary_key=True, index=True)
    calendar_id     = Column(Integer, ForeignKey("content_calendars.id", ondelete="CASCADE"), nullable=False, index=True)
    schedule_at     = Column(DateTime, nullable=False, index=True)   # планируемая дата+время публикации (UTC)
    platform        = Column(String, nullable=False, index=True)     # tg / vk / yt / ig
    type            = Column(String, nullable=False)                 # text / image / reels / youtube / poll / news
    is_news         = Column(Boolean, default=False)
    brief           = Column(Text, nullable=True)                    # о чём пост (от плана)
    prepared_content_md = Column(Text, nullable=True)                # готовый текст
    prepared_media_url  = Column(String, nullable=True)              # картинка/видео (если есть)
    status          = Column(String, default="planned", index=True)
    cost_kop        = Column(Integer, default=0)                     # сколько списано за подготовку (0 для freemium)
    published_at    = Column(DateTime, nullable=True)
    error           = Column(Text, nullable=True)
    manual_override = Column(Boolean, default=False)                  # юзер отредактировал руками
    # ID опубликованного поста на платформе (для последующего fetch'а метрик).
    # TG: message_id (число, но храним как строку для совместимости).
    # VK: post_id (число, как строка).
    # YT/IG: пока пусто (auto-publish не поддерживается).
    external_post_id  = Column(String, nullable=True)
    external_chat_id  = Column(String, nullable=True)                 # для TG — куда отправили
    # Exponential backoff при ошибках публикации: 1мин → 5мин → 30мин → 2ч → 6ч.
    # После 5 неудач — оставляем в error до ручного вмешательства.
    publish_fail_count   = Column(Integer, default=0)
    publish_next_retry_at = Column(DateTime, nullable=True)
    # Метрики (обновляются cron'ом creators_metrics_loop раз в 6 часов).
    stats_views     = Column(Integer, default=0)
    stats_likes     = Column(Integer, default=0)
    stats_comments  = Column(Integer, default=0)
    stats_shares    = Column(Integer, default=0)
    stats_fetched_at = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    calendar = relationship("ContentCalendar", backref="items")


class UserCalendarConnection(Base):
    """Подключение календаря юзера для модуля «📅 Календарь» (Loom Phase 2).

    Один юзер может подключить несколько календарей разных провайдеров
    (рабочий Google + личный Yandex) — каждое подключение отдельная строка.

    provider:
        google  — OAuth 2.0 через GOOGLE_CLIENT_ID + scope calendar.readonly.
                  Храним refresh_token (EncryptedString), access_token обновляем
                  по требованию через token endpoint.
        yandex  — CalDAV через app-password (caldav.yandex.ru). Без OAuth.
                  Храним email + app-password (EncryptedString).
        ics     — generic ICS URL (Google public link / Apple iCloud share).
                  No auth — публичный URL, только чтение.

    calendar_id:
        Google: id календаря ("primary" или email или unique-id).
        Yandex: имя календаря в CalDAV (default "events-default").
        ics:    URL для GET.

    account_email:
        Для UI отображения «какой аккаунт привязан». Для Google — берётся из
        userinfo endpoint при OAuth, для Yandex — email который ввёл юзер.
    """
    __tablename__ = "user_calendar_connections"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    provider        = Column(String, nullable=False)     # google | yandex | ics
    account_email   = Column(String, nullable=True)
    calendar_id     = Column(String, nullable=True)
    # Google: refresh_token (long-lived). Yandex: app-password.
    access_token    = Column(EncryptedString(2048), nullable=True)
    refresh_token   = Column(EncryptedString(2048), nullable=True)
    token_expires_at = Column(DateTime, nullable=True)
    # Для ICS — публичный URL календаря
    ics_url         = Column(String, nullable=True)
    is_active       = Column(Boolean, default=True)
    last_synced_at  = Column(DateTime, nullable=True)
    last_error      = Column(Text, nullable=True)
    fail_count      = Column(Integer, default=0)
    created_at      = Column(DateTime, default=datetime.utcnow)
    # Кэш событий из cron-sync (calendar_sync_loop, раз в 30 мин).
    # JSON-список событий на ближайшие 14 дней. Используется в
    # _fetch_calendar_context_for_user когда last_synced_at <30 мин назад —
    # тогда не делаем live-запрос к Google/Yandex/ICS (быстрее ответ агента).
    cached_events_json = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "provider", "account_email",
                          name="uq_user_calendar_account"),
    )


class LocalCalendarEvent(Base):
    """Локальное событие календаря — создаётся юзером с /calendar.html
    или оркестратором при «внеси в календарь на 12 мая в 12:00».

    Это «наш» источник, в отличие от Google/Yandex/ICS — пишется в БД
    без OAuth-токенов. Видно в `fetch_all_user_events` как source='local'.
    Сейчас НЕ синхронизируется обратно в Google (нужен write-scope) —
    отдельная задача после большого теста.

    all_day=True означает событие без времени (только дата).
    """
    __tablename__ = "local_calendar_events"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    title       = Column(String, nullable=False)
    start       = Column(DateTime, nullable=False, index=True)
    end         = Column(DateTime, nullable=True)
    all_day     = Column(Boolean, default=False)
    description = Column(Text, nullable=True)
    location    = Column(String, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_local_calendar_user_start", "user_id", "start"),
    )


class CreatorChannelConnection(Base):
    """Подключение канала бренда — TG/VK/YouTube/Instagram.

    token — зашифрован EncryptedString. Для TG это bot-token (юзер делает бота
    админом канала); для VK — community access_token; для YT — OAuth refresh_token;
    для Instagram — не используется (только генерация, постит юзер сам).
    """
    __tablename__ = "creator_channel_connections"

    id              = Column(Integer, primary_key=True, index=True)
    brand_id        = Column(Integer, ForeignKey("creator_brands.id", ondelete="CASCADE"), nullable=False, index=True)
    platform        = Column(String, nullable=False)         # tg / vk / yt / ig
    channel_id      = Column(String, nullable=True)           # @channel_name или -100... для TG, group_id для VK
    title           = Column(String, nullable=True)           # отображаемое имя
    token           = Column(EncryptedString(2048), nullable=True)
    is_active       = Column(Boolean, default=True)
    fail_count      = Column(Integer, default=0)
    last_error_at   = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    brand = relationship("CreatorBrand", backref="channels")

    __table_args__ = (
        UniqueConstraint("brand_id", "platform", "channel_id", name="uq_creator_channel"),
    )


class CreatorAnalysisRun(Base):
    """Запуск анализа соцсети (свой профиль или конкурент).

    target_type: own | competitor
    target_url: URL аккаунта / канала
    result_md: рекомендации в markdown
    """
    __tablename__ = "creator_analysis_runs"

    id              = Column(Integer, primary_key=True, index=True)
    brand_id        = Column(Integer, ForeignKey("creator_brands.id", ondelete="CASCADE"), nullable=False, index=True)
    target_type     = Column(String, nullable=False)         # own / competitor
    target_url      = Column(String, nullable=False)
    platform        = Column(String, nullable=True)          # tg / vk / yt / ig — для удобства фильтрации
    status          = Column(String, default="running", index=True)  # running / done / failed
    result_md       = Column(Text, nullable=True)
    cost_kop        = Column(Integer, default=0)
    error           = Column(Text, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)

    brand = relationship("CreatorBrand", backref="analysis_runs")


# ─── ИИ Агенты v2 (Knowledge Hub + готовые роли) ──────────────────────────
# См. docs/modules/22-agents-v2-roadmap.md.
# Каталог из 6 предобученных ролей (Поисковик/Парсер/Юрист/Бухгалтер/
# Креатор/Автоответчик). Каждая роль — pipeline (orchestra) + system_prompt
# + список default-категорий Knowledge Hub.
#
# Runtime реюзаем через shadow Solution: при сидинге AgentRole создаётся
# парный Solution (subcategory='_agent_role', скрыт из /solutions каталога),
# который хранит реальный orchestra_json. Запуск роли = создание SolutionRun
# для shadow-Solution + обёртка в AgentRun. Это даёт нам бесплатно SSE,
# биллинг, share-token, attachments — всё что уже работает у Solutions.

class AgentRole(Base):
    """Готовая роль ИИ-агента (Поисковик/Юрист/Бухгалтер/...).

    pipeline_json — полная orchestra (как в Solution.orchestra_json).
    Сидится через scripts/seed_agent_roles.py, обновляется PR'ом в git.
    """
    __tablename__ = "agent_roles"

    id              = Column(Integer, primary_key=True, index=True)
    slug            = Column(String, unique=True, nullable=False, index=True)
    title           = Column(String, nullable=False)
    icon            = Column(String, nullable=True)            # эмодзи для карточки
    description     = Column(Text, nullable=True)              # 2-3 предложения для UI
    short_summary   = Column(String, nullable=True)            # одна строка под названием
    base_price_kop  = Column(Integer, default=0)               # фикс-цена за запуск (pay-per-run)
    default_model   = Column(String, default="claude-sonnet")
    system_prompt   = Column(Text, nullable=True)              # ядро поведения роли
    pipeline_json   = Column(Text, nullable=True)              # JSON orchestra (как у Solution)
    # Какие категории Knowledge Hub роль подмешивает по умолчанию (CSV).
    # Например 'legal,contacts' для Юриста. Пусто = вся база.
    default_kb_categories = Column(String, nullable=True)
    # FK на shadow-Solution который реально хранит orchestra и используется
    # solutions_orchestra.run_orchestra. Заполняется сидером.
    shadow_solution_id = Column(Integer, ForeignKey("solutions.id", ondelete="SET NULL"),
                                nullable=True)
    is_active       = Column(Boolean, default=True)
    sort_order      = Column(Integer, default=0)
    created_at      = Column(DateTime, default=datetime.utcnow)


class AgentRun(Base):
    """Запуск роли пользователем. Тонкая обёртка вокруг SolutionRun.

    Реальные стадии/прогресс/SSE/биллинг крутятся в SolutionRun, мы только
    линкуем и фильтруем «мои запуски агентов» отдельно от пилотов Solutions.
    """
    __tablename__ = "agent_runs"

    id                = Column(Integer, primary_key=True, index=True)
    user_id           = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                                nullable=False, index=True)
    role_id           = Column(Integer, ForeignKey("agent_roles.id", ondelete="CASCADE"),
                                nullable=False, index=True)
    # FK на SolutionRun где реально крутится orchestra. nullable на старте
    # (создаём AgentRun чуть раньше SolutionRun атомарно — но в одном запросе).
    solution_run_id   = Column(Integer, ForeignKey("solution_runs.id", ondelete="CASCADE"),
                                nullable=True, index=True)
    skills_json       = Column(Text, nullable=True)            # выбранные скилы (Иitre 4)
    # Снапшот input — храним для UI «мои запуски» без JOIN на solution_run
    input_preview     = Column(String, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow, index=True)

    role = relationship("AgentRole")


# ─── ИИ Агенты модульные (новый раздел 23) ─────────────────────────────────
# См. docs/modules/23-agents-modular-roadmap.md.
# Новая архитектура: юзер создаёт агента через диалог с Agent Builder, агент
# имеет spec (modules + schedule + triggers + goals), работает по расписанию
# или по запросу, общается с юзером в чате. Заменяет старый AgentConfig +
# canvas-конструктор (те остаются как legacy для разработчиков).

class Agent(Base):
    """Личный агент юзера. ОДИН на юзера (UNIQUE user_id) — это «твой ИИ»,
    которого ты знакомишь с собой и наращиваешь модулями.

    Архитектурно (после правки 2026-05-16):
      User (1) ←→ (1) Agent
                       ↓ messages — история общения
                       ↓ profile_json — Memory Hub (что агент знает о тебе)
                       ↓ personality_json — стиль / тон / имя агента
                       ↓ (1)─(N) AgentModule — подключённые навыки

    Раньше задумывалось «много агентов на юзера» — переделано на singleton
    после правки видения. Старые поля name/icon/spec_json/status оставлены
    для backward-compat, но используются по-новому:
      name/icon — кастомизация персонажа агента (по умолчанию «Че», 🤖)
      spec_json — global config (предпочтения, расписания cross-module)
      status — onboarding|active|paused (draft превратился в onboarding)
    """
    __tablename__ = "agents"

    id              = Column(Integer, primary_key=True, index=True)
    # Singleton enforcing в Python (get-or-create в роутах), НЕ через UNIQUE
    # constraint — иначе на existing-БД с старыми дублями миграция упадёт.
    # Если у юзера случайно несколько Agent — берём min(id) и архивируем остальные.
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    name            = Column(String, nullable=False, default="Че")
    icon            = Column(String, nullable=True, default="🤖")
    spec_json       = Column(Text, nullable=True)              # global config
    # Memory Hub: что агент узнал о юзере (имя, сфера, тон, цели).
    # Структура JSON: {"name":"...","industry":"...","tone":"...","goals":"...",
    #                  "facts":[{"key":"...","value":"...","learned_at":"..."}, ...]}
    profile_json    = Column(Text, nullable=True)
    # Personality: персонификация агента (одно лицо поверх N моделей).
    # {"display_name":"Че", "icon":"🤖", "voice":"friendly", "addon_prompt":"..."}
    personality_json = Column(Text, nullable=True)
    # onboarding — агент знакомится с юзером (первые 5-10 сообщений)
    # active — работает (по расписанию/триггерам/командам)
    # paused — на паузе по запросу юзера
    # archived — soft-deleted (для тестов; обычно не удаляем)
    status          = Column(String, default="onboarding", index=True)
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_activity_at = Column(DateTime, nullable=True)

    modules = relationship("AgentModule", back_populates="agent", cascade="all, delete-orphan")


class AgentModule(Base):
    """Подключённый к агенту навык (модуль). Каждый — со своим уровнем
    прокачки и персональной памятью.

    Прокачка L0-L4 (из ТЗ раздел 5.2):
      L0 — базовый (по умолчанию)
      L1 — знает базовые факты о юзере (5-10 взаимодействий)
      L2 — подстраивается под стиль (≥30 взаимодействий + подключён источник)
      L3 — действует проактивно (4+ недели + накопленная база)
      L4 — автономия (явное разрешение юзера + история без ошибок)

    module_memory_json — что модуль выучил под этого юзера (paraprompts).
    custom_settings_json — пользовательские настройки модуля (расписания,
    фильтры, токены каналов).
    """
    __tablename__ = "agent_modules"

    id              = Column(Integer, primary_key=True, index=True)
    agent_id        = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    slug            = Column(String, nullable=False, index=True)   # из AGENT_REGISTRY
    level           = Column(Integer, default=0)                    # 0..4
    interaction_count = Column(Integer, default=0)                  # для прокачки L→L+1
    module_memory_json   = Column(Text, nullable=True)              # выученные правила
    custom_settings_json = Column(Text, nullable=True)              # параметры юзера
    # Включённые опциональные скилы модуля (Итерация 4, ТЗ раздел 5.4).
    # CSV-список skill_slug'ов из AGENT_REGISTRY[module_slug]['skills']. Каждый
    # включённый скил добавляет свою price_delta_kop к стоимости invoke_module
    # и расширяет allowed_tools модуля.
    enabled_skills  = Column(Text, nullable=True)
    # cron-расписание этого модуля (опц.) — если задано, scheduler-cron его запустит
    schedule_cron   = Column(String, nullable=True)
    # Последний раз cron-runtime триггерил этот модуль (отдельно от last_used_at,
    # которое для любых вызовов включая ручные команды юзера).
    last_cron_fired_at = Column(DateTime, nullable=True)
    is_enabled      = Column(Boolean, default=True)
    connected_at    = Column(DateTime, default=datetime.utcnow)
    last_used_at    = Column(DateTime, nullable=True)

    agent = relationship("Agent", back_populates="modules")

    __table_args__ = (
        UniqueConstraint("agent_id", "slug", name="uix_agent_module"),
    )


class LlmCache(Base):
    """Cache LLM-вызовов (exact-match по hash). TTL по expires_at, очищается
    периодически scheduler'ом. Экономия 30-50% по ТЗ раздел 4.5 + 7.2.

    Ключ = SHA256(model + JSON(messages) + JSON(extra-filtered)).
    Не учитываем user_api_key / _user_id / _request_id — это не контент.
    """
    __tablename__ = "llm_cache"

    cache_key   = Column(String, primary_key=True, index=True)
    model       = Column(String, nullable=True, index=True)   # для аналитики
    response    = Column(Text, nullable=False)                # JSON сериализованный dict
    purpose     = Column(String, nullable=True, index=True)   # _purpose из extra
    created_at  = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at  = Column(DateTime, nullable=False, index=True)
    hit_count   = Column(Integer, default=0)
    last_hit_at = Column(DateTime, nullable=True)


class AgentMessage(Base):
    """Сообщение в чате с агентом — двусторонний поток между юзером и агентом.

    role:
      user      — сообщение юзера
      assistant — ответ агента (LLM-output)
      system    — служебное (например «расписание изменено»)
      tool      — результат tool-call'а (виден в трейсе, но не в основном UI)

    Используется и для Agent Builder диалога (status=draft), и для последующего
    общения с активным агентом (команды / изменение настроек).
    """
    __tablename__ = "agent_messages"

    id              = Column(Integer, primary_key=True, index=True)
    agent_id        = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    role            = Column(String, nullable=False)            # user|assistant|system|tool
    content         = Column(Text, nullable=True)
    # tool_calls / attachments / mode (build|command|settings) / cost_kop /
    # pending_action_ids (список id'шек PendingAgentAction, который надо
    # отрендерить в UI при отображении этого сообщения)
    meta_json       = Column(Text, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)


class PendingAgentAction(Base):
    """Отложенное действие, которое модуль предложил выполнить.

    Не запускается автоматически — юзер ОБЯЗАТЕЛЬНО подтверждает через UI.
    Это безопасный flow для опасных действий: отправка email, создание
    встречи в Google, пауза рекламной кампании, и т.п.

    Lifecycle:
      pending   → юзер видит preview в чате, кнопки [Отправить] / [Отмена]
      confirmed → tool реально вызван, в result_json — что вернул tool
      cancelled → юзер нажал «Отмена», ничего не делается
      error     → юзер нажал confirm, но tool упал; error в result_json
      expired   → 24 часа не подтверждено — auto-cancel cron'ом

    action_type — slug инструмента (send_email, create_google_event,
    yandex_direct_pause_campaign, vk_ads_pause_campaign, и т.п.).

    params_json — параметры специфичные для tool (to/subject/body для email,
    title/start/end для calendar, и т.д.). preview_text — человекочитаемая
    выжимка для UI (1-3 строки).
    """
    __tablename__ = "pending_agent_actions"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    agent_id        = Column(Integer, ForeignKey("agents.id", ondelete="SET NULL"),
                              nullable=True, index=True)
    module_slug     = Column(String, nullable=False, index=True)  # mail|calendar|direct_ads|...
    action_type     = Column(String, nullable=False, index=True)  # send_email|create_google_event|...
    params_json     = Column(Text, nullable=False)
    preview_text    = Column(Text, nullable=True)                  # для рендера в чате
    status          = Column(String, default="pending", index=True)  # pending|confirmed|cancelled|error|expired
    result_json     = Column(Text, nullable=True)                  # что вернул tool (или error message)
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)
    confirmed_at    = Column(DateTime, nullable=True)
    # Опциональная ссылка на сообщение в чате — для линковки UI
    chat_message_id = Column(Integer, ForeignKey("agent_messages.id", ondelete="SET NULL"),
                              nullable=True)


class WorkoutLog(Base):
    """Структурированный лог тренировки от модуля coach.

    Заменяет свободный формат [LEARNED:fact: 28.05 жим 80×8×4] на чёткие
    поля → точные отчёты по агрегатам (тоннаж, рекорды, динамика).

    Одна запись = одно упражнение в один день. Юзер делает 5 упражнений
    за тренировку → 5 строк. Сеты внутри одного упражнения хранятся в
    sets_json как список словарей: [{"weight": 80, "reps": 8}, ...].
    """
    __tablename__ = "workout_logs"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    workout_date    = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    exercise        = Column(String, nullable=False)   # «жим лёжа», «присед»
    sets_json       = Column(Text, nullable=False)     # JSON [{weight, reps}, ...]
    notes           = Column(Text, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)


class MealLog(Base):
    """Структурированный лог приёма пищи от модуля nutrition.

    Заменяет свободный формат [LEARNED:fact: 26.05 обед курица + рис ≈ 550 ккал]
    на отдельные поля → точная сумма ккал/БЖУ за день/неделю.

    meal_type: breakfast|lunch|dinner|snack
    """
    __tablename__ = "meal_logs"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    meal_date       = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    meal_type       = Column(String, nullable=False)   # breakfast|lunch|dinner|snack
    description     = Column(Text, nullable=False)     # «овсянка + банан + кофе»
    calories        = Column(Integer, nullable=True)
    protein_g       = Column(Integer, nullable=True)
    fat_g           = Column(Integer, nullable=True)
    carbs_g         = Column(Integer, nullable=True)
    notes           = Column(Text, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
