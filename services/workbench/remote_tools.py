"""The coding tools, on another machine.

When a chat's Code mode points at another computer (``targets.remote``), the
assistant keeps using the tools it knows — the shell, ``read_file``,
``write_file``, ``edit_file``, ``ls``, ``glob``, ``grep`` — and this module
carries each call out over SSH on that computer, through the same
``hosts.run`` the workbench already uses (one reused connection, audited).

File contents travel base64-encoded in both directions, so quoting, line
endings and non-ASCII text survive Windows PowerShell and a POSIX shell
alike. A file that uses Windows line endings (or starts with a byte-order
mark) is shown to the assistant with plain ``\\n`` and written back the way
it was found.

Same inputs and result shapes as the local tools in
``src/agent_tools/filesystem_tools.py`` and ``subprocess_tools.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import textwrap
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from . import common, hosts

MAX_READ_BYTES = 2_000_000
MAX_WRITE_BYTES = 1_500_000
MAX_READ_CHARS = 100_000            # what one read hands the assistant, like the local tool
MAX_LISTING = 20_000
MAX_HITS = 200
SHELL_TIMEOUT = 900

ROUTED = ("bash", "read_file", "write_file", "edit_file", "ls", "glob", "grep", "get_workspace")
NOT_THERE = {
    "python": "The python tool runs on this server. On {name}, run Python through the shell tool instead "
              "(write the script with write_file, then run it).",
    "manage_bg_jobs": "Background jobs run on this server only. On {name}, start long-running things with the shell tool.",
}
_SKIP_DIRS = (".git", "node_modules", ".venv", "__pycache__")


class RemoteError(Exception):
    """A remote file operation that did not work, said in the tool's words."""


def _ps(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _sh(text: str) -> str:
    return "'" + str(text).replace("'", "'\\''") + "'"


def _windows(target: dict) -> bool:
    return target.get("os") == "windows" or target.get("shell") in ("powershell", "cmd")


def _where(target: dict) -> str:
    return f"{target['name']}:{target.get('folder') or '~'}"


# ── running a helper script there ──────────────────────────────────────────

def _helper(target: dict, powershell: str, posix: str, audit: str, timeout: float = 120,
            limit: int = MAX_READ_BYTES * 2) -> str:
    """Run one of this module's own scripts on the target and return what it
    printed. The scripts report through a first line starting ``AEGIS:``."""
    folder = target.get("folder") or ""
    if _windows(target):
        prelude = "$ErrorActionPreference = 'Stop'\n"
        if folder:
            prelude += f"Set-Location -LiteralPath {_ps(folder)}\n"
        script = prelude + "try {\n" + powershell + "\n} catch { 'AEGIS:ERROR ' + $_.Exception.Message }\n"
        result = hosts.run(target["name"], script, shell="powershell", timeout=timeout, audit_line=audit, output_limit=limit)
    else:
        prelude = f"cd -- {_sh(folder)} 2>/dev/null || {{ echo 'AEGIS:ERROR the working folder does not exist there'; exit 0; }}\n" \
            if folder else ""
        result = hosts.run(target["name"], prelude + posix, shell="bash", timeout=timeout, audit_line=audit, output_limit=limit)
    output = result.get("output") or ""
    if "AEGIS:" not in output:
        problem = (result.get("stderr") or output or result.get("note") or "no answer").strip()
        raise RemoteError(f"{target['name']} did not carry that out: {problem[:300]}")
    return output[output.index("AEGIS:"):]


def _outcome(output: str, path: str) -> Tuple[str, str]:
    """``(resolved path, rest of the output)`` for an ``AEGIS:OK`` answer; raises otherwise."""
    first, _, rest = output.partition("\n")
    first = first.strip()
    if first.startswith("AEGIS:OK"):
        return first[len("AEGIS:OK"):].strip() or path, rest
    if first.startswith("AEGIS:NOFILE"):
        raise RemoteError(f"{path}: not found")
    if first.startswith("AEGIS:ISDIR"):
        raise RemoteError(f"{path}: is a directory (use ls)")
    if first.startswith("AEGIS:TOOBIG"):
        size = first.split()[-1]
        raise RemoteError(f"{path}: {size} bytes is too large to read whole (limit {MAX_READ_BYTES}); "
                          "look at part of it with the shell tool")
    raise RemoteError(f"{path}: {first[len('AEGIS:ERROR'):].strip() or 'could not be done'}")


_PS_RESOLVE = "$p = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath({path})"


def read_bytes(target: dict, path: str) -> Tuple[str, bytes]:
    powershell = "\n".join([
        _PS_RESOLVE.format(path=_ps(path)),
        "if (Test-Path -LiteralPath $p -PathType Container) { 'AEGIS:ISDIR'; return }",
        "if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { 'AEGIS:NOFILE'; return }",
        "$n = (Get-Item -LiteralPath $p -Force).Length",
        f"if ($n -gt {MAX_READ_BYTES}) {{ \"AEGIS:TOOBIG $n\"; return }}",
        "'AEGIS:OK ' + $p",
        "[Convert]::ToBase64String([IO.File]::ReadAllBytes($p))",
    ])
    posix = "\n".join([
        f"p={_sh(path)}",
        'if [ -d "$p" ]; then echo AEGIS:ISDIR; exit 0; fi',
        'if [ ! -f "$p" ]; then echo AEGIS:NOFILE; exit 0; fi',
        'n=$(wc -c < "$p" 2>/dev/null) || { echo "AEGIS:ERROR permission denied"; exit 0; }',
        f'if [ "$n" -gt {MAX_READ_BYTES} ]; then echo "AEGIS:TOOBIG $n"; exit 0; fi',
        'echo "AEGIS:OK $p"',
        'base64 < "$p" | tr -d \'\\n\'',
    ])
    resolved, body = _outcome(_helper(target, powershell, posix, f"read_file {path}"), path)
    try:
        return resolved, base64.b64decode(re.sub(r"\s+", "", body), validate=False)
    except Exception:
        raise RemoteError(f"{path}: came back garbled")


def write_bytes(target: dict, path: str, data: bytes) -> str:
    if len(data) > MAX_WRITE_BYTES:
        raise RemoteError(f"{path}: {len(data)} bytes is more than one write carries (limit {MAX_WRITE_BYTES}); "
                          "copy large files with manage_hosts copy")
    encoded = base64.b64encode(data).decode("ascii")
    powershell = "\n".join([
        _PS_RESOLVE.format(path=_ps(path)),
        "if (Test-Path -LiteralPath $p -PathType Container) { 'AEGIS:ISDIR'; return }",
        "$d = Split-Path -Parent $p",
        "if ($d -and -not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }",
        f"[IO.File]::WriteAllBytes($p, [Convert]::FromBase64String('{encoded}'))",
        "'AEGIS:OK ' + $p",
    ])
    posix = "\n".join([
        f"p={_sh(path)}",
        'if [ -d "$p" ]; then echo AEGIS:ISDIR; exit 0; fi',
        'mkdir -p -- "$(dirname -- "$p")" 2>/dev/null',
        "if base64 -d </dev/null >/dev/null 2>&1; then decode=-d; else decode=-D; fi",
        'if base64 $decode > "$p" <<\'AEGIS_B64\'',
        "\n".join(textwrap.wrap(encoded, 76)) if encoded else "",
        "AEGIS_B64",
        'then echo "AEGIS:OK $p"; else echo "AEGIS:ERROR could not write (permission denied?)"; fi',
    ])
    resolved, _ = _outcome(_helper(target, powershell, posix, f"write_file {path} ({len(data)} bytes)"), path)
    return resolved


class RemoteText:
    """A text file as the assistant sees it, and how it was stored."""

    def __init__(self, path: str, text: str, crlf: bool, bom: bool):
        self.path, self.text, self.crlf, self.bom = path, text, crlf, bom


def read_text(target: dict, path: str) -> RemoteText:
    resolved, data = read_bytes(target, path)
    if b"\x00" in data[:8000]:
        raise RemoteError(f"{path}: not a text file")
    bom = data.startswith(b"\xef\xbb\xbf")
    text = data[3:].decode("utf-8", errors="replace") if bom else data.decode("utf-8", errors="replace")
    crlf = "\r\n" in text
    return RemoteText(resolved, text.replace("\r\n", "\n") if crlf else text, crlf, bom)


def write_text(target: dict, path: str, text: str, crlf: bool = False, bom: bool = False) -> str:
    if crlf:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    data = text.encode("utf-8")
    return write_bytes(target, path, (b"\xef\xbb\xbf" + data) if bom else data)


# ── the tools ──────────────────────────────────────────────────────────────

def _args(content: str, first_line_key: str = "path") -> Dict[str, Any]:
    stripped = (content or "").strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {first_line_key: stripped.split("\n", 1)[0].strip()}


def _diff(old: str, new: str, path: str) -> Optional[dict]:
    try:
        from src.agent_tools.filesystem_tools import _unified_diff
        return _unified_diff(old, new, path)
    except Exception:
        return None


def _read_file(content: str, target: dict) -> dict:
    args = _args(content)
    path = str(args.get("path") or "").strip()
    if not path:
        return {"error": "read_file: path required", "exit_code": 1}
    try:
        offset, limit = int(args.get("offset") or 0), int(args.get("limit") or 0)
    except (TypeError, ValueError):
        offset, limit = 0, 0
    try:
        found = read_text(target, path)
    except RemoteError as exc:
        return {"error": f"read_file: {exc}", "exit_code": 1}
    text = found.text
    if offset > 0 or limit > 0:
        lines = text.splitlines(keepends=True)
        start = max(offset, 1) - 1
        text = "".join(lines[start: start + limit] if limit > 0 else lines[start:])
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + f"\n... [truncated at {MAX_READ_CHARS} chars]"
    return {"output": text, "exit_code": 0}


def _write_file(content: str, target: dict) -> dict:
    lines = (content or "").split("\n", 1)
    path, body = lines[0].strip(), (lines[1] if len(lines) > 1 else "")
    stripped = (content or "").strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and "path" in parsed:
                path, body = str(parsed.get("path") or "").strip(), str(parsed.get("content") or "")
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    if not path:
        return {"error": "write_file: path required", "exit_code": 1}
    old, crlf, bom = "", False, False
    try:
        before = read_text(target, path)
        old, crlf, bom = before.text, before.crlf, before.bom
    except RemoteError:
        pass                                              # a new file, or one that is not text
    try:
        resolved = write_text(target, path, body, crlf=crlf, bom=bom)
    except RemoteError as exc:
        return {"error": f"write_file: {exc}", "exit_code": 1}
    result = {"output": f"Wrote {len(body.encode('utf-8'))} bytes to {resolved} on {target['name']}", "exit_code": 0}
    diff = _diff(old, body, resolved)
    if diff:
        result["diff"] = diff
    return result


def _edit_file(content: str, target: dict) -> dict:
    args = _args(content)
    path = str(args.get("path") or "").strip()
    old, new = args.get("old_string", ""), args.get("new_string", "")
    replace_all = bool(args.get("replace_all", False))
    if not path:
        return {"error": "edit_file: path required", "exit_code": 1}
    if not isinstance(old, str) or old == "":
        return {"error": "edit_file: old_string required (use write_file to create a file)", "exit_code": 1}
    if not isinstance(new, str):
        new = str(new)
    if old == new:
        return {"error": "edit_file: old_string and new_string are identical", "exit_code": 1}
    try:
        found = read_text(target, path)
    except RemoteError as exc:
        message = str(exc)
        if message.endswith("not found"):
            message += " (use write_file to create it)"
        return {"error": f"edit_file: {message}", "exit_code": 1}
    wanted = old.replace("\r\n", "\n")
    count = found.text.count(wanted)
    if count == 0:
        return {"error": f"edit_file: old_string not found in {found.path}. Read the file and match it exactly.", "exit_code": 1}
    if count > 1 and not replace_all:
        return {"error": f"edit_file: old_string is not unique in {found.path} ({count} matches). "
                         "Add surrounding context or set replace_all=true.", "exit_code": 1}
    replacement = new.replace("\r\n", "\n")
    updated = found.text.replace(wanted, replacement) if replace_all else found.text.replace(wanted, replacement, 1)
    try:
        write_text(target, found.path, updated, crlf=found.crlf, bom=found.bom)
    except RemoteError as exc:
        return {"error": f"edit_file: {exc}", "exit_code": 1}
    done = count if replace_all else 1
    result = {"output": f"Edited {found.path} on {target['name']} ({done} replacement{'s' if done != 1 else ''})", "exit_code": 0}
    diff = _diff(found.text, updated, found.path)
    if diff:
        result["diff"] = diff
    return result


def _ls(content: str, target: dict) -> dict:
    path = str(_args(content).get("path") or "").strip() or "."
    powershell = "\n".join([
        _PS_RESOLVE.format(path=_ps(path)),
        "if (-not (Test-Path -LiteralPath $p)) { 'AEGIS:NOFILE'; return }",
        "'AEGIS:OK ' + $p",
        "Get-ChildItem -LiteralPath $p -Force | Sort-Object @{Expression = { -not $_.PSIsContainer }}, Name | "
        "Select-Object -First 500 | ForEach-Object { if ($_.PSIsContainer) { $_.Name + '/' } "
        "else { '{0}  ({1} bytes)' -f $_.Name, $_.Length } }",
    ])
    posix = "\n".join([
        f"p={_sh(path)}",
        'if [ ! -e "$p" ]; then echo AEGIS:NOFILE; exit 0; fi',
        'echo "AEGIS:OK $(cd -- "$p" 2>/dev/null && pwd || echo "$p")"',
        'ls -1Ap -- "$p" 2>&1 | head -n 500',
    ])
    try:
        resolved, listing = _outcome(_helper(target, powershell, posix, f"ls {path}"), path)
    except RemoteError as exc:
        return {"error": f"ls: {exc}", "exit_code": 1}
    listing = listing.strip()
    return {"output": f"{resolved} on {target['name']}:\n{listing}" if listing else f"{resolved} on {target['name']} is empty",
            "exit_code": 0}


def _files_under(target: dict, path: str) -> Tuple[str, List[str]]:
    skip_ps = "|".join(re.escape(name) for name in _SKIP_DIRS)
    powershell = "\n".join([
        _PS_RESOLVE.format(path=_ps(path)),
        "if (-not (Test-Path -LiteralPath $p -PathType Container)) { 'AEGIS:NOFILE'; return }",
        "'AEGIS:OK ' + $p",
        "Get-ChildItem -LiteralPath $p -Recurse -File -Force -Name -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_ -notmatch '(^|\\\\)({skip_ps})(\\\\|$)' }} | Select-Object -First {MAX_LISTING}",
    ])
    prune = " -o ".join(f"-name {_sh(name)}" for name in _SKIP_DIRS)
    posix = "\n".join([
        f"p={_sh(path)}",
        'if [ ! -d "$p" ]; then echo AEGIS:NOFILE; exit 0; fi',
        'cd -- "$p" || { echo "AEGIS:ERROR cannot enter $p"; exit 0; }',
        'echo "AEGIS:OK $(pwd)"',
        f"find . \\( {prune} \\) -prune -o -type f -print 2>/dev/null | head -n {MAX_LISTING} | sed 's|^\\./||'",
    ])
    resolved, listing = _outcome(_helper(target, powershell, posix, f"list files under {path}", timeout=180), path)
    return resolved, [line.strip().replace("\\", "/") for line in listing.splitlines() if line.strip()]


def _glob(content: str, target: dict) -> dict:
    from src.agent_tools.filesystem_tools import _glob_to_regex

    args = _args(content, first_line_key="pattern")
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return {"error": "glob: pattern is required", "exit_code": 1}
    path = str(args.get("path") or "").strip() or "."
    try:
        resolved, files = _files_under(target, path)
    except RemoteError as exc:
        return {"error": f"glob: {exc}", "exit_code": 1}
    regex = _glob_to_regex(pattern.replace("\\", "/"))
    matched = [name for name in files if regex.fullmatch(name) or regex.fullmatch(name.rsplit("/", 1)[-1])]
    if not matched:
        return {"output": f"No files matching {pattern!r} under {resolved} on {target['name']}", "exit_code": 0}
    out = "\n".join(matched[:MAX_HITS])
    if len(matched) > MAX_HITS:
        out += f"\n... [capped at {MAX_HITS} of {len(matched)} files]"
    if len(files) >= MAX_LISTING:
        out += f"\n... [only the first {MAX_LISTING} files under {resolved} were looked at]"
    return {"output": f"Under {resolved} on {target['name']}:\n{out}", "exit_code": 0}


def _grep(content: str, target: dict) -> dict:
    args = _args(content, first_line_key="pattern")
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return {"error": "grep: pattern is required", "exit_code": 1}
    path = str(args.get("path") or "").strip() or "."
    ignore_case = bool(args.get("ignore_case"))
    include = str(args.get("glob") or "").strip()
    try:
        hits = max(1, min(int(args.get("max_results") or MAX_HITS), MAX_HITS))
    except (TypeError, ValueError):
        hits = MAX_HITS
    skip_ps = "|".join(re.escape(name) for name in _SKIP_DIRS)
    include_ps = f" -Include {_ps(include.rsplit('/', 1)[-1])}" if include else ""
    powershell = "\n".join([
        _PS_RESOLVE.format(path=_ps(path)),
        "if (-not (Test-Path -LiteralPath $p)) { 'AEGIS:NOFILE'; return }",
        "'AEGIS:OK ' + $p",
        "$files = if (Test-Path -LiteralPath $p -PathType Leaf) { Get-Item -LiteralPath $p -Force } else { "
        f"Get-ChildItem -LiteralPath $p -Recurse -File -Force{include_ps} -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.FullName -notmatch '\\\\({skip_ps})\\\\' }} }}",
        f"$files | Select-String -Pattern {_ps(pattern)}{'' if ignore_case else ' -CaseSensitive'} -ErrorAction SilentlyContinue | "
        f"Select-Object -First {hits} | ForEach-Object {{ '{{0}}:{{1}}:{{2}}' -f $_.Path, $_.LineNumber, $_.Line.Trim() }}",
    ])
    excludes = " ".join(f"--exclude-dir={_sh(name)}" for name in _SKIP_DIRS)
    include_sh = f" --include={_sh(include.rsplit('/', 1)[-1])}" if include else ""
    posix = "\n".join([
        f"p={_sh(path)}",
        'if [ ! -e "$p" ]; then echo AEGIS:NOFILE; exit 0; fi',
        'echo "AEGIS:OK $p"',
        # Perl-style patterns (\\d, \\w, lookarounds) where this grep has them, like the local tool.
        "if echo x | grep -qP x 2>/dev/null; then kind=-P; else kind=-E; fi",
        f"grep -rnI{'i' if ignore_case else ''} $kind {excludes}{include_sh} -e {_sh(pattern)} -- \"$p\" 2>/dev/null | head -n {hits}",
    ])
    try:
        resolved, found = _outcome(_helper(target, powershell, posix, f"grep {pattern[:80]} in {path}", timeout=180), path)
    except RemoteError as exc:
        return {"error": f"grep: {exc}", "exit_code": 1}
    lines = [line[:400] for line in found.splitlines() if line.strip()]
    if not lines:
        return {"output": f"No matches for {pattern!r} under {resolved} on {target['name']}", "exit_code": 0}
    out = "\n".join(lines)
    if len(lines) >= hits:
        out += f"\n... [capped at {hits} matches]"
    return {"output": out, "exit_code": 0}


def _shell(content: str, target: dict) -> dict:
    from src.constants import MAX_OUTPUT_CHARS

    command = (content or "").strip("\n")
    if not command.strip():
        return {"error": "bash: nothing to run", "exit_code": 1}
    if command.lstrip().startswith("#!bg"):
        return {"error": f"bash: background jobs run on this server only, not on {target['name']}. "
                         "Run it in the foreground, or start it detached with that machine's own tools.", "exit_code": 1}
    shell = target.get("shell") or "bash"
    folder = target.get("folder") or ""
    cwd = ""
    if shell == "powershell" and folder:
        command = f"Set-Location -LiteralPath {_ps(folder)} -ErrorAction Stop\n{command}"
    else:
        cwd = folder
    try:
        result = hosts.run(target["name"], command, shell=shell, timeout=SHELL_TIMEOUT, cwd=cwd, output_limit=MAX_OUTPUT_CHARS)
    except common.WorkbenchError as exc:
        return {"error": f"bash: {exc}", "exit_code": 1}
    output = (result.get("output") or "").rstrip()
    err = (result.get("stderr") or "").rstrip()
    if err:
        output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
    if result.get("note"):
        output = (output + "\n" + result["note"]).strip()
    code = int(result.get("exit_code") or 0)
    if code == 124:
        return {"error": f"bash: timed out after {SHELL_TIMEOUT}s on {target['name']}", "exit_code": 124, "stdout": output}
    return {"output": output or "(no output)", "exit_code": code}


def folder_exists(target: dict, path: str) -> str:
    """The full path of a folder on the target, or raise when it is not one."""
    powershell = "\n".join([
        _PS_RESOLVE.format(path=_ps(path)),
        "if (Test-Path -LiteralPath $p -PathType Container) { 'AEGIS:OK ' + (Resolve-Path -LiteralPath $p).ProviderPath } else { 'AEGIS:NOFILE' }",
    ])
    posix = f'p={_sh(path)}\nif [ -d "$p" ]; then echo "AEGIS:OK $(cd -- "$p" && pwd)"; else echo AEGIS:NOFILE; fi'
    resolved, _ = _outcome(_helper({**target, "folder": ""}, powershell, posix, f"check folder {path}"), path)
    return resolved


def _get_workspace(content: str, target: dict) -> dict:
    kind = "Windows, PowerShell" if _windows(target) else f"{target.get('os') or 'linux'}, {target.get('shell') or 'bash'}"
    folder = target.get("folder") or "the login folder (set one with manage_hosts workdir)"
    return {"output": f"Code mode is working on {target['name']} ({kind}). Folder: {folder}.\n"
                      "The shell and the file tools act on that machine, not on this server.", "exit_code": 0}


_TOOLS: Dict[str, Callable[[str, dict], dict]] = {
    "bash": _shell, "read_file": _read_file, "write_file": _write_file, "edit_file": _edit_file,
    "ls": _ls, "glob": _glob, "grep": _grep, "get_workspace": _get_workspace,
}


async def execute(tool: str, content: str, target: dict,
                  progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None) -> Optional[dict]:
    """Carry out one tool call on the target machine. None when the tool is
    not one that moves (the caller then runs it here as usual)."""
    if tool in NOT_THERE:
        return {"error": f"{tool}: " + NOT_THERE[tool].format(name=target["name"]), "exit_code": 1}
    work = _TOOLS.get(tool)
    if work is None:
        return None
    started = time.time()

    async def ticker() -> None:
        while True:
            await asyncio.sleep(2.0)
            try:
                await progress_cb({"elapsed_s": round(time.time() - started, 1), "tail": f"running on {target['name']}…"})
            except Exception:
                pass

    tick = asyncio.create_task(ticker()) if progress_cb and tool == "bash" else None
    try:
        return await asyncio.to_thread(work, content, target)
    except RemoteError as exc:
        return {"error": f"{tool}: {exc}", "exit_code": 1}
    except common.WorkbenchError as exc:
        return {"error": f"{tool}: {exc}", "exit_code": 1}
    except Exception as exc:
        return {"error": f"{tool} on {target['name']} failed: {type(exc).__name__}: {exc}", "exit_code": 1}
    finally:
        if tick is not None:
            tick.cancel()
