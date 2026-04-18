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

conn.commit()
conn.close()
print('\n✅ Миграции завершены')
