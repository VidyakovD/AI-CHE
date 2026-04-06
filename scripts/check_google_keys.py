import sqlite3, os, httpx

db_path = os.path.join(os.path.dirname(__file__), '..', 'chat.db')
conn = sqlite3.connect(db_path)
c = conn.cursor()
rows = c.execute("SELECT provider, key_value FROM api_keys WHERE provider IN ('gemini','google','nano','veo','veo_project_id')").fetchall()

for provider, key_value in rows:
    print(f"\n=== provider={provider} key={key_value[:20]}... ===")

    if provider == 'veo_project_id':
        print(f"  Project ID: {key_value}")
        continue

    # Test 1: v1beta gemini-2.0-flash
    url1 = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    try:
        r = httpx.post(url1, json={"contents":[{"parts":[{"text":"hi"}]}]}, timeout=10)
        print(f"  gemini-2.0-flash (v1beta): {r.status_code}")
    except Exception as e:
        print(f"  gemini-2.0-flash (v1beta): ERR {e}")

    # Test 2: v1 gemini-pro
    url2 = "https://generativelanguage.googleapis.com/v1/models/gemini-pro:generateContent"
    try:
        r = httpx.post(url2, json={"contents":[{"parts":[{"text":"hi"}]}]}, timeout=10)
        print(f"  gemini-pro (v1): {r.status_code}")
    except Exception as e:
        print(f"  gemini-pro (v1): ERR {e}")

    # Test 3: imagen-3.0-generate-001
    url3 = "https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-001:predict"
    try:
        r = httpx.post(url3, json={"instances":[{"prompt":"test"}]}, timeout=10)
        print(f"  imagen-3.0-generate-001 (v1beta): {r.status_code}")
        if r.status_code != 200:
            print(f"    {r.text[:150]}")
    except Exception as e:
        print(f"  imagen-3.0-generate-001 (v1beta): ERR {e}")

conn.close()
