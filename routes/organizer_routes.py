"""Authenticated reverse proxy for the vendored Organizer sub-app.

The Organizer (photo / cinema / music) runs as a loopback-only stdlib HTTP
server managed by src.organizer_bridge. It has NO auth of its own, so every
request must arrive here, behind Aegis's global AuthMiddleware. This router
forwards /organizer/* to the child and returns its response verbatim.

Access model (Phase 1): the whole Organizer is admin-only — it can move and
delete files anywhere on disk, so it is gated like other host-level powers
(e.g. bash). A logged-in non-admin gets 403. The child binds 127.0.0.1 only,
so it is never reachable from the tailnet except through this authed proxy.

URL model: the Organizer's own pages use root-relative URLs (its api()/post()
helpers and asset refs were made prefix-relative), so serving its index at
"/organizer/" (trailing slash) makes every relative fetch resolve back under
"/organizer/". We strip that prefix before forwarding to the child.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response, RedirectResponse

from core.middleware import require_admin
from src.organizer_bridge import BASE_URL, ensure_started, health

# Hop-by-hop headers (RFC 7230) plus framing headers we must not pass through.
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "content-encoding",
}

# One shared client; httpx transparently decompresses upstream responses.
_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))


def setup_organizer_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/organizer")
    async def organizer_root(request: Request):
        require_admin(request)
        # Trailing slash so the Organizer's relative URLs resolve under it.
        return RedirectResponse(url="/organizer/", status_code=307)

    @router.api_route(
        "/organizer/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
    )
    async def organizer_proxy(request: Request, path: str):
        require_admin(request)

        if not health(timeout=0.5):
            # Startup should have launched it; recover if it died.
            if not ensure_started(wait=8.0):
                raise HTTPException(
                    502, "Organizer service is not running")

        url = f"{BASE_URL}/{path}"
        body = await request.body()
        fwd_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _HOP and k.lower() != "cookie"
        }
        try:
            upstream = await _client.request(
                request.method, url,
                params=list(request.query_params.multi_items()),
                content=body or None,
                headers=fwd_headers,
            )
        except httpx.ConnectError:
            raise HTTPException(502, "Organizer service is not reachable")
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Organizer proxy error: {exc}")

        resp_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in _HOP and k.lower() != "content-type"
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=upstream.headers.get("content-type"),
        )

    return router
