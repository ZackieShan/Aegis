"""Which machine a chat is coding on.

Code mode has a machine picker. The choice is kept here per chat, on the
server, and looked up on every tool call (``src/tool_execution.py``): while a
chat's target is another machine, its shell and file tools run there instead
of on this server. Keeping it server-side means a follow-up that starts
without the browser (a background job finishing) still lands on the right
machine. Only an administrator's chat can hold a target.

``local`` — this server — is the default and is stored as "no target".
"""

from __future__ import annotations

from typing import Optional

from . import common, hosts

_KEEP = 400            # most recent chats; older choices are forgotten


def bind(session_id: str, host_name: str, folder: Optional[str] = None) -> dict:
    """Set the machine this chat codes on. ``local`` (or nothing) clears it.
    ``folder`` is where its shell starts and relative paths resolve; when
    omitted, a folder chosen earlier for the same machine is kept."""
    session_id = str(session_id or "").strip()
    if not session_id:
        raise common.WorkbenchError("No chat to set a machine for.")
    host = hosts.get(host_name)                      # raises for a name that is not registered
    if host.get("builtin"):
        clear(session_id)
        return describe(session_id)

    def mutate(data: dict):
        targets = data.setdefault("session_targets", {})
        current = targets.get(session_id) or {}
        keep_folder = current.get("folder", "") if current.get("host") == host["name"] else ""
        targets[session_id] = {"host": host["name"], "folder": keep_folder if folder is None else str(folder).strip(),
                               "at": common.now()}
        if len(targets) > _KEEP:
            for stale in sorted(targets, key=lambda sid: targets[sid].get("at") or 0)[: len(targets) - _KEEP]:
                del targets[stale]

    common.update(mutate)
    return describe(session_id)


def clear(session_id: str) -> None:
    session_id = str(session_id or "").strip()
    if session_id and session_id in common.load().get("session_targets", {}):
        common.update(lambda data: data.get("session_targets", {}).pop(session_id, None))


def remote(session_id: Optional[str]) -> Optional[dict]:
    """The other machine this chat's tools run on, or None for this server.
    Called on every tool call — the store is cached, so this is a lookup."""
    if not session_id or not common.enabled():
        return None
    entry = common.load().get("session_targets", {}).get(session_id)
    if not entry:
        return None
    host = common.load()["hosts"].get(entry.get("host") or "")
    if not host:
        return None                                   # the machine was removed from the registry
    return {"name": host["name"], "os": host.get("os") or "linux", "shell": host.get("shell") or "bash",
            "folder": entry.get("folder") or ""}


def describe(session_id: Optional[str]) -> dict:
    """What the picker and the assistant are told about this chat's machine."""
    target = remote(session_id)
    if target is None:
        return {"host": "local", "label": "This server", "os": "linux", "shell": "bash", "folder": "", "remote": False}
    return {"host": target["name"], "label": target["name"], "os": target["os"], "shell": target["shell"],
            "folder": target["folder"], "remote": True}


def set_folder(session_id: str, folder: str) -> dict:
    target = remote(session_id)
    if target is None:
        raise common.WorkbenchError("This chat is working on this server; open a project with manage_projects instead.")
    return bind(session_id, target["name"], folder)


def machines() -> list:
    """The machines Code mode can work on, this server first."""
    out = []
    for host in hosts.list_hosts():
        builtin = bool(host.get("builtin"))
        out.append({"name": "local" if builtin else host["name"],
                    "label": "This server" if builtin else host["name"],
                    "os": host.get("os") or "linux", "shell": host.get("shell") or "bash",
                    "address": "" if builtin else str(host.get("address") or ""), "builtin": builtin})
    return out


def directive(info: dict) -> str:
    """The standing instruction a Code-mode turn starts with."""
    habits = ("- Look before you change: read the files involved, check `git status`, find how the project builds and is tested.\n"
              "- Make the change with `edit_file` / `write_file`, run it, and fix what fails. Do not stop at a plan, and do not "
              "paste code for the user to apply themselves.\n")
    closing = ("- Finish by saying what changed, what you ran to check it, and anything left undone.\n"
               "- Before anything destructive or irreversible (deleting data, force-pushing, reinstalling), say what you are "
               "about to do and get a yes first.")
    note = f"\n({info['note']})" if info.get("note") else ""
    if not info.get("remote"):
        return ("## Code mode\n"
                "You are working as a coding agent on this server (Linux). The user chose Code mode to get software work "
                "done, not to talk about it.\n" + habits +
                "- Projects live in ~/apps/<name>. Begin with `manage_projects` (open, create or clone) so the file tools and "
                "the shell work inside the project, and commit with git when a step works. `manage_apps` keeps an app "
                "running and gives it an address.\n" + closing + note)
    name = info["host"]
    windows = info.get("os") == "windows" or info.get("shell") in ("powershell", "cmd")
    system = "Windows" if windows else ("macOS" if info.get("os") == "macos" else "Linux")
    shell = ("- What you put in the `bash` tool is run there by Windows PowerShell 5.1. Write PowerShell, not bash: no `&&` "
             "(use `;` and check `$LASTEXITCODE`), paths like `C:\\Users\\…`, `Get-ChildItem`, `Get-Content`, `Select-String`.\n"
             if windows else "- The shell there is bash.\n")
    folder = info.get("folder") or ""
    where = (f"- Working folder there: `{folder}`. Relative paths and the shell start in it.\n" if folder else
             "- No working folder is set yet: find or create the project folder there, then fix it with "
             "`manage_hosts` `{\"action\": \"workdir\", \"path\": \"…\"}` so relative paths and the shell start in it.\n")
    return (f"## Code mode — on {name} ({system})\n"
            f"You are working as a coding agent on the user's other computer \"{name}\", not on this server.\n"
            f"- Your ordinary tools are already connected to {name}: the shell tool (`bash`) runs commands there, and "
            f"`read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep` read and write files there. Paths are paths on "
            f"that machine. Use these, exactly as you would locally.\n"
            f"- Do NOT use `manage_hosts` run for {name} (the general rule about other computers does not apply here: you "
            f"are already on it). `manage_hosts` is only for `workdir` and `copy` below.\n" + shell + where +
            f"- `python` and background jobs do not run there; run programs through the shell. To move files between this "
            f"server and {name}, use `manage_hosts` copy.\n" + habits + closing + note)


def for_turn(session_id: str, host_name: str) -> dict:
    """Point this chat at ``host_name`` for a Code-mode turn and return what
    the turn is told. A machine that is not registered falls back to this
    server, and says so."""
    note = ""
    try:
        info = bind(session_id, host_name or "local")
    except common.WorkbenchError as exc:
        clear(session_id)
        info = describe(session_id)
        note = f"The machine “{host_name}” could not be used ({exc}), so this turn works on this server."
    info["note"] = note
    return info
