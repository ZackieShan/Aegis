"""Workbench endpoints — what the /apps, /projects, /hosts and /run chat
commands call. Administrator only: these start services and run commands on
other machines. The agent reaches the same functions through its tools
(``src/agent_tools/workbench_tools.py``); both go through ``services/workbench``.

    GET  /api/workbench/overview?session_id=…   projects, apps, hosts, agents
    POST /api/workbench/project                  open / close / create / clone for a chat
    POST /api/workbench/app                      start / stop / restart / remove / expose / unexpose
    GET  /api/workbench/app/{name}/logs
    POST /api/workbench/run                      one command on a host
"""

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)


async def _body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON object required")
    return data


def setup_workbench_routes() -> APIRouter:
    router = APIRouter(tags=["workbench"])

    def _guard(request: Request) -> None:
        require_admin(request)
        from services import workbench
        if not workbench.enabled():
            raise HTTPException(503, "The workbench is switched off (setting workbench_enabled).")

    async def _call(work):
        from services.workbench import WorkbenchError
        try:
            return await asyncio.to_thread(work)
        except WorkbenchError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/api/workbench/overview")
    async def overview(request: Request, session_id: str = "") -> Dict[str, Any]:
        _guard(request)
        from services.workbench import apps, delegates, hosts, projects

        def work():
            return {
                "projects_folder": str(projects.projects_root()),
                "open_project": projects.bound_workspace(session_id),
                "projects": projects.list_projects(),
                "apps": apps.list_apps(),
                "tailnet_host": apps.tailnet_host(),
                "hosts": hosts.list_hosts(),
                "peers": hosts.tailnet_peers(),
                "agents": delegates.available(),
            }

        return await _call(work)

    @router.post("/api/workbench/project")
    async def project(request: Request) -> Dict[str, Any]:
        _guard(request)
        body = await _body(request)
        from services.workbench import projects

        action = str(body.get("action") or "open").lower()
        session_id = str(body.get("session_id") or "")
        name = str(body.get("name") or "").strip()

        def work():
            if action == "open":
                return projects.open_project(session_id, name)
            if action == "close":
                return {"closed": projects.close_project(session_id)}
            if action == "create":
                info = projects.create(name, str(body.get("description") or ""))
                if session_id:
                    projects.open_project(session_id, info["path"])
                return info
            if action == "clone":
                info = projects.clone(str(body.get("url") or ""), name)
                if session_id:
                    projects.open_project(session_id, info["path"])
                return info
            if action == "status":
                return projects.status(name or projects.bound_workspace(session_id) or "")
            raise projects.WorkbenchError("action is one of: open, close, create, clone, status")

        return await _call(work)

    @router.post("/api/workbench/app")
    async def app_control(request: Request) -> Dict[str, Any]:
        _guard(request)
        body = await _body(request)
        from services.workbench import apps

        action = str(body.get("action") or "").lower()
        name = str(body.get("name") or "").strip()

        def work():
            if action in ("start", "stop", "restart"):
                return apps.control(name, action)
            if action in ("expose", "unexpose"):
                return apps.set_exposed(name, action == "expose")
            if action == "remove":
                return apps.remove(name)
            if action == "status":
                return apps.status(name)
            raise apps.WorkbenchError("action is one of: start, stop, restart, expose, unexpose, remove, status")

        return await _call(work)

    @router.get("/api/workbench/app/{name}/logs")
    async def app_logs(request: Request, name: str, lines: int = 80) -> Dict[str, Any]:
        _guard(request)
        from services.workbench import apps

        return {"name": name, "log": await _call(lambda: apps.logs(name, lines))}

    @router.post("/api/workbench/run")
    async def run_on_host(request: Request) -> Dict[str, Any]:
        _guard(request)
        body = await _body(request)
        from services.workbench import hosts

        host = str(body.get("host") or "").strip()
        command = str(body.get("command") or "")
        timeout = float(body.get("timeout") or hosts.DEFAULT_TIMEOUT)
        return await _call(lambda: hosts.run(host, command, shell=str(body.get("shell") or ""), timeout=timeout,
                                             cwd=str(body.get("cwd") or "")))

    return router
