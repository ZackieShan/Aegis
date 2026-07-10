import os

import pytest

from tests.helpers.cli_loader import load_script


def test_is_runnable_subcommand_requires_executable_file(tmp_path):
    cli = load_script("aegis")
    sub = tmp_path / "aegis-demo"
    sub.write_text("#!/bin/sh\n")
    sub.chmod(0o644)

    if os.name != "nt":
        # Windows has no executable bit — chmod(0o644) is a no-op there and
        # every existing file passes os.access(X_OK), so only assert the
        # non-executable rejection on POSIX.
        assert cli._is_runnable_subcommand(sub) is False

    sub.chmod(0o755)
    assert cli._is_runnable_subcommand(sub) is True
