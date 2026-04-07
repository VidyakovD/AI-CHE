import json, os, sys
sys.path.insert(0, "/root/AI-CHE")
os.chdir("/root/AI-CHE")

# Find uploaded PDFs
uploads = os.listdir("/root/AI-CHE/uploads")
pdfs = [f for f in uploads if f.endswith(".pdf")]
print(f"Uploads: {len(uploads)}, PDFs: {len(pdfs)}")
for f in pdfs:
    path = f"/root/AI-CHE/uploads/{f}"
    try:
        size = os.path.getsize(path)
        print(f"  PDF: {f} ({size} bytes)")
    except:
        print(f"  PDF: {f} (size error)")

# Check what the code actually does
from ai import _file_to_base64

for f in pdfs:
    fp = f"/uploads/{f}"
    try:
        b64, mime = _file_to_base64(fp)
        print(f"READ OK: mime={mime}, b64_len={len(b64)}")
    except Exception as e:
        print(f"READ ERR: {e}")
