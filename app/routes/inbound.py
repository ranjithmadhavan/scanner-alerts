"""The public address charting tools POST to. No login: the secret in the URL is the key (see app/inbound.py)."""

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app import inbound

router = APIRouter()


@router.post("/hook/{token}")
async def receive(token: str, request: Request):
    if int(request.headers.get("content-length") or 0) > inbound.MAX_BODY:
        return JSONResponse({"ok": False, "error": f"Send at most {inbound.MAX_BODY} bytes"}, status_code=413)
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > inbound.MAX_BODY:
            break
    status, text = await run_in_threadpool(inbound.receive, token, raw)
    return JSONResponse({"ok": True} if status == 200 else {"ok": False, "error": text}, status_code=status)
