import contextlib
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite:///./chat.db"

engine = create_engine(
    DATABASE_URL,
    # 30s busy_timeout — спасает от "database is locked" под нагрузкой
    # (несколько uvicorn workers + scheduler + IMAP параллельно).
    connect_args={"check_same_thread": False, "timeout": 30},
)


# WAL + foreign_keys включаем на каждое новое соединение.
# WAL: writers не блокируют readers (критично для длинных AI-вызовов).
# foreign_keys: SQLite по умолчанию ВЫКЛ — без этого CASCADE/FK не работают.
from sqlalchemy import event as _sa_event


@_sa_event.listens_for(engine, "connect")
def _sqlite_pragma_on_connect(dbapi_connection, _connection_record):
    cur = dbapi_connection.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")  # баланс между скоростью и надёжностью
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
    finally:
        cur.close()


SessionLocal = sessionmaker(bind=engine)

Base = declarative_base()


@contextlib.contextmanager
def db_session():
    """
    Контекст-менеджер для разовых сессий вне FastAPI Depends.
    Гарантирует rollback при исключении и закрытие в любом случае.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Lightweight schema migrations (без Alembic) ─────────────────────────────
# SQLAlchemy create_all() умеет создавать новые таблицы, но НЕ умеет добавлять
# колонки к существующим. Для прода используем идемпотентный ALTER TABLE.
#
# Как добавить миграцию: append (table, column, sql_type) к LIGHTWEIGHT_MIGRATIONS.
# Запуск произойдёт автоматически при startup.

LIGHTWEIGHT_MIGRATIONS: list[tuple[str, str, str]] = [
    # Уведомления о низком балансе
    ("users", "low_balance_threshold", "INTEGER DEFAULT 100"),
    ("users", "low_balance_alerted_at", "DATETIME"),
    # Однократность приветственного бонуса (atomic gate — см. routes/auth.py).
    ("users", "welcome_bonus_claimed_at", "DATETIME"),
    # Однократность реферального бонуса за регистрацию (на referred-user).
    ("users", "referral_signup_bonus_paid_at", "DATETIME"),
    # MAX (мессенджер VK group)
    ("chatbots", "max_token", "VARCHAR"),
    ("chatbots", "max_webhook_set", "BOOLEAN DEFAULT 0"),
    # Привязка чат-бота к сайтам и презентациям (виджет встраивается автоматически)
    ("site_projects", "attached_bot_id", "INTEGER"),
    ("presentation_projects", "attached_bot_id", "INTEGER"),
    ("presentation_projects", "image_paths", "TEXT"),
    # Конструктор ботов: parent_bot_id указывает на бот-конструктор,
    # который сгенерил этот бот через AI-диалог в TG/MAX.
    ("chatbots", "parent_bot_id", "INTEGER"),
    ("chatbots", "auto_generated", "BOOLEAN DEFAULT 0"),
    # Фоновая генерация сайта (вместо синхронной — клиент таймаутил после 60-90 сек)
    ("site_projects", "gen_status", "VARCHAR"),
    ("site_projects", "gen_started_at", "DATETIME"),
    ("site_projects", "gen_progress", "VARCHAR"),
    ("site_projects", "gen_error", "TEXT"),
    ("site_projects", "enhanced_spec", "TEXT"),
    # Виджет: список доменов, с которых разрешён WS-коннект (через запятую).
    # Пусто/NULL = разрешено любым (legacy back-compat).
    ("chatbots", "widget_allowed_origins", "TEXT"),
    # Лимит дочерних ботов через AI-конструктор — защита от runaway-creation.
    ("users", "max_auto_bots", "INTEGER DEFAULT 5"),
    # Прайс-лист бота: vector embedding для semantic search
    ("bot_price_items", "embedding_json", "TEXT"),
    # Security: уведомления о входе с нового IP
    ("users", "last_login_ip", "VARCHAR"),
    ("users", "last_login_at", "DATETIME"),
    # КП: CRM lifecycle (этап B.6 спринта проектов КП)
    ("proposal_projects", "crm_stage", "VARCHAR DEFAULT 'new'"),
    ("proposal_projects", "opened_at", "DATETIME"),
    ("proposal_projects", "replied_at", "DATETIME"),
    ("proposal_projects", "won_at", "DATETIME"),
    ("proposal_projects", "lost_at", "DATETIME"),
    ("proposal_projects", "public_token", "VARCHAR"),
    ("proposal_projects", "outbox_message_id", "VARCHAR"),
    # КП собственный прайс-лист (вместо подтягивания из ChatBot.BotPriceItem).
    # Новые таблицы proposal_price_lists/items создаются Base.metadata.create_all.
    ("proposal_projects", "price_list_id", "INTEGER"),
    # КП: расширенная персонализация бренда
    ("proposal_brands", "tagline", "VARCHAR"),
    ("proposal_brands", "usp_list", "TEXT"),
    ("proposal_brands", "guarantees", "TEXT"),
    ("proposal_brands", "tone", "VARCHAR DEFAULT 'business'"),
    ("proposal_brands", "intro_phrase", "VARCHAR"),
    ("proposal_brands", "cta_phrase", "VARCHAR"),
    # Telegram-бот управления (привязка юзера)
    ("users", "tg_user_id", "VARCHAR"),
    ("users", "tg_username", "VARCHAR"),
    ("users", "tg_link_code", "VARCHAR"),
    ("users", "tg_link_expires", "DATETIME"),
    ("users", "tg_notify_proposals", "BOOLEAN DEFAULT 1"),
    ("users", "tg_notify_records", "BOOLEAN DEFAULT 1"),
    ("users", "tg_notify_errors", "BOOLEAN DEFAULT 1"),
]

# Indexes/constraints — CREATE INDEX IF NOT EXISTS идемпотентен
# Нужны для защиты от double-spend (UNIQUE yookassa_payment_id)
LIGHTWEIGHT_INDEXES: list[tuple[str, str]] = [
    # (index_name, full_sql_create_statement)
    ("uq_subscriptions_yookassa_id",
     "CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_yookassa_id "
     "ON subscriptions(yookassa_payment_id) WHERE yookassa_payment_id IS NOT NULL"),
    # ВАЖНО: было INDEX (не UNIQUE) — при гонке webhook+confirm можно было
    # вставить две Transaction с одним payment_id и зачислить реф. бонус 2×.
    # Теперь UNIQUE — second INSERT уйдёт в IntegrityError → catch → rollback.
    ("uq_transactions_yookassa_id",
     "CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_yookassa_id "
     "ON transactions(yookassa_payment_id) WHERE yookassa_payment_id IS NOT NULL"),
    # Один юзер — одна активация промокода (защита от race в /promo/apply,
    # где SELECT → INSERT не атомарно, два параллельных запроса оба пройдут).
    ("uq_promo_uses_code_user",
     "CREATE UNIQUE INDEX IF NOT EXISTS uq_promo_uses_code_user "
     "ON promo_uses(code_id, user_id)"),
]


def apply_lightweight_migrations():
    """Идемпотентно добавляет недостающие колонки и индексы (SQLite)."""
    from sqlalchemy import text
    import logging
    log = logging.getLogger(__name__)
    with engine.connect() as conn:
        for table, col, sql_type in LIGHTWEIGHT_MIGRATIONS:
            try:
                rows = list(conn.execute(text(f"PRAGMA table_info({table})")))
            except Exception as e:
                log.warning(f"migration: cannot read {table}: {e}")
                continue
            existing = {row[1] for row in rows}
            if col in existing:
                continue
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}"))
                conn.commit()
                log.info(f"migration: added {table}.{col} {sql_type}")
            except Exception as e:
                log.error(f"migration: failed to add {table}.{col}: {e}")
        # Indexes
        for name, sql in LIGHTWEIGHT_INDEXES:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception as e:
                log.warning(f"migration: index {name}: {e}")