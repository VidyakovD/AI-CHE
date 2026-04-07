import base64, httpx

# Create a tiny 1x1 PNG
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
b64 = base64.b64encode(png).decode()

blocks = [
    {"type": "text", "text": "что это за изображение?"},
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
]

payload = {
    "model": "claude-sonnet-4.6",
    "max_tokens": 1024,
    "stream": False,
    "system": "Ты полезный AI ассистент.",
    "messages": [{"role": "user", "content": blocks}]
}

key = "sk-aw-16025441f6b484f331fd1e7fea27ff11"
r = httpx.post(
    "https://api.aws-us-east-3.com/v1/messages",
    json=payload,
    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    timeout=120
)
print(f"Status: {r.status_code}")
data = r.json()
text = data.get("content", [{}])[0].get("text", "")
print(f"Response: {text[:500]}")
if "error" in str(data):
    print(f"Full error: {data}")
