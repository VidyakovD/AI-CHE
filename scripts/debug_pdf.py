import json, os, sys
sys.path.insert(0, "/root/AI-CHE")
os.chdir("/root/AI-CHE")

os.environ["ANTHROPIC_BASE_URL"] = "https://api.aws-us-east-3.com"
os.environ["ANTHROPIC_API_KEYS"] = "sk-aw-16025441f6b484f331fd1e7fea27ff11"

content = {"text": "что в файле?", "file_url": "/uploads/9ae21b1a-bc33-42a2-b673-585ade894743_КП Видео 1.pdf"}
print(f"Input type: {type(content)}")
print(f"file_url in content: {'file_url' in content}")

from ai import _prepare_claude_content, _file_to_base64

blocks = _prepare_claude_content(content)
print(f"Blocks count: {len(blocks)}")
for i, b in enumerate(blocks):
    print(f"  Block {i}: type={b.get('type')}")
    if "source" in b:
        src = b["source"]
        print(f"    source: type={src.get('type')}, media_type={src.get('media_type')}, data_len={len(src.get('data',''))}")

# Now test the full Claude API call
import httpx

payload = {
    "model": "claude-sonnet-4.6",
    "max_tokens": 1024,
    "stream": False,
    "system": "Ты полезный AI ассистент.",
    "messages": [
        {"role": "user", "content": blocks}
    ]
}

print(f"\nPayload: {json.dumps(payload, ensure_ascii=False)[:300]}...")

key = "sk-aw-16025441f6b484f331fd1e7fea27ff11"
r = httpx.post(
    "https://api.aws-us-east-3.com/v1/messages",
    json=payload,
    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    timeout=120
)
print(f"Status: {r.status_code}")
data = r.json()
print(f"Response: {data.get('content', [{}])[0].get('text', '')[:500]}")
if "error" in str(data):
    print(f"Full: {data}")
