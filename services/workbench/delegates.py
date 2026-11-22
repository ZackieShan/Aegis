"""Hand a task to another coding agent installed on this server.

Aegis's own agent runs on the local model. These are the other agents on the
box — Claude Code, Kimi Code, Codex, OpenCode (their own cloud models and
logins) and Aider (the local model again, but a purpose-built code editor).
Each has a non-interactive mode; this module knows the flags, runs the agent
in a project folder as a **background job**, and lets the result come back to
the chat that asked for it (Aegis re-invokes its agent when a background job
finishes).

Autonomy is explicit, because a non-interactive agent cannot ask anyone:

``edits``  (default) the agent may change files in the project; commands that
           would need approval are refused by the agent itself
``full``   the agent's "never ask" mode — it runs whatever it decides to
"""

from __future__ import annotations

import os
import shlex
from typing import Dict, List, Optional

from . import common
from .common import WorkbenchError

AUTONOMY = ("edits", "full")
MAX_RUNTIME_S = 2 * 3600

AGENTS: Dict[str, Dict[str, str]] = {
    "claude": {"label": "Claude Code", "bin": "claude"},
    "kimi": {"label": "Kimi Code", "bin": "kimi"},
    "codex": {"label": "Codex", "bin": "codex"},
    "opencode": {"label": "OpenCode", "bin": "opencode"},
    "aider": {"label": "Aider (local model)", "bin": ""},
}
_ALIASES = {"claude-code": "claude", "claudecode": "claude", "cc": "claude", "kimi-code": "kimi",
            "open-code": "opencode", "openai": "codex"}


def normalize(agent: str) -> str:
    agent = (agent or "").strip().lower().replace(" ", "-")
    agent = _ALIASES.get(agent, agent)
    if agent not in AGENTS:
        raise WorkbenchError(f"Unknown agent '{agent}'. Available: {', '.join(AGENTS)}.")
    return agent


def available() -> List[dict]:
    out = []
    for name, spec in AGENTS.items():
        if name == "aider":
            try:
                from src import coding_agent
                path = coding_agent._aider_python()
                installed = bool(path and os.path.exists(path))
            except Exception:
                path, installed = "", False
        else:
            path = common.which(spec["bin"]) or ""
            installed = bool(path)
        out.append({"agent": name, "label": spec["label"], "installed": installed})
    return out


def build_argv(agent: str, task: str, autonomy: str = "edits", resume: bool = False) -> List[str]:
    """The command line for one non-interactive run (not for aider)."""
    agent = normalize(agent)
    full = autonomy == "full"
    if agent == "claude":
        argv = ["claude", "-p", task, "--output-format", "text"]
        argv += ["--dangerously-skip-permissions"] if full else ["--permission-mode", "acceptEdits"]
        if resume:
            argv.append("--continue")
        return argv
    if agent == "kimi":
        # Kimi's prompt mode refuses --yolo / --auto ("Cannot combine --prompt
        # with …"): it applies its own non-interactive policy, so autonomy
        # cannot be chosen here.
        argv = ["kimi", "-p", task, "--output-format", "text"]
        if resume:
            argv.append("--continue")
        return argv
    if agent == "codex":
        argv = ["codex", "exec", "--skip-git-repo-check", "--color", "never"]
        argv += ["--dangerously-bypass-approvals-and-sandbox"] if full else ["--sandbox", "workspace-write"]
        return argv + [task]
    if agent == "opencode":
        argv = ["opencode", "run"]
        if full:
            argv.append("--auto")
        if resume:
            argv.append("--continue")
        return argv + [task]
    raise WorkbenchError("aider is started through its own launcher.")


def _job_script(agent: str, argv: List[str], cwd: str, extra_env: Optional[Dict[str, str]] = None) -> str:
    env = common.user_env(extra_env)
    exported = ["HOME", "PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", *(extra_env or {})]
    lines = [f"# hand-off to {AGENTS[agent]['label']}"]
    lines += [f"export {key}={shlex.quote(env[key])}" for key in dict.fromkeys(exported) if key in env]
    lines.append(f"cd {shlex.quote(cwd)} || exit 1")
    lines.append(shlex.join(argv) + " < /dev/null")
    return "\n".join(lines)


def start(
    agent: str,
    task: str,
    cwd: str,
    session_id: str,
    autonomy: str = "",
    resume: bool = False,
    owner: str = "",
) -> dict:
    """Start an agent on a task in ``cwd`` as a background job of this chat."""
    agent = normalize(agent)
    task = (task or "").strip()
    if not task:
        raise WorkbenchError("Give the task to hand over.")
    if not cwd or not os.path.isdir(cwd):
        raise WorkbenchError("Open a project first (manage_projects open …) so the agent has a folder to work in.")
    if not session_id:
        raise WorkbenchError("Hand-offs run as background jobs of a chat; there is no chat here.")
    autonomy = (autonomy or str(common.settings().get("delegate_autonomy") or "edits")).strip().lower()
    if autonomy not in AUTONOMY:
        raise WorkbenchError("autonomy is 'edits' or 'full'.")

    extra_env: Dict[str, str] = {}
    if agent == "aider":
        from src import coding_agent
        try:
            argv, cwd, aider_env, _endpoint, _use_git = coding_agent._prepare(cwd, task, None, None, owner or "", None)
        except ValueError as exc:
            raise WorkbenchError(f"aider: {exc}") from exc
        for key in ("OPENAI_API_BASE", "OPENAI_API_KEY"):
            if aider_env.get(key):
                extra_env[key] = aider_env[key]
        argv = [str(part) for part in argv]
    else:
        if not common.which(AGENTS[agent]["bin"]):
            raise WorkbenchError(f"{AGENTS[agent]['label']} is not installed on this server.")
        argv = build_argv(agent, task, autonomy, resume)

    from src import bg_jobs
    record = bg_jobs.launch(_job_script(agent, argv, cwd, extra_env), session_id=session_id, cwd=cwd,
                            max_runtime_s=MAX_RUNTIME_S)
    job_id = record.get("id", "")
    entry = {"job": job_id, "agent": agent, "cwd": cwd, "task": task[:300], "autonomy": autonomy,
             "session": session_id, "started": common.now()}

    def mutate(data: dict):
        data["delegations"][job_id] = entry
        # Keep the registry small: the job records themselves live in bg_jobs.
        for stale in sorted(data["delegations"], key=lambda k: data["delegations"][k].get("started", 0))[:-40]:
            del data["delegations"][stale]

    common.update(mutate)
    common.audit("delegate", f"{agent} [{autonomy}] in {cwd}: {task[:200]}")
    if agent in ("kimi", "aider"):
        autonomy = "the agent's own default"            # neither takes an autonomy switch here
    return {
        "job": job_id, "agent": agent, "label": AGENTS[agent]["label"], "folder": cwd, "autonomy": autonomy,
        "status": "running",
        "note": "Runs in the background. You are called again with its output when it finishes — do not poll.",
    }


def _job(job_id: str) -> dict:
    from src import bg_jobs
    bg_jobs.refresh()
    record = bg_jobs.get((job_id or "").strip())
    if not record:
        raise WorkbenchError(f"No such hand-off job: {job_id!r} (finished jobs are kept for an hour).")
    return record


def status(job_id: str) -> dict:
    from src import bg_jobs
    record = _job(job_id)
    entry = common.load()["delegations"].get(record.get("id", ""), {})
    out = {"job": record.get("id"), "agent": entry.get("agent", ""), "status": record.get("status", "unknown"),
           "folder": entry.get("cwd") or record.get("cwd", "")}
    if record.get("status") != "running":
        out["result"] = common.clip(bg_jobs.result_text(record), 9000)
    else:
        out["output_so_far"] = common.clip(bg_jobs._read_output(record), 4000)
    return out


def stop(job_id: str) -> dict:
    from src import bg_jobs
    record = _job(job_id)
    bg_jobs.kill(record["id"])
    return {"job": record["id"], "status": "killed"}


def recent(session_id: str = "") -> List[dict]:
    from src import bg_jobs
    jobs = bg_jobs.refresh()
    out = []
    for job_id, entry in sorted(common.load()["delegations"].items(), key=lambda kv: -kv[1].get("started", 0)):
        if session_id and entry.get("session") != session_id:
            continue
        record = jobs.get(job_id) or {}
        out.append({"job": job_id, "agent": entry.get("agent"), "task": entry.get("task", "")[:120],
                    "folder": entry.get("cwd"), "status": record.get("status", "finished (record expired)")})
    return out[:15]
