import sqlite3, os
db_path = os.path.join(os.path.dirname(__file__), '..', 'chat.db')
conn = sqlite3.connect(db_path)
c = conn.cursor()

for key, val, desc in [
    ('tg_bot_token', '', 'Token Telegram bota (dlya uv'),
    ('tg_admin_chat_id', '', 'Chat ID admina v Telegram'),
]:
    try:
        c.execute("INSERT OR IGNORE INTO pricing_settings (key,value,description) VALUES (?, ?, ?)", (key, val, desc))
        print(f"  {key}: added (или уже есть)")
    except Exception as e:
        print(f"  {key}: {e}")

conn.commit()
rows = c.execute("SELECT key, value FROM pricing_settings WHERE key LIKE '%tg%'").fetchall()
for r in rows:
    print(f"  {r[0]} = '{r[1]}'")
conn.close()
