"""Helpers for resolving runtime paths in source and frozen builds."""

import os
import sys


def get_app_root() -> str:
    """Return the app root directory.

    In normal source runs, this is the repository root. In a frozen Windows
    build, it is the bundle content root (PyInstaller's internal directory)
    so bundled runtime folders like `static/`, `scripts/`, and `data/` stay
    together with the executable payload.
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_python_exe():
    """A REAL Python interpreter for spawning child processes, or None.

    In source runs this is sys.executable (the venv python). In a frozen
    PyInstaller build sys.executable is Aegis.exe itself — spawning it with
    a script path forks whole new app instances instead of running the
    script. Callers that spawn python children (built-in MCP servers, the
    agent python tool, pip installs) must use this and degrade with a clear
    message when it returns None.
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    import shutil
    for name in ("python", "python3"):
        exe = shutil.which(name)
        if exe and os.path.normcase(exe) != os.path.normcase(sys.executable):
            return exe
    return None


def get_default_data_dir() -> str:
    """Return the default path to the data directory.

    In normal runs, this is a 'data' subdirectory under the app root.
    In frozen builds, it is a persistent user directory (~/.aegis/data)
    to prevent SQLite databases and other persistent files from being
    written to the ephemeral, temporary extraction bundle directory.
    """
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.expanduser("~"), ".aegis", "data")
    return os.path.join(get_app_root(), "data")