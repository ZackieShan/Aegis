"""Turning phone notifications on and off for the signed-in person.

    GET  /api/push/key            the server's public key (to subscribe with)
    GET  /api/push/devices        this account's devices (no keys)
    POST /api/push/subscribe      body: the browser's PushSubscription JSON
    POST /api/push/unsubscribe    body: {endpoint}
    POST /api/push/test           send a test notification to this account's devices
"""

from fastapi import APIRouter, Body, HTTPException, Request

from services import push
from src.auth_helpers import require_user


def setup_push_routes() -> APIRouter:
    router = APIRouter(prefix="/api/push", tags=["push"])

    @router.get("/key")
    def key(request: Request):
        require_user(request)
        return {"key": push.public_key(), "enabled": push.enabled()}

    @router.get("/devices")
    def devices(request: Request):
        return {"devices": push.devices(require_user(request) or None)}

    @router.post("/subscribe")
    def subscribe(request: Request, body: dict = Body(...)):
        owner = require_user(request) or None
        agent = request.headers.get("user-agent", "")
        if not push.subscribe(owner, body, agent):
            raise HTTPException(400, "This browser's notification address is not one I can send to.")
        return {"ok": True, "devices": len(push.devices(owner))}

    @router.post("/unsubscribe")
    def unsubscribe(request: Request, body: dict = Body(...)):
        owner = require_user(request) or None
        return {"ok": push.unsubscribe(owner, str(body.get("endpoint") or ""))}

    @router.post("/test")
    async def test(request: Request):
        owner = require_user(request) or None
        sent = await push.send(owner, "Notifications are on", "Reminders will show up here, even with the app closed.",
                               url="/home", tag="push-test")
        return {"ok": sent > 0, "sent": sent, "devices": len(push.devices(owner))}

    return router
