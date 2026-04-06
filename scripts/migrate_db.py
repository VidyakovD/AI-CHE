import sqlite3, os

db_path = os.path.join(os.path.dirname(__file__), '..', 'chat.db')
conn = sqlite3.connect(db_path)
c = conn.cursor()

try:
    c.execute('ALTER TABLE users ADD COLUMN is_banned BOOLEAN DEFAULT 0')
    print('is_banned added')
except Exception as e:
    print(f'is_banned skip: {e}')

c.execute("""CREATE TABLE IF NOT EXISTS support_requests (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, type TEXT NOT NULL,
    description TEXT, status TEXT DEFAULT 'open', admin_response TEXT,
    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')),
    updated_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
)""")
print('support_requests ready')

conn.commit()
conn.close()
