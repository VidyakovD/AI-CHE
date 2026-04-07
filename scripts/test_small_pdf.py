import os, sys, base64, httpx
sys.path.insert(0, "/root/AI-CHE")
os.chdir("/root/AI-CHE")

# Create a tiny PDF for testing
pdf_content = b"""%PDF-1.0
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<<>>>>endobj
xref
0 4
0000000000 65535 f
0000000010 00000 n
0000000053 00000 n
0000000102 00000 n
trailer<</Size 4/Root 1 0 R>>
startxref
160
%%EOF"""

b64 = base64.b64encode(pdf_content).decode()
blocks = [
    {"type": "text", "text": "проанализируй этот документ"},
    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
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
    print(f"Full: {data}")
