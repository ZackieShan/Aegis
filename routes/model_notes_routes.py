"""A sentence on each model, for the picker.

    POST /api/model-notes/lookup   {"models": ["id", …]}  → what each is and what to use it for
    PUT  /api/model-notes          {"model", "summary", "use"}  write one (administrators)

Anyone signed in reads them; what an administrator writes is shown to
everyone. The notes themselves are in ``services/model_notes``.
"""

from fastapi import APIRouter, Body, HTTPException, Request

from core.middleware import require_admin
from services import model_notes
from src.auth_helpers import require_user


def setup_model_notes_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["models"])

    @router.post("/model-notes/lookup")
    def model_notes_lookup(request: Request, body: dict = Body(...)):
        require_user(request)
        models = body.get("models") if isinstance(body, dict) else None
        if not isinstance(models, list):
            raise HTTPException(400, "models must be a list")
        return {"notes": model_notes.notes_for(str(m) for m in models if isinstance(m, str))}

    @router.put("/model-notes")
    def model_notes_set(request: Request, body: dict = Body(...)):
        """Write the note for one model (both fields empty removes it)."""
        require_admin(request)
        try:
            note = model_notes.set_note(str(body.get("model") or ""), str(body.get("summary") or ""), str(body.get("use") or ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "note": note}

    return router
