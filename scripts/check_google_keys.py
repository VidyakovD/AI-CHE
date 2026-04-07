import httpx, json

GOOGLE_KEY = "AIzaSyAIHS_VwcqtPJn7pO5CrDBfFMpKXa-FIIM"

models_to_test = [
    # Gemini models
    ("gemini-2.5-flash", "generateContent"),
    ("gemini-2.5-pro", "generateContent"),
    ("gemini-2.0-flash", "generateContent"),
    ("gemini-2.0-flash-lite", "generateContent"),
    ("gemini-1.5-flash", "generateContent"),
    ("gemini-1.5-pro", "generateContent"),
    ("gemini-2.0-flash-exp", "generateContent"),
    ("gemini-2.0-flash-thinking-exp", "generateContent"),
    # Imagen
    ("imagen-3.0-generate-001", "predict"),
    ("imagen-3.0-generate-002", "predict"),
    ("imagen-4.0-generate-001", "predict"),
]

for model_name, method in models_to_test:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:{method}"
    if method == "generateContent":
        payload = {"contents": [{"parts": [{"text": "hello"}]}]}
    else:
        payload = {"instances": [{"prompt": "test"}]}
    try:
        r = httpx.post(url, json=payload, timeout=15)
        status = "OK" if r.status_code < 400 else f"FAIL {r.status_code}"
        print(f"  {method:20s}  {model_name:35s} -> {status}")
        if r.status_code == 400 or r.status_code == 403:
            try:
                detail = r.json().get("error", {}).get("message", "")[:120]
                print(f"      detail: {detail}")
            except:
                pass
    except Exception as e:
        print(f"  {method:20s}  {model_name:35s} -> ERR {e}")
