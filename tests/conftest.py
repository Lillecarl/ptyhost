"""
What every test of this suite shares: a guard against a test that hangs.

A hung test held `checks.ptyhost-unit` past ten minutes and wrote no
log at all, so the one fact that mattered -- which await never came --
was lost when the build was stopped by hand. Lillecarl/pymux#546.

`pymux/tests/conftest.py` holds the same guard, and says why it is not
pytest's own `faulthandler_timeout`.
"""

from __future__ import annotations

import faulthandler
import os
import subprocess
import sys
import time

import pytest

#: How long one test may take before it is stuck. The check sets it.
HANG_SECONDS = float(os.environ.get("PTYHOST_HANG_SECONDS") or 0)

#: Where the stacks go: a copy of stderr made before any test's capture,
#: which the exit would otherwise throw away with the capture.
_stacks_go_to: int | None = None


def pytest_configure(config):
    global _stacks_go_to
    try:
        fileno = sys.stderr.fileno()
    except AttributeError, ValueError, OSError:
        fileno = sys.__stderr__.fileno()
    _stacks_go_to = os.dup(fileno)


@pytest.fixture
def holder(tmp_path):
    "A holder (`ptyhost.holder`) in the foreground, and the path of its socket."
    path = str(tmp_path / "holder.sock")
    process = subprocess.Popen([sys.executable, "-m", "ptyhost.holder", "--socket", path])
    deadline = time.monotonic() + 5.0
    while not os.path.exists(path):
        assert process.poll() is None, "the holder ended at start"
        assert time.monotonic() < deadline, "the holder never bound its socket"
        time.sleep(0.01)
    try:
        yield path, process
    finally:
        process.kill()
        process.wait()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item):
    "Dump every thread's stack and end the run when a test hangs."
    if HANG_SECONDS:
        faulthandler.dump_traceback_later(HANG_SECONDS, exit=True, file=_stacks_go_to)
    try:
        yield
    finally:
        if HANG_SECONDS:
            faulthandler.cancel_dump_traceback_later()
