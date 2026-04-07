"""Simple reverse proxy for Anthropic API - runs on NL server via nginx."""
import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI()
http_client = httpx.AsyncClient(timeout=120)

@app.api_route("/{path:path}", methods=["GET","POST","PUT","DELETE"])
async def proxy(request: Request, path: str):
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }
    headers["host"] = "api.anthropic.com"

    resp = await http_client.request(
        method=request.method,
        url=f"https://api.anthropic.com/{path}",
        content=body,
        headers=headers,
    )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()},
    )
