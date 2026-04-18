"""
Миграции БД. Запуск: python -m scripts.migrate_db
Или напрямую из корня проекта: python scripts/migrate_db.py
"""
import sqlite3, os

db_path = os.path.join(os.path.dirname(__file__), '..', 'chat.db')
conn = sqlite3.connect(db_path)
c = conn.cursor()

def migrate(sql, label):
    try:
        c.execute(sql)
        print(f'✓ {label}')
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            print(f'- {label} (already exists)')
        else:
            print(f'✗ {label}: {e}')

# ── Users ─────────────────────────────────────────────────────────────────────
migrate('ALTER TABLE users ADD COLUMN is_banned BOOLEAN DEFAULT 0', 'users.is_banned')

# ── Support Requests ──────────────────────────────────────────────────────────
c.execute("""CREATE TABLE IF NOT EXISTS support_requests (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, type TEXT NOT NULL,
    description TEXT, status TEXT DEFAULT 'open', admin_response TEXT,
    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')),
    updated_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
)""")
print('✓ support_requests')

# ── ChatBots (постоянные боты) ────────────────────────────────────────────────
c.execute("""CREATE TABLE IF NOT EXISTS chatbots (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    name TEXT DEFAULT 'Мой бот',
    model TEXT DEFAULT 'gpt',
    system_prompt TEXT,
    tg_token TEXT,
    tg_webhook_set BOOLEAN DEFAULT 0,
    vk_token TEXT,
    vk_group_id TEXT,
    vk_secret TEXT,
    vk_confirmation TEXT,
    vk_confirmed BOOLEAN DEFAULT 0,
    avito_client_id TEXT,
    avito_client_secret TEXT,
    avito_user_id TEXT,
    widget_enabled BOOLEAN DEFAULT 0,
    widget_secret TEXT,
    workflow_json TEXT,
    max_replies_day INTEGER DEFAULT 100,
    cost_per_reply INTEGER DEFAULT 5,
    replies_today INTEGER DEFAULT 0,
    replies_reset_at DATETIME,
    status TEXT DEFAULT 'off',
    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')),
    updated_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
)""")
print('✓ chatbots')

# Если таблица уже существовала без workflow_json — добавим колонку
migrate('ALTER TABLE chatbots ADD COLUMN workflow_json TEXT', 'chatbots.workflow_json')

# ── Usage Logs (подсчёт токенов по запросам) ──────────────────────────────────
c.execute("""CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    ch_charged INTEGER DEFAULT 0,
    used_own_key BOOLEAN DEFAULT 0,
    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
)""")
print('✓ usage_logs')
migrate('CREATE INDEX IF NOT EXISTS idx_usage_logs_user ON usage_logs(user_id)', 'usage_logs.idx_user')
migrate('CREATE INDEX IF NOT EXISTS idx_usage_logs_created ON usage_logs(created_at)', 'usage_logs.idx_created')

# ── ModelPricing: per-token колонки ───────────────────────────────────────────
migrate('ALTER TABLE model_pricing ADD COLUMN ch_per_1k_input REAL DEFAULT 0', 'model_pricing.ch_per_1k_input')
migrate('ALTER TABLE model_pricing ADD COLUMN ch_per_1k_output REAL DEFAULT 0', 'model_pricing.ch_per_1k_output')
migrate('ALTER TABLE model_pricing ADD COLUMN min_ch_per_req INTEGER DEFAULT 1', 'model_pricing.min_ch_per_req')

# ── Workflow Store (key-value для воркфлоу) ───────────────────────────────────
c.execute("""CREATE TABLE IF NOT EXISTS workflow_store (
    id INTEGER PRIMARY KEY,
    bot_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    updated_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
)""")
print('✓ workflow_store')
migrate('CREATE INDEX IF NOT EXISTS idx_wfstore_bot_key ON workflow_store(bot_id, key)', 'workflow_store.idx')

# ── IMAP Credentials ──────────────────────────────────────────────────────────
c.execute("""CREATE TABLE IF NOT EXISTS imap_credentials (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    label TEXT DEFAULT 'Main',
    host TEXT NOT NULL,
    port INTEGER DEFAULT 993,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    use_ssl BOOLEAN DEFAULT 1,
    last_uid INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
)""")
print('✓ imap_credentials')

# ── Knowledge Files (база знаний ботов) ───────────────────────────────────────
c.execute("""CREATE TABLE IF NOT EXISTS knowledge_files (
    id INTEGER PRIMARY KEY,
    bot_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    mime TEXT,
    size INTEGER DEFAULT 0,
    description TEXT,
    tags TEXT,
    summary TEXT,
    facts TEXT,
    content_text TEXT,
    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
)""")
print('✓ knowledge_files')
migrate('CREATE INDEX IF NOT EXISTS idx_kb_bot ON knowledge_files(bot_id)', 'knowledge_files.idx')

conn.commit()
conn.close()
print('\n✅ Миграции завершены')
