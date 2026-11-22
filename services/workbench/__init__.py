"""Workbench: build, host and operate things from a chat.

``projects``   code folders (git repositories) and which chat works in which
``apps``       a project's app kept running as a systemd user service with a tailnet URL
``hosts``      commands and file copies on other machines over SSH
``delegates``  hand-offs to other coding agents installed on this server

See ``docs/workbench.md``.
"""

from . import apps, common, delegates, hosts, projects
from .common import WorkbenchError, enabled

__all__ = ["apps", "common", "delegates", "hosts", "projects", "WorkbenchError", "enabled"]
