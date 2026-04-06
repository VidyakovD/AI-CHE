import sqlite3, os
db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chat.db")
conn = sqlite3.connect(db)
c = conn.cursor()
c.execute("SELECT id, provider, key_value, label, status FROM api_keys ORDER BY provider, id")
rows = c.fetchall()
if not rows:
    print("Таблица api_keys пуста!")
else:
    for r in rows:
        pid, prov, val, lbl, st = r
        preview = val[:12] + "..." + val[-4:] if len(val) > 16 else "***"
        print(f"  id={pid}  prov={prov:20}  key={preview:25}  label={(lbl or ''):20}  status={st}")
conn.close()
