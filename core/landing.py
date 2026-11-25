"""Which app a signed-in person gets from "/": the family screen or the full app.

Two screens, one line between them:

* the **family screen** (``/home``) — chat that does things, lists, reminders, trackers, boards and the
  pages a card opens (Kitchen, Reader, Translate, Almanac, the memory page). Everyone can use it.
* the **full app** (``/``, "the workshop") — everything else, including the server's own controls. It is
  the administrator's. An administrator can let one person in (``allow_full_app``), and that person
  gets it without the controls.

An administrator moves between the two with a switch that is remembered per browser (a cookie), so a
phone can stay in family mode while a desk stays in the workshop. These functions are the whole rule;
``app.py`` only reads the request and calls them.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

VIEW_COOKIE = "aegis_view"          # "family": this browser stays on the family screen until told otherwise
FAMILY, WORKSHOP = "family", "workshop"


def may_open_full_app(experience: Optional[Dict[str, Any]], is_admin: bool) -> bool:
    """Whether the full app is this person's to open."""
    if is_admin:
        return True
    experience = experience or {}
    return experience.get("mode") != "simple" or bool(experience.get("allow_full_app"))


def lands_on_home(experience: Optional[Dict[str, Any]], is_admin: bool, wants_full: bool = False, view: str = "") -> bool:
    """True when "/" should show the family screen.

    ``wants_full`` is the ``?full`` on the address (how the family screen's own door asks for the full app);
    ``view`` is the browser's remembered choice."""
    experience = experience or {}
    if experience.get("mode") == "simple":
        if is_admin:                                    # an administrator who set their own account to the family screen
            return not wants_full
        return not (experience.get("allow_full_app") and wants_full)
    return view == FAMILY and not wants_full


def switch_target(view: str, allowed_full: bool) -> Optional[str]:
    """Where the switch leads, or None when ``view`` is not one of the two."""
    if view == FAMILY:
        return "/home"
    if view == WORKSHOP:
        return "/?full=1" if allowed_full else "/home"
    return None
