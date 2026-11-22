"""Workbench tools: build, host and operate things from a chat.

    manage_projects   create / clone / open a code project (binds it to this chat)
    manage_apps       keep a project's app running as a service with a tailnet URL
    manage_hosts      run commands and copy files on other machines over SSH
    delegate_agent    hand a task to another coding agent installed on this server

All four are administrator-only (see ``tool_security``): between them they can
start services, run commands on other computers and let another agent edit
code. The work is in ``services/workbench``; this module parses the model's
JSON, runs the blocking work off the event loop and words the results.
"""

import asyncio
import json
from typing import Any, Dict

from src.constants import MAX_OUTPUT_CHARS

WORKBENCH_TOOLS = ("manage_projects", "manage_apps", "manage_hosts", "delegate_agent")


def _parse(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            pass
    # "list" / "status myapp" — a bare action word with an optional name.
    words = raw.split(None, 1)
    return {"action": words[0].lower(), "name": words[1].strip() if len(words) > 1 else ""}


def _ok(payload: Any, lead: str = "") -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=1, ensure_ascii=False, default=str)
    if lead:
        text = f"{lead}\n{text}" if text else lead
    return {"output": text[:MAX_OUTPUT_CHARS], "exit_code": 0}


def _fail(message: str) -> dict:
    return {"error": message, "exit_code": 1}


async def _guarded(tool: str, work):
    from services.workbench import common

    if not common.enabled():
        return _fail("The workbench is switched off (setting workbench_enabled).")
    try:
        return await asyncio.to_thread(work)
    except common.WorkbenchError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(f"{tool} failed: {type(exc).__name__}: {exc}")


def _truthy(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


# A run without a chat (a skill audit, a scheduled task) has nothing to keep a
# project open in. Saying "the shell starts there" anyway sent a whole
# scaffold-and-commit into the wrong folder.
_NOT_OPENED = ("This run has no chat to keep it open in, so the shell does NOT start there: begin every shell "
               "command with `cd {path} &&` and give the file tools full paths under it.")


class ManageProjectsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse(content)
        session_id = (ctx or {}).get("session_id") or ""

        def work():
            from services.workbench import projects

            action = str(args.get("action") or "list").lower()
            name = str(args.get("name") or args.get("project") or "").strip()
            if action == "list":
                bound = projects.bound_workspace(session_id)
                return _ok({"projects_folder": str(projects.projects_root()), "open_in_this_chat": bound,
                            "projects": projects.list_projects()})
            if action == "create":
                info = projects.create(name, str(args.get("description") or ""))
                if session_id:
                    projects.open_project(session_id, info["path"])
                    return _ok(info, f"Created {info['path']} (git repository, first commit made). This chat now works in "
                                     "that folder: file tools are confined to it and the shell starts there.")
                return _ok(info, f"Created {info['path']} (git repository, first commit made). " + _NOT_OPENED.format(path=info["path"]))
            if action == "clone":
                info = projects.clone(str(args.get("url") or args.get("repo") or ""), name)
                if session_id:
                    projects.open_project(session_id, info["path"])
                    return _ok(info, f"Cloned into {info['path']}. This chat now works in that folder.")
                return _ok(info, f"Cloned into {info['path']}. " + _NOT_OPENED.format(path=info["path"]))
            if action == "open":
                info = projects.open_project(session_id, name or str(args.get("path") or ""))
                return _ok(info, f"This chat now works in {info['path']}: file tools are confined to it and the shell "
                                 "starts there. It stays that way until another project is opened or this one is closed.")
            if action == "close":
                was = projects.close_project(session_id)
                return _ok("This chat is no longer tied to a project." if was else "No project was open in this chat.")
            if action == "status":
                target = name or projects.bound_workspace(session_id) or ""
                return _ok(projects.status(target))
            raise projects.WorkbenchError("action is one of: list, create, clone, open, close, status")

        return await _guarded("manage_projects", work)


class ManageAppsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse(content)
        session_id = (ctx or {}).get("session_id") or ""

        def work():
            from services.workbench import apps, projects

            action = str(args.get("action") or "list").lower()
            name = str(args.get("name") or args.get("app") or "").strip()
            if action == "list":
                return _ok({"apps": apps.list_apps(), "tailnet_host": apps.tailnet_host()})
            if action == "deploy":
                folder = str(args.get("project") or args.get("dir") or "").strip() \
                    or projects.bound_workspace(session_id) or name
                port = args.get("port")
                env = args.get("env") if isinstance(args.get("env"), dict) else None
                result = apps.deploy(
                    name or (folder.rstrip("/").rsplit("/", 1)[-1] if folder else ""),
                    str(args.get("command") or ""),
                    directory=folder,
                    port=int(port) if str(port or "").isdigit() else None,
                    env=env,
                    expose=_truthy(args.get("expose"), True),
                    description=str(args.get("description") or ""),
                    web=_truthy(args.get("web"), True),
                )
                lead = f"'{result['name']}' is {result.get('state', 'unknown')}."
                if result.get("url"):
                    lead += f" Open it at {result['url']} (any device on the tailnet)."
                elif result.get("local_url"):
                    lead += f" It listens on {result['local_url']} (this server only)."
                lead += " It restarts by itself after a crash or a reboot."
                return _ok(result, lead)
            if action == "status":
                return _ok(apps.status(name))
            if action == "logs":
                return _ok(apps.logs(name, int(args.get("lines") or 60)) or "(no log lines yet)")
            if action in ("start", "stop", "restart"):
                return _ok(apps.control(name, action))
            if action in ("expose", "unexpose"):
                return _ok(apps.set_exposed(name, action == "expose"))
            if action == "remove":
                return _ok(apps.remove(name), "Stopped and unregistered. The project folder was not touched.")
            raise apps.WorkbenchError(
                "action is one of: list, deploy, status, logs, start, stop, restart, expose, unexpose, remove")

        return await _guarded("manage_apps", work)


class ManageHostsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse(content)
        progress_cb = (ctx or {}).get("progress_cb")
        session_id = (ctx or {}).get("session_id") or ""

        action = str(args.get("action") or ("run" if args.get("command") else "list")).lower()
        host = str(args.get("host") or args.get("name") or "").strip()
        if action == "run" and progress_cb:
            first = str(args.get("command") or "").strip().splitlines()[:1]
            await progress_cb({"elapsed_s": 0, "tail": f"{host or 'host'}: {(first[0] if first else '')[:140]}"})

        def work():
            from services.workbench import hosts

            if action == "list":
                return _ok({"hosts": hosts.list_hosts()})
            if action == "peers":
                return _ok({"tailnet": hosts.tailnet_peers()})
            if action == "run":
                command = args.get("command") or args.get("script") or args.get("cmd") or args.get("commands") or ""
                if isinstance(command, list):
                    command = "\n".join(str(line) for line in command)
                if not str(command).strip():
                    # Said with an example: a bare "nothing to run" got the same
                    # empty call sent seven times in a row.
                    from services.workbench import targets
                    here = targets.remote(session_id)
                    hint = (f' In Code mode your shell tool (`bash`) already runs on {here["name"]} — use that instead.'
                            if here and here["name"] == host.lower() else "")
                    raise hosts.WorkbenchError(
                        'run needs "command": the PowerShell or bash text to run on that machine, e.g. '
                        f'{{"action": "run", "host": "{host or "desktop"}", "command": "Get-ChildItem"}}.' + hint)
                args["command"] = str(command)
                result = hosts.run(host, str(args.get("command") or ""), shell=str(args.get("shell") or ""),
                                   timeout=float(args.get("timeout") or hosts.DEFAULT_TIMEOUT),
                                   cwd=str(args.get("cwd") or ""))
                payload = _ok(result)
                payload["exit_code"] = 0 if result["exit_code"] == 0 else 1
                if result["exit_code"] != 0:
                    payload["error"] = payload.pop("output")
                return payload
            if action == "add":
                return _ok(hosts.add(host, str(args.get("address") or ""), str(args.get("user") or ""),
                                     port=int(args.get("port") or 22), os_kind=str(args.get("os") or ""),
                                     shell=str(args.get("shell") or ""), description=str(args.get("description") or "")),
                           "Host registered and reachable.")
            if action == "remove":
                return _ok(hosts.remove(host))
            if action == "test":
                return _ok(hosts.test(host))
            if action == "copy":
                return _ok(hosts.copy(host, str(args.get("source") or ""), str(args.get("destination") or ""),
                                      direction=str(args.get("direction") or "put")))
            if action == "workdir":
                # Code mode on another machine: the folder its shell starts in
                # and that relative paths are read against, for this chat.
                from services.workbench import remote_tools, targets
                target = targets.remote(session_id)
                if target is None:
                    raise hosts.WorkbenchError("This chat is not in Code mode on another machine. On this server, "
                                               "open a project with manage_projects instead.")
                wanted = str(args.get("path") or args.get("cwd") or "").strip()
                if not wanted:
                    return _ok(targets.describe(session_id))
                try:
                    folder = remote_tools.folder_exists(target, wanted)
                except remote_tools.RemoteError as exc:
                    raise hosts.WorkbenchError(f"{exc} on {target['name']}. Create the folder first with the shell tool.")
                return _ok(targets.set_folder(session_id, folder), f"Working in {folder} on {target['name']} from now on.")
            raise hosts.WorkbenchError("action is one of: list, peers, run, add, remove, test, copy, workdir")

        return await _guarded("manage_hosts", work)


class DelegateAgentTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse(content)
        session_id = (ctx or {}).get("session_id") or ""
        owner = (ctx or {}).get("owner") or ""

        def work():
            from services.workbench import delegates, projects

            action = str(args.get("action") or ("run" if args.get("task") else "agents")).lower()
            if action == "agents":
                return _ok({"agents": delegates.available()})
            if action == "list":
                return _ok({"hand_offs": delegates.recent(session_id)})
            if action == "run":
                folder = str(args.get("project") or args.get("dir") or "").strip()
                cwd = str(projects.resolve(folder)) if folder else (projects.bound_workspace(session_id) or "")
                result = delegates.start(
                    str(args.get("agent") or ""), str(args.get("task") or ""), cwd, session_id,
                    autonomy=str(args.get("autonomy") or ""), resume=_truthy(args.get("continue"), False), owner=owner)
                return _ok(result, f"{result['label']} is working in {result['folder']} (job {result['job']}).")
            job = str(args.get("job") or args.get("name") or "")
            if action == "status":
                return _ok(delegates.status(job))
            if action == "stop":
                return _ok(delegates.stop(job))
            raise delegates.WorkbenchError("action is one of: agents, run, status, stop, list")

        return await _guarded("delegate_agent", work)
