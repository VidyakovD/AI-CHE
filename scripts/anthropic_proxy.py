"""Simple reverse proxy for Anthropic API - runs on NL server via nginx."""
import os, logging
import httpx
from fastapi import FastAPI, Request, Response, HTTPException

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

PROXY_TOKEN = os.getenv("PROXY_TOKEN", "")
if not PROXY_TOKEN:
    log.warning("PROXY_TOKEN not set — proxy is OPEN (set env var for security)")

app = FastAPI()
http_client = httpx.AsyncClient(timeout=120)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(request: Request, path: str):
    if PROXY_TOKEN:
        auth = request.headers.get("x-proxy-token", "")
        if auth != PROXY_TOKEN:
            raise HTTPException(403, "Invalid proxy token")

    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding", "x-proxy-token")
    }
    headers["host"] = "api.anthropic.com"

    try:
        resp = await http_client.request(
            method=request.method,
            url=f"https://api.anthropic.com/{path}",
            content=body,
            headers=headers,
        )
    except httpx.TimeoutException:
        log.error(f"Timeout proxying {request.method} /{path}")
        raise HTTPException(504, "Gateway timeout")
    except httpx.ConnectError as e:
        log.error(f"Connection error: {e}")
        raise HTTPException(502, "Bad gateway")

    log.info(f"{request.method} /{path} → {resp.status_code}")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()},
    )
