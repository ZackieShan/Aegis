"""Where the keys for outside services are kept, and how much each paid service has been used.

``connections.json`` in the data folder (never in the code, never in git) holds, per service, what an
administrator typed in — secrets encrypted with the app's own key — and whether the service is switched
on. ``connections_usage.json`` beside it counts the requests made to services that charge for them, by
month, so a limit can be kept.

One set of keys serves the whole house: every account's assistant uses them. Only an administrator can
see that they are set, change them or remove them, and nobody is shown a key again once it is saved.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

from . import catalog

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_cache: Dict[str, Any] = {"at": 0.0, "stamp": None, "data": None}
_CACHE_SECONDS = 2.0
VERSION = 1


def _off() -> bool:
    """The test suite: never the real keys, never the real files."""
    return bool(os.environ.get("AEGIS_CONNECTIONS_DISABLED"))


def _data_dir() -> str:
    from src.constants import DATA_DIR
    return DATA_DIR


def keys_file() -> str:
    return os.environ.get("AEGIS_CONNECTIONS_FILE") or os.path.join(_data_dir(), "connections.json")


def usage_file() -> str:
    return os.environ.get("AEGIS_CONNECTIONS_USAGE_FILE") or os.path.join(_data_dir(), "connections_usage.json")


# ── reading and writing ────────────────────────────────────────────────────

def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            found = json.load(handle)
        return found if isinstance(found, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as problem:
        logger.error("Could not read %s: %s", os.path.basename(path), problem)
        return {}


def _kept() -> Dict[str, Any]:
    """Everything kept, by service id. Read again when the file changes; otherwise held for a moment."""
    if _off():
        return {}
    path = keys_file()
    now = time.monotonic()
    with _lock:
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            stamp = None
        if _cache["data"] is not None and _cache["stamp"] == stamp and now - _cache["at"] < _CACHE_SECONDS:
            return _cache["data"]
        services = _load(path).get("services") if stamp is not None else {}
        data = {key: value for key, value in (services or {}).items() if isinstance(value, dict)}
        _cache.update(at=now, stamp=stamp, data=data)
        return data


def _keep(services: Dict[str, Any]) -> None:
    if _off():
        raise RuntimeError("connections are switched off (AEGIS_CONNECTIONS_DISABLED)")
    from core.atomic_io import atomic_write_json
    with _lock:
        atomic_write_json(keys_file(), {"version": VERSION, "services": services}, indent=2, mode=0o600)
        _cache.update(at=0.0, stamp=None, data=None)


def forget_cache() -> None:
    with _lock:
        _cache.update(at=0.0, stamp=None, data=None)


def _plain(value: Any) -> str:
    from src.secret_storage import decrypt
    return decrypt(str(value)) if value else ""


# ── what the rest of the app asks ──────────────────────────────────────────

def stored(service_id: str, field: str = "api_key") -> str:
    """What was typed in on the Integrations screen for this service, or ''."""
    kept = (_kept().get(service_id) or {}).get("fields") or {}
    return _plain(kept.get(field)).strip()


def _elsewhere(item: catalog.Field) -> str:
    """The same thing from where it used to be kept: the settings file, then the environment."""
    if _off():
        return ""
    if item.setting:
        try:
            from src.settings import load_settings
            found = str(load_settings().get(item.setting) or "").strip()
            if found:
                return found
        except Exception:
            pass
    for name in item.env:
        found = (os.environ.get(name) or "").strip()
        if found:
            return found
    return ""


def value(service_id: str, field: str = "api_key") -> str:
    """The key (or other field) to use for a service: the Integrations screen first, then the old places."""
    service = catalog.get(service_id)
    if service is None:
        return ""
    found = stored(service_id, field)
    if found:
        return found
    item = service.field(field)
    return _elsewhere(item) if item is not None else ""


def where(service_id: str, field: str = "api_key") -> str:
    """'here' (the Integrations screen), 'settings', 'environment' or '' — for the administrator's screen."""
    service = catalog.get(service_id)
    item = service.field(field) if service else None
    if item is None or _off():
        return ""
    if stored(service_id, field):
        return "here"
    if item.setting:
        try:
            from src.settings import load_settings
            if str(load_settings().get(item.setting) or "").strip():
                return "settings"
        except Exception:
            pass
    return "environment" if any((os.environ.get(name) or "").strip() for name in item.env) else ""


def on(service_id: str) -> bool:
    """Is the service switched on? On unless an administrator switched it off."""
    if _off():
        return False
    return (_kept().get(service_id) or {}).get("on") is not False


def has_key(service_id: str) -> bool:
    service = catalog.get(service_id)
    if service is None:
        return False
    return all(value(service_id, item.name) for item in service.fields if not item.optional)


def ready(service_id: str) -> bool:
    """May the app use this service now: switched on, and with whatever it cannot work without."""
    service = catalog.get(service_id)
    if service is None or not on(service_id):
        return False
    return has_key(service_id) if service.needs_key else True


def usable(service_id: str) -> bool:
    """Ready, and (for a paid service) still inside this month's limit."""
    return ready(service_id) and not over_limit(service_id)


# ── changing what is kept (administrators, through the routes) ─────────────

class Refused(ValueError):
    """What was typed in cannot be kept as it is; the message says why, for the person."""


def _clean(item: catalog.Field, raw: Any) -> str:
    text = str(raw or "").strip()
    if len(text) > 512:
        raise Refused(f"{item.label} is too long to be right.")
    if any(ord(ch) < 32 for ch in text) or (item.secret and " " in text):
        raise Refused(f"{item.label} has spaces or line breaks in it — paste it again.")
    return text


def save(service_id: str, fields: Optional[Dict[str, Any]] = None, on: Optional[bool] = None,
         limit: Optional[int] = None) -> None:
    """Keep what an administrator typed in. A field left out is left as it was; an empty one is removed."""
    service = catalog.get(service_id)
    if service is None:
        raise Refused("That is not a service this app knows.")
    from src.secret_storage import encrypt
    with _lock:
        services = dict(_kept())
        mine = dict(services.get(service_id) or {})
        held = dict(mine.get("fields") or {})
        typed = False
        for name, raw in (fields or {}).items():
            item = service.field(name)
            if item is None:
                raise Refused(f"{service.name} has no field called {name}.")
            text = _clean(item, raw)
            if text:
                held[name] = encrypt(text) if item.secret else text
                typed = True
            else:
                held.pop(name, None)
        mine["fields"] = held
        if on is not None:
            mine["on"] = bool(on)
        if limit is not None:
            if not service.metered:
                raise Refused(f"{service.name} is not paid for by the request, so it has no limit to set.")
            try:
                number = int(limit)
            except (TypeError, ValueError):
                raise Refused("The monthly limit must be a whole number (0 for no limit).")
            if number < 0 or number > 10_000_000:
                raise Refused("The monthly limit must be between 0 (no limit) and 10,000,000.")
            mine["limit"] = number
        if typed:
            mine["changed"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            mine.pop("tried", None)
        services[service_id] = mine
        _keep(services)
    if typed:
        _clear_old_copies(service, set(fields or {}))


def _clear_old_copies(service: catalog.Service, names: set) -> None:
    """Once a key is kept here (encrypted), blank the plain copy the settings file had."""
    olds = [item.setting for item in service.fields if item.name in names and item.setting and item.secret]
    if not olds:
        return
    try:
        from src.constants import SETTINGS_FILE
        from src.settings import save_settings
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if isinstance(saved, dict) and any(saved.get(name) for name in olds):
            for name in olds:
                if saved.get(name):
                    saved[name] = ""
            save_settings(saved)
    except FileNotFoundError:
        pass
    except Exception as problem:
        logger.warning("Could not clear the old copy of a key in the settings file: %s", problem)


def take_from_settings(posted: Dict[str, Any], current: Dict[str, Any]) -> list:
    """A key typed on the older Search settings tab arrives as a settings value (``brave_api_key`` …).

    Keep it here instead, encrypted, and leave that settings value blank in ``current`` (the settings
    about to be saved). Returns the settings keys that were taken. Never raises: a key that cannot be
    kept here stays where it was put, as it always did.
    """
    taken: list = []
    if _off() or not isinstance(posted, dict):
        return taken
    for service in catalog.SERVICES:
        for item in service.fields:
            if not (item.setting and item.secret and item.setting in posted):
                continue
            text = str(posted.get(item.setting) or "").strip()
            if not text:
                continue                    # an empty box is not a request to remove the key kept here
            try:
                save(service.id, {item.name: text})
            except Exception as problem:
                logger.warning("A %s key typed in the settings could not be kept with the others: %s", service.name, problem)
                continue
            current[item.setting] = ""
            taken.append(item.setting)
    return taken


def forget(service_id: str) -> None:
    """Remove everything kept for a service: its keys, its switch, its limit. The count of use stays."""
    service = catalog.get(service_id)
    if service is None:
        raise Refused("That is not a service this app knows.")
    with _lock:
        services = dict(_kept())
        services.pop(service_id, None)
        _keep(services)
    _clear_old_copies(service, {item.name for item in service.fields})


def note_try(service_id: str, ok: bool, said: str) -> None:
    """Remember how the last try of a key went, for the screen."""
    if _off() or catalog.get(service_id) is None:
        return
    with _lock:
        services = dict(_kept())
        mine = dict(services.get(service_id) or {})
        mine["tried"] = {"ok": bool(ok), "said": str(said)[:300], "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        services[service_id] = mine
        _keep(services)


# ── use of the paid services ───────────────────────────────────────────────

def _month(when: Optional[float] = None) -> str:
    return time.strftime("%Y-%m", time.localtime(when))


def count(service_id: str, kind: str = "request", number: int = 1) -> None:
    """One more paid request went out. Never raises: a count that cannot be written is logged and lost."""
    if _off() or number <= 0:
        return
    from core.atomic_io import atomic_write_json
    try:
        with _lock:
            path = usage_file()
            usage = _load(path)
            month = usage.setdefault(service_id, {}).setdefault(_month(), {})
            month["requests"] = int(month.get("requests") or 0) + number
            kinds = month.setdefault("kinds", {})
            kinds[kind] = int(kinds.get(kind) or 0) + number
            month["last"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            for months in usage.values():                      # a year of months is plenty
                if isinstance(months, dict) and len(months) > 14:
                    for old in sorted(months)[:-14]:
                        months.pop(old, None)
            atomic_write_json(path, usage, indent=1, mode=0o600)
    except Exception as problem:
        logger.warning("Could not count a request to %s: %s", service_id, problem)


def used(service_id: str, month: Optional[str] = None) -> Dict[str, Any]:
    """This month's requests to a service: {"requests": n, "kinds": {...}, "last": when}."""
    if _off():
        return {"requests": 0, "kinds": {}, "last": ""}
    with _lock:
        found = (_load(usage_file()).get(service_id) or {}).get(month or _month()) or {}
    return {"requests": int(found.get("requests") or 0), "kinds": dict(found.get("kinds") or {}), "last": str(found.get("last") or "")}


def limit(service_id: str) -> int:
    """The most paid requests a month this app may make to a service. 0 means no limit.

    Until an administrator says otherwise it is what the service gives away each month, so that out of
    the box the app spends nothing.
    """
    service = catalog.get(service_id)
    if service is None or not service.metered:
        return 0
    kept = (_kept().get(service_id) or {}).get("limit")
    if isinstance(kept, int) and kept >= 0:
        return kept
    return service.free_per_month


def over_limit(service_id: str) -> bool:
    cap = limit(service_id)
    return bool(cap) and used(service_id)["requests"] >= cap


def cost_so_far(service_id: str) -> float:
    """What this month's use costs beyond the free requests, in US dollars."""
    service = catalog.get(service_id)
    if service is None or not service.metered:
        return 0.0
    paid = max(0, used(service_id)["requests"] - service.free_per_month)
    return round(paid * service.price_per_1000 / 1000.0, 2)


# ── for the administrator's screen ─────────────────────────────────────────

def _masked(text: str) -> str:
    return ("•" * 6 + text[-4:]) if len(text) >= 12 else ("•" * 6 if text else "")


def describe(service: catalog.Service) -> Dict[str, Any]:
    """One service as the Integrations screen shows it. No key is ever in here, only its last four characters."""
    kept = _kept().get(service.id) or {}
    fields = []
    for item in service.fields:
        text = value(service.id, item.name)
        fields.append({
            "name": item.name, "label": item.label, "secret": item.secret, "optional": item.optional or not service.needs_key,
            "placeholder": item.placeholder, "set": bool(text), "shown": (_masked(text) if item.secret else text),
            "from": where(service.id, item.name),
        })
    found: Dict[str, Any] = {
        "id": service.id, "name": service.name, "group": service.group, "does": service.does,
        "needs_key": service.needs_key, "key_adds": service.key_adds, "signup": service.signup, "cost": service.cost,
        "sends": service.sends, "tools": list(service.tools), "fields": fields,
        "on": on(service.id), "has_key": has_key(service.id), "ready": ready(service.id),
        "tried": kept.get("tried") if isinstance(kept.get("tried"), dict) else None,
        "changed": str(kept.get("changed") or ""),
    }
    if service.metered:
        use = used(service.id)
        found["metered"] = {
            "month": _month(), "requests": use["requests"], "kinds": use["kinds"], "last": use["last"],
            "limit": limit(service.id), "free": service.free_per_month, "price_per_1000": service.price_per_1000,
            "cost": cost_so_far(service.id), "over": over_limit(service.id),
        }
    return found


def overview() -> Dict[str, Any]:
    return {"groups": [{"id": key, "label": label} for key, label in catalog.GROUPS],
            "services": [describe(service) for service in catalog.SERVICES]}
