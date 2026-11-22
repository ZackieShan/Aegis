"""Accounts: what a signed-in person can see about their own, and the
administrator's console for everyone's.

    GET  /api/me                              who am I, my role, how my app is set up
    PUT  /api/me                              my own cosmetic choices (name shown, theme, colour)

    GET  /api/admin/overview                  people, roles, and the catalogues the console needs
    POST /api/admin/users                     create an account (role, password or PIN, experience)
    PUT  /api/admin/users/{name}              role, privileges, experience, PIN rule, on/off, default model
    POST /api/admin/users/{name}/password     set a new password or PIN
    POST /api/admin/users/{name}/sign-out     end all of the account's sessions
    DELETE /api/admin/users/{name}?data=keep|erase   delete the account; erase or keep what it stored
    POST /api/admin/removed/{name}/erase      erase what a deleted account left behind
    DELETE /api/admin/removed/{name}          free a deleted account's name (a new account would get its data)
    PUT  /api/admin/roles/{name}              create or change a role
    DELETE /api/admin/roles/{name}            remove a custom role / reset a built-in one
    GET  /api/admin/activity                  sign-ins and administrative changes

Everything under /api/admin requires an administrator. The console shows how
much each person has stored (counts), never what.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from core import access
from core.auth import AuthManager, PASSWORD_MAX_BYTES, RESERVED_USERNAMES, privilege_problems
from core.middleware import require_admin
from src.auth_helpers import get_current_user
from src.constants import PASSWORD_MIN_LENGTH

logger = logging.getLogger(__name__)

PIN_MIN, PIN_MAX = 4, 12
# Parts of the full interface an administrator can hide for one person. Keys
# are the ones the app's own Appearance switches use (static/app.js).
HIDEABLE_UI = [
    ("email-section", "Email"), ("tool-calendar", "Calendar"), ("tool-notes", "Notes"), ("tool-tasks", "Tasks"),
    ("tool-gallery", "Gallery / Studio"), ("tool-library", "Library"), ("tool-memory", "Brain"),
    ("tool-research", "Deep Research"), ("tool-compare", "Compare"), ("tool-recipes", "Recipes"),
    ("tool-cookbook", "Cookbook"), ("tool-theme", "Theme"), ("models-section", "Model list"),
    ("mode-toggle", "Mode switch (Agent / Chat / Code)"), ("bash-toggle-btn", "Shell switch"), ("web-toggle-btn", "Web switch"),
    ("incognito-btn", "Nobody mode button"),
]


def client_address(request: Request) -> str:
    """The caller's address; behind a local reverse proxy, the one it reports."""
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") or ""
    if host in ("127.0.0.1", "::1", "localhost"):
        headers = getattr(request, "headers", None)
        forwarded = (headers.get("x-forwarded-for") or "") if headers is not None else ""
        forwarded = forwarded.split(",")[0].strip() if isinstance(forwarded, str) else ""
        if forwarded:
            return forwarded[:64]
    return host if isinstance(host, str) else ""


def password_problem(password: str, allow_pin: bool) -> Optional[str]:
    """Why this password is not acceptable, or None when it is."""
    password = password or ""
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        return f"That is too long: use at most {PASSWORD_MAX_BYTES} characters."
    # Digits only and short enough to be a PIN: the PIN rules. A longer run of
    # digits is a password like any other, whether or not a PIN is allowed.
    if allow_pin and password.isdigit() and len(password) <= PIN_MAX:
        if len(password) < PIN_MIN:
            return f"A PIN is {PIN_MIN} to {PIN_MAX} digits."
        ascending = "01234567890123456789"
        if len(set(password)) == 1 or password in ascending or password in ascending[::-1]:
            return "That PIN is too easy to guess — avoid repeats and runs like 1111 or 1234."
        return None
    if len(password) < PASSWORD_MIN_LENGTH:
        if allow_pin:
            return f"Use at least {PASSWORD_MIN_LENGTH} characters, or a PIN of {PIN_MIN}–{PIN_MAX} digits."
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters"
    return None


def setup_account_routes(auth_manager: AuthManager, session_manager=None, memory_manager=None,
                         memory_vector=None) -> APIRouter:
    router = APIRouter(tags=["accounts"])

    def _admin(request: Request) -> str:
        require_admin(request)
        return get_current_user(request) or ""

    def _known(username: str) -> str:
        username = (username or "").strip().lower()
        if username not in auth_manager.users:
            raise HTTPException(404, "No such account")
        return username

    def _prefs(username: str) -> dict:
        from routes.prefs_routes import _load_for_user
        return _load_for_user(username) or {}

    def _user_view(username: str, states: Dict[str, dict], roles: Dict[str, dict]) -> Dict[str, Any]:
        record = auth_manager.users[username]
        role = auth_manager.get_role(username)
        state = states.get(username) or {}
        prefs = _prefs(username)
        counts = {"chats": 0, "memories": 0}
        try:
            if session_manager is not None:
                counts["chats"] = len(session_manager.get_sessions_for_user(username) or {})
        except Exception:
            pass
        try:
            if memory_manager is not None:
                counts["memories"] = len(memory_manager.load(owner=username))
        except Exception:
            pass
        experience = auth_manager.get_experience(username)
        return {
            "username": username,
            "display_name": experience.get("display_name") or "",
            "role": role, "role_label": (roles.get(role) or {}).get("label") or role,
            "is_admin": bool(record.get("is_admin")), "disabled": bool(record.get("disabled")),
            "created": record.get("created"),
            "last_sign_in": state.get("last_sign_in"), "sign_ins": int(state.get("sign_ins") or 0),
            "allow_pin": bool(record.get("allow_pin")), "two_factor": bool(record.get("totp_enabled")),
            "experience": experience, "experience_set": sorted((record.get("experience") or {}).keys()),
            "privileges": auth_manager.get_privileges(username),
            # An administrator has everything; nothing is "set for this person".
            "privilege_overrides": {} if record.get("is_admin") else auth_manager.privilege_overrides(username),
            "default_model": {"endpoint_id": prefs.get("default_endpoint_id") or "", "model": prefs.get("default_model") or ""},
            "counts": counts,
        }

    def _left_by(username: str) -> Dict[str, int]:
        from services import account_data
        return account_data.leftover(username, memory_manager=memory_manager)

    def _removed_view() -> list:
        """Deleted accounts whose name is still reserved, newest first, with
        how much each left behind (never what)."""
        out = []
        for name, record in auth_manager.removed().items():
            try:
                left = _left_by(name)
            except Exception:
                logger.warning("could not count what %s left behind", name, exc_info=True)
                left = None
            out.append({"username": name, "at": record.get("at"), "by": record.get("by") or "",
                        "left": left, "erase_failed": record.get("erase_failed") or []})
        return sorted(out, key=lambda entry: entry.get("at") or 0, reverse=True)

    def _erase(request: Request, username: str) -> Dict[str, Any]:
        """Erase what a deleted account stored; free its name once nothing is left."""
        from services import account_data
        state = request.app.state
        result = account_data.erase(
            username, accounts=list(auth_manager.users), memory_manager=memory_manager,
            session_manager=session_manager, memory_vector=memory_vector,
            upload_handler=getattr(state, "upload_handler", None),
            personal_docs_manager=getattr(state, "personal_docs_manager", None))
        try:
            remaining = _left_by(username)["total"]
        except Exception:
            remaining = -1
        if not result["failed"] and remaining == 0:
            auth_manager.release_removed(username)
            result["name_free"] = True
        else:
            auth_manager.note_removed(username, erase_failed=result["failed"] or ["some of it"], erase_tried=time.time())
            result["name_free"] = False
        return result

    # ── the signed-in person's own account ───────────────────────────────

    @router.get("/api/me")
    def me(request: Request):
        username = get_current_user(request)
        if not username or username not in auth_manager.users:
            # Sign-in switched off / single-user mode: the full app, nothing customised.
            return {"username": username or "", "display_name": "", "is_admin": True, "role": "admin",
                    "role_label": "Administrator", "experience": access.normalize_experience(None),
                    "privileges": {}, "tools": list(access.TOOL_GROUPS), "home": "/", "allow_pin": False}
        role = auth_manager.get_role(username)
        experience = auth_manager.get_experience(username)
        return {
            "username": username, "display_name": experience.get("display_name") or username.title(),
            "is_admin": auth_manager.is_admin(username), "role": role,
            "role_label": access.get_role(role).get("label") or role,
            "experience": experience, "privileges": auth_manager.get_privileges(username),
            "tools": access.get_role(role).get("tools") or [],
            "home": "/home" if experience.get("mode") == "simple" else "/",
            "allow_pin": auth_manager.allows_pin(username),
        }

    @router.put("/api/me")
    def me_update(request: Request, body: dict = Body(...)):
        """A person's own cosmetic choices. Which interface they get, what
        the assistant may do and how it is told to talk to them stay with
        the administrator."""
        username = get_current_user(request)
        if not username or username not in auth_manager.users:
            raise HTTPException(400, "No account to change")
        allowed = {key: body[key] for key in ("display_name", "theme", "accent") if key in body}
        if allowed:
            auth_manager.set_experience(username, allowed)
        return me(request)

    # ── the administrator's console ──────────────────────────────────────

    @router.get("/api/admin/overview")
    def overview(request: Request):
        admin = _admin(request)
        roles = access.list_roles()
        states = access.sign_in_state()
        users = [_user_view(name, states, roles) for name in sorted(auth_manager.users)]
        in_use: Dict[str, int] = {}
        for user in users:
            in_use[user["role"]] = in_use.get(user["role"], 0) + 1
        return {
            "me": admin,
            "users": users,
            "removed": _removed_view(),
            "roles": [dict(definition, name=name, in_use=in_use.get(name, 0)) for name, definition in roles.items()],
            "privileges": access.privilege_catalogue(),
            "tool_groups": [{"id": key, "label": group["label"], "description": group["description"]}
                            for key, group in access.TOOL_GROUPS.items()],
            "sections": [{"id": key, "label": access.SECTION_LABELS[key]} for key in access.HOME_SECTIONS],
            "themes": list(access.THEMES), "modes": list(access.MODES),
            "hideable_ui": [{"id": key, "label": label} for key, label in HIDEABLE_UI],
            "policy": {"password_min_length": PASSWORD_MIN_LENGTH, "pin_min": PIN_MIN, "pin_max": PIN_MAX,
                       "signup_enabled": bool(auth_manager.signup_enabled)},
        }

    @router.post("/api/admin/users")
    def create_user(request: Request, body: dict = Body(...)):
        admin = _admin(request)
        username = str(body.get("username") or "").strip().lower()
        role = str(body.get("role") or "member")
        password = str(body.get("password") or "")
        allow_pin = bool(body.get("allow_pin"))
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,31}", username):
            raise HTTPException(400, "A username is 2–32 letters, digits, dots, dashes or underscores.")
        if username in RESERVED_USERNAMES:
            raise HTTPException(400, "That username is reserved.")
        if username in auth_manager.users:
            raise HTTPException(409, "That username is taken.")
        if username in auth_manager.removed():
            raise HTTPException(409, f"“{username}” was the name of an account that was deleted, and what it stored is "
                                     "still on this server. Erase that or free the name under People → Deleted "
                                     "accounts, or choose another name.")
        if not access.role_exists(role):
            raise HTTPException(400, "No such role.")
        problem = password_problem(password, allow_pin)
        if problem:
            raise HTTPException(400, problem)
        if not auth_manager.create_user(username, password, is_admin=(role == "admin")):
            raise HTTPException(400, "Could not create the account.")
        if role != "admin":
            auth_manager.set_role(username, role)
        if allow_pin:
            auth_manager.set_allow_pin(username, True)
        experience = dict(body.get("experience") or {})
        if body.get("display_name"):
            experience["display_name"] = body["display_name"]
        if experience:
            auth_manager.set_experience(username, experience)
        access.audit("account_created", user=username, by=admin, ip=client_address(request), detail=f"role {role}")
        return {"ok": True, "user": _user_view(username, access.sign_in_state(), access.list_roles())}

    @router.put("/api/admin/users/{username}")
    def update_user(request: Request, username: str, body: dict = Body(...)):
        admin = _admin(request)
        username = _known(username)
        changed = []
        if "role" in body:
            role = str(body["role"])
            current = auth_manager.get_role(username)
            if role != current:
                if role == "admin" or current == "admin":
                    from core.auth import SetAdminResult
                    result = auth_manager.set_admin(username, role == "admin", admin)
                    if result != SetAdminResult.OK:
                        raise HTTPException(400, "That would leave no administrator." if result == SetAdminResult.LAST_ADMIN
                                            else "Could not change the administrator setting.")
                    if role != "admin":
                        auth_manager.set_role(username, role)
                elif not auth_manager.set_role(username, role):
                    raise HTTPException(400, "No such role.")
                changed.append(f"role {current} → {role}")
        if isinstance(body.get("privileges"), dict) and not auth_manager.is_admin(username):
            wrong = privilege_problems(body["privileges"])
            if wrong:
                raise HTTPException(400, "Not a valid value for: " + ", ".join(sorted(wrong))
                                    + ". A switch is true or false; a limit is a whole number, 0 or more.")
            auth_manager.set_privileges(username, body["privileges"])
            changed.append("privileges " + ", ".join(sorted(body["privileges"])))
        if isinstance(body.get("experience"), dict):
            auth_manager.set_experience(username, body["experience"])
            changed.append("experience " + ", ".join(sorted(body["experience"])))
        if isinstance(body.get("allow_pin"), bool):
            auth_manager.set_allow_pin(username, body["allow_pin"])
            changed.append(f"PIN {'allowed' if body['allow_pin'] else 'not allowed'}")
        if isinstance(body.get("disabled"), bool):
            if not auth_manager.set_disabled(username, body["disabled"], admin):
                raise HTTPException(400, "You cannot switch off your own account or the last administrator.")
            changed.append("switched off" if body["disabled"] else "switched on")
        if isinstance(body.get("default_model"), dict):
            from routes.prefs_routes import _load_for_user, _save_for_user
            prefs = _load_for_user(username) or {}
            model = str(body["default_model"].get("model") or "").strip()
            endpoint = str(body["default_model"].get("endpoint_id") or "").strip()
            if model and endpoint:
                prefs["default_model"], prefs["default_endpoint_id"] = model, endpoint
            else:
                prefs.pop("default_model", None)
                prefs.pop("default_endpoint_id", None)
            _save_for_user(username, prefs)
            changed.append(f"default model {model or 'follows the shared default'}")
        if changed:
            access.audit("account_changed", user=username, by=admin, ip=client_address(request), detail="; ".join(changed))
        return {"ok": True, "user": _user_view(username, access.sign_in_state(), access.list_roles())}

    @router.post("/api/admin/users/{username}/password")
    def reset_password(request: Request, username: str, body: dict = Body(...)):
        admin = _admin(request)
        username = _known(username)
        problem = password_problem(str(body.get("password") or ""), auth_manager.allows_pin(username))
        if problem:
            raise HTTPException(400, problem)
        auth_manager.admin_set_password(username, str(body["password"]))
        access.audit("password_reset", user=username, by=admin, ip=client_address(request))
        return {"ok": True}

    @router.post("/api/admin/users/{username}/sign-out")
    def sign_out_everywhere(request: Request, username: str):
        admin = _admin(request)
        username = _known(username)
        keep = request.cookies.get("aegis_session") if username == admin else None
        ended = auth_manager.revoke_user_sessions(username, except_token=keep)
        access.audit("signed_out_everywhere", user=username, by=admin, ip=client_address(request),
                     detail=f"{ended} session{'' if ended == 1 else 's'}")
        return {"ok": True, "ended": ended}

    @router.delete("/api/admin/users/{username}")
    def delete_user(request: Request, username: str, data: str = "keep"):
        """Delete an account. ``data=erase`` also erases everything it stored;
        otherwise that stays on the server, out of everyone's reach, and the
        name stays reserved until it is erased or freed (see ``/api/admin/removed``)."""
        admin = _admin(request)
        username = _known(username)
        if data not in ("keep", "erase"):
            raise HTTPException(400, "data is keep or erase")
        if username == admin:
            raise HTTPException(400, "You cannot delete your own account.")
        if not auth_manager.delete_user(username, admin):
            raise HTTPException(400, "Could not delete the account.")
        invalidate = getattr(request.app.state, "invalidate_token_cache", None)
        if callable(invalidate):
            invalidate()                                   # their API tokens went with the account
        from services import account_data
        account_data.forget_account_settings(username)     # their phones, preferences, sign-in record
        result: Dict[str, Any] = {"ok": True, "data": data}
        if data == "erase":
            erased = _erase(request, username)
            result.update(removed=erased["removed"], failed=erased["failed"], name_free=erased["name_free"])
        else:
            try:
                left = _left_by(username)
            except Exception:
                left = None
            if left is not None and left["total"] == 0:
                auth_manager.release_removed(username)     # it never stored anything: nothing to reserve the name for
            result.update(left=left, name_free=left is not None and left["total"] == 0)
        access.audit("account_deleted", user=username, by=admin, ip=client_address(request),
                     detail="data erased" if data == "erase" and result.get("name_free") else
                            ("data partly erased" if data == "erase" else "data kept"))
        return result

    @router.post("/api/admin/removed/{username}/erase")
    def erase_removed(request: Request, username: str):
        """Erase what a deleted account left on the server."""
        admin = _admin(request)
        username = (username or "").strip().lower()
        if username not in auth_manager.removed() or username in auth_manager.users:
            raise HTTPException(404, "No deleted account of that name.")
        erased = _erase(request, username)
        access.audit("data_erased", user=username, by=admin, ip=client_address(request),
                     detail="" if erased["name_free"] else "not all of it")
        return {"ok": True, **erased}

    @router.delete("/api/admin/removed/{username}")
    def free_removed(request: Request, username: str):
        """Free a deleted account's name without erasing what it left: the
        next account given that name gets it."""
        admin = _admin(request)
        username = (username or "").strip().lower()
        if not auth_manager.release_removed(username):
            raise HTTPException(404, "No deleted account of that name.")
        access.audit("name_freed", user=username, by=admin, ip=client_address(request))
        return {"ok": True}

    @router.put("/api/admin/roles/{name}")
    def save_role(request: Request, name: str, body: dict = Body(...)):
        admin = _admin(request)
        name = (name or "").strip().lower()
        try:
            role = access.save_role(name, body or {})
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        access.audit("role_changed", by=admin, ip=client_address(request), detail=name)
        return {"ok": True, "role": dict(role, name=name)}

    @router.delete("/api/admin/roles/{name}")
    def delete_role(request: Request, name: str):
        admin = _admin(request)
        name = (name or "").strip().lower()
        builtin = name in access.BUILTIN_ORDER
        if not builtin and any(auth_manager.get_role(user) == name for user in auth_manager.users):
            raise HTTPException(409, "Accounts still have this role. Give them another one first.")
        if not access.delete_role(name):
            raise HTTPException(404, "Nothing to remove.")
        access.audit("role_reset" if builtin else "role_deleted", by=admin, ip=client_address(request), detail=name)
        return {"ok": True}

    @router.get("/api/admin/activity")
    def activity(request: Request, limit: int = 200, user: Optional[str] = None):
        _admin(request)
        return {"entries": access.read_audit(limit=min(max(limit, 1), 1000), user=(user or "").strip().lower() or None)}

    return router
