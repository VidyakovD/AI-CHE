import json, os, sys, base64, httpx
sys.path.insert(0, "/root/AI-CHE")
os.chdir("/root/AI-CHE")

os.environ["ANTHROPIC_BASE_URL"] = "https://api.aws-us-east-3.com"
os.environ["ANTHROPIC_API_KEYS"] = "sk-aw-16025441f6b484f331fd1e7fea27ff11"

from ai import anthropic_response, _file_to_base64

# Simulate real message with file attachment
content = {"text": "что в файле?", "file_url": "/uploads/9ae21b1a-bc33-42a2-b673-585ade894743_КП Видео 1.pdf"}

# Same format as stored in DB
messages = [
    {"role": "system", "content": "Ты полезный AI ассистент."},
    {"role": "user", "content": content},
]

print("Testing anthropic_response with PDF attachment...")
result = anthropic_response("claude-sonnet-4.6", messages)
print(f"Result type: {result.get('type')}")
print(f"Result content: {result.get('content', '')[:500]}")
