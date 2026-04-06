import sqlite3, os
db_path = os.path.join(os.path.dirname(__file__), '..', 'chat.db')
conn = sqlite3.connect(db_path)
c = conn.cursor()
cols = c.execute("PRAGMA table_info(api_keys)").fetchall()
print("Структура api_keys:")
for col in cols:
    print(f"  {col[1]} ({col[2]})")
print()
rows = c.execute("SELECT id, provider, key_value, label, status FROM api_keys").fetchall()
for r in rows:
    preview = r[2][:10] + "..." if len(r[2]) > 10 else r[2]
    print(f"  id={r[0]}  provider={r[1]}  key={preview}  label={r[3]}  status={r[4]}")
conn.close()
