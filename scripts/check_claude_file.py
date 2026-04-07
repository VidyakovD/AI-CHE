import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.chdir(os.path.join(os.path.dirname(__file__), '..'))

# Check what parse() returns
content = '{"text": "test", "file_url": "/uploads/test.pdf"}'

def parse(c):
    try:
        p = json.loads(c)
        if isinstance(p, dict) and "file_url" in p:
            return p
    except:
        pass
    return c

result = parse(content)
print(f"type: {type(result)}")
print(f"file_url: {'file_url' in result if isinstance(result, dict) else 'NO'}")

# Check that _prepare_claude_content handles dict correctly
from ai import _prepare_claude_content, _file_to_base64

blocks = _prepare_claude_content(result)
print(f"blocks count: {len(blocks)}")
for b in blocks:
    print(f"  type={b.get('type')}")
    if 'source' in b:
        print(f"    media_type={b['source'].get('media_type')}")
