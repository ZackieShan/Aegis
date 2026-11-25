"""Projects: folders of code the agent builds in, each a git repository.

A project lives under the projects root (``projects_dir`` setting, default
``~/apps`` — the same place hosted apps live on this kind of box). *Opening*
a project binds it to a chat: from then on that chat's file tools are confined
to the project and its shell starts there, on every later message, after an
Aegis restart, and in the follow-up turn of a background job. That binding —
not a browser setting — is what makes "keep working on the fitness app" work
from a phone.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from . import common
from .common import WorkbenchError

# This app and its engines sit in the same folder. They can be opened to work
# on, but never created over, deployed as an app, or removed by a tool.
PROTECTED = frozenset({"aegis", "engine"})

_GITIGNORE = """\
# secrets and local state
.env
.env.*
!.env.example
*.sqlite3
*.db

# python
__pycache__/
*.py[cod]
.venv/
venv/

# node
node_modules/
dist/
build/
.next/

# editors / OS
.DS_Store
.idea/
.vscode/
"""

_URL_OK = re.compile(
    r"^(https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"            # https
    r"|ssh://[A-Za-z0-9._~:/@%-]+"                                # ssh://user@host/path
    r"|[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[A-Za-z0-9._~/-]+)$"       # git@host:owner/repo
)
_SHORTHAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")


def projects_root() -> Path:
    raw = str(common.settings().get("projects_dir") or "").strip() or os.environ.get("AEGIS_PROJECTS_DIR", "").strip()
    return Path(raw).expanduser() if raw else common.real_home() / "apps"


def _git(args: List[str], cwd: Path, timeout: float = 30):
    return common.run(["git", *args], cwd=str(cwd), timeout=timeout)


def describe(path: Path) -> dict:
    """One project as the tools report it."""
    info: Dict[str, object] = {"name": path.name, "path": str(path), "git": (path / ".git").exists()}
    if path.name in PROTECTED:
        info["protected"] = True
    if info["git"]:
        rc, out, _ = _git(["status", "--porcelain=v1", "--branch"], path, timeout=15)
        if rc == 0:
            lines = out.splitlines()
            head = lines[0][3:] if lines and lines[0].startswith("## ") else ""
            info["branch"] = head.split("...")[0].replace("No commits yet on ", "")
            match = re.search(r"\[(.+)\]", head)
            if match:
                info["sync"] = match.group(1)                      # "ahead 2", "behind 1"
            info["uncommitted"] = max(0, len(lines) - 1)
        rc, out, _ = _git(["log", "-1", "--format=%h %s (%cr)"], path, timeout=15)
        if rc == 0 and out.strip():
            info["last_commit"] = out.strip()[:140]
        rc, out, _ = _git(["remote", "get-url", "origin"], path, timeout=10)
        if rc == 0 and out.strip():
            info["remote"] = re.sub(r"//[^/@]+@", "//", out.strip())      # never echo embedded credentials
    return info


def list_projects() -> List[dict]:
    root = projects_root()
    if not root.is_dir():
        return []
    out = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir() and not entry.name.startswith((".", "_")):
            out.append(describe(entry))
    return out


def resolve(name_or_path: str) -> Path:
    """A project name (under the projects root) or an absolute folder path."""
    raw = (name_or_path or "").strip()
    if not raw:
        raise WorkbenchError("Which project? Give its name or an absolute path.")
    if raw.startswith(("/", "~")):
        path = Path(raw).expanduser()
    else:
        path = projects_root() / raw
    path = Path(os.path.realpath(path))
    if not path.is_dir():
        known = ", ".join(p["name"] for p in list_projects()) or "none yet"
        raise WorkbenchError(f"No project folder at {path}. Existing projects: {known}.")
    return path


def create(name: str, description: str = "") -> dict:
    """New folder + git repository with a README, a .gitignore and a first commit."""
    name = common.valid_name(name)
    if name in PROTECTED:
        raise WorkbenchError(f"'{name}' is reserved.")
    root = projects_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    if path.exists():
        raise WorkbenchError(f"{path} already exists — open it instead.")
    path.mkdir()
    title = name.replace("-", " ").replace("_", " ").title()
    (path / "README.md").write_text(f"# {title}\n\n{description.strip() or 'Describe the app here.'}\n", encoding="utf-8")
    (path / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    rc, _, err = _git(["init", "-b", "main"], path)
    if rc != 0:
        raise WorkbenchError(f"git init failed: {err.strip()[:300]}")
    _git(["add", "-A"], path)
    rc, _, err = _git(["commit", "-m", "Initial commit"], path)
    info = describe(path)
    if rc != 0:
        info["warning"] = f"created, but the first commit failed: {err.strip()[:200]}"
    return info


def clone(url: str, name: str = "") -> dict:
    """Clone a repository into the projects root. ``owner/repo`` means GitHub."""
    url = (url or "").strip()
    if _SHORTHAND.match(url):
        url = f"https://github.com/{url}.git"
    # Only plain transports: git's "ext::"/"fd::" remote helpers run commands.
    if not _URL_OK.match(url) or url.startswith("-"):
        raise WorkbenchError("Give an https:// or ssh git URL, git@host:owner/repo, or owner/repo for GitHub.")
    if not name:
        name = re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]).lower()
        name = re.sub(r"[^a-z0-9._-]+", "-", name).strip("-.") or "project"
    name = common.valid_name(name)
    if name in PROTECTED:
        raise WorkbenchError(f"'{name}' is reserved — pass another name.")
    root = projects_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    if path.exists():
        raise WorkbenchError(f"{path} already exists — open it, or clone under another name.")
    rc, _, err = common.run(["git", "clone", "--", url, str(path)], cwd=str(root), timeout=900)
    if rc != 0:
        hint = ""
        if "Authentication failed" in err or "could not read Username" in err or "Permission denied" in err:
            hint = " (private repository? this server is logged in to GitHub through `gh`; use the https URL)"
        raise WorkbenchError(f"git clone failed: {common.clip(err.strip(), 500)}{hint}")
    return describe(path)


# ── binding a project to a chat ────────────────────────────────────────────

def open_project(session_id: str, name_or_path: str) -> dict:
    if not session_id:
        raise WorkbenchError("Opening a project needs a chat to bind it to.")
    path = resolve(name_or_path)
    from src.tool_execution import vet_workspace
    vetted = vet_workspace(str(path))
    if not vetted:
        raise WorkbenchError(f"{path} cannot be used as a workspace.")

    def mutate(data: dict):
        data["session_projects"][session_id] = {"path": vetted, "name": Path(vetted).name, "at": common.now()}

    common.update(mutate)
    info = describe(Path(vetted))
    info["bound_to_chat"] = True
    return info


def close_project(session_id: str) -> bool:
    def mutate(data: dict):
        return data["session_projects"].pop(session_id, None) is not None

    return bool(common.update(mutate))


def bound_workspace(session_id: Optional[str]) -> Optional[str]:
    """The folder bound to this chat, or None. Called on every tool call —
    the store is cached by mtime, so this is a dict lookup."""
    if not session_id or not common.enabled():
        return None
    entry = common.load()["session_projects"].get(session_id)
    if not entry:
        return None
    path = entry.get("path") or ""
    return path if path and os.path.isdir(path) else None


def forget_missing() -> int:
    """Drop bindings whose folder is gone."""
    def mutate(data: dict):
        gone = [sid for sid, entry in data["session_projects"].items() if not os.path.isdir(entry.get("path") or "")]
        for sid in gone:
            del data["session_projects"][sid]
        return len(gone)

    return int(common.update(mutate))


def status(name_or_path: str) -> dict:
    """Git state of a project plus what is deployed from it."""
    path = resolve(name_or_path)
    info = describe(path)
    if info.get("git"):
        rc, out, _ = _git(["status", "--short"], path, timeout=15)
        if rc == 0 and out.strip():
            info["changes"] = common.clip(out.strip(), 1500)
    from . import apps
    deployed = [app for app in apps.list_apps() if os.path.realpath(app.get("dir", "")) == str(path)]
    if deployed:
        info["apps"] = deployed
    return info
