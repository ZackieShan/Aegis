import asyncio
import sys
import time
import collections
from typing import Optional, Callable, Awaitable, Tuple, Dict
from src.constants import MAX_OUTPUT_CHARS

DEFAULT_BASH_TIMEOUT = 60 * 60     # 1 hour
DEFAULT_PYTHON_TIMEOUT = 60 * 60

PROGRESS_INTERVAL_S = 2.0
PROGRESS_TAIL_LINES = 12

async def _run_subprocess_streaming(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
) -> Tuple[str, str, Optional[int], bool]:
    started = time.time()
    stdout_full: list[str] = []
    stderr_full: list[str] = []
    tail = collections.deque(maxlen=PROGRESS_TAIL_LINES)

    async def _reader(stream, full_buf, label: str):
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            full_buf.append(decoded)
            if label == "err":
                tail.append(f"! {decoded}")
            else:
                tail.append(decoded)

    async def _progress_emitter():
        await asyncio.sleep(PROGRESS_INTERVAL_S)
        while True:
            if progress_cb:
                try:
                    await progress_cb({
                        "elapsed_s": round(time.time() - started, 1),
                        "tail": "\n".join(list(tail)),
                    })
                except Exception:
                    pass
            await asyncio.sleep(PROGRESS_INTERVAL_S)

    rd_out = asyncio.create_task(_reader(proc.stdout, stdout_full, "out"))
    rd_err = asyncio.create_task(_reader(proc.stderr, stderr_full, "err"))
    prog_task = asyncio.create_task(_progress_emitter()) if progress_cb else None

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
        for t in (rd_out, rd_err):
            t.cancel()
        if prog_task is not None:
            prog_task.cancel()
        raise
    finally:
        if prog_task is not None and not prog_task.done():
            prog_task.cancel()
            try:
                await prog_task
            except (asyncio.CancelledError, Exception):
                pass
        for t in (rd_out, rd_err):
            try:
                await asyncio.wait_for(t, timeout=1)
            except Exception:
                pass

    return (
        "\n".join(stdout_full),
        "\n".join(stderr_full),
        proc.returncode,
        timed_out,
    )

class BashTool:
    @staticmethod
    async def _spawn(content: str, **kwargs) -> asyncio.subprocess.Process:
        """Spawn the agent's shell command under a REAL shell.

        On Windows create_subprocess_shell means cmd.exe — but the agent
        prompt teaches bash syntax (heredocs, `if [ ... ]`, $VARs), and the
        background (#!bg, src/bg_jobs.py) and /api/shell/exec paths already
        run Git Bash. Route the foreground tool through Git Bash too so the
        same command doesn't succeed in background and fail in foreground.
        PowerShell/cmd invocations pass through untouched (bash -c would
        mangle $env:VAR and cmd flags). Mirrors routes/shell_routes.py.
        """
        from core.platform_compat import IS_WINDOWS, find_bash
        if IS_WINDOWS:
            cmd_trim = content.strip()
            if not (cmd_trim.startswith("powershell") or cmd_trim.startswith("cmd ")):
                bash = find_bash()
                if bash:
                    return await asyncio.create_subprocess_exec(bash, "-c", content, **kwargs)
        return await asyncio.create_subprocess_shell(content, **kwargs)

    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd, _truncate
        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")
        proc = await self._spawn(
            content,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subproc_env,
            cwd=agent_cwd(),
        )
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_BASH_TIMEOUT,
            progress_cb=progress_cb,
        )
        if timed_out:
            return {"error": f"bash: timed out after {DEFAULT_BASH_TIMEOUT}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _truncate(output, MAX_OUTPUT_CHARS)
        return {"output": output or "(no output)", "exit_code": rc or 0}

class PythonTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd, _truncate
        from src.runtime_paths import get_python_exe
        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")
        python = get_python_exe()
        if not python:
            return {"error": "python tool unavailable: no Python interpreter found "
                             "(portable build without a system python)", "exit_code": 1}
        proc = await asyncio.create_subprocess_exec(
            python, "-I", "-c", content,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subproc_env,
            cwd=agent_cwd(),
        )
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_PYTHON_TIMEOUT,
            progress_cb=progress_cb,
        )
        if timed_out:
            return {"error": f"python: timed out after {DEFAULT_PYTHON_TIMEOUT}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _truncate(output, MAX_OUTPUT_CHARS)
        return {"output": output or "(no output)", "exit_code": rc or 0}
