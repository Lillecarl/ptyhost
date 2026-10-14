"""
A program a person runs starts at normal priority, whatever pymux was
reniced to. Lillecarl/pymux#673.

The sandbox may not lower a niceness, so a raised one is what
`os.getpriority` answers here.
"""

from __future__ import annotations

import os
import subprocess
import sys

from ptyhost.spawn import at_normal_priority


def _set_calls(monkeypatch, current: int) -> list[tuple[int, int, int]]:
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(os, "getpriority", lambda which, who: current)
    monkeypatch.setattr(os, "setpriority", lambda *args: calls.append(args))
    return calls


def test_a_raised_priority_goes_back_to_normal(monkeypatch):
    calls = _set_calls(monkeypatch, -10)
    at_normal_priority()
    assert calls == [(os.PRIO_PROCESS, 0, 0)]


def test_a_lowered_priority_stays(monkeypatch):
    calls = _set_calls(monkeypatch, 5)
    at_normal_priority()
    assert calls == []


def test_the_wrapper_runs_the_program():
    done = subprocess.run(
        [sys.executable, "-m", "ptyhost.normal_priority", "sh", "-c", "echo $0", "here"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (done.returncode, done.stdout) == (0, "here\n")


def test_the_wrapper_says_a_missing_program_as_a_shell_does():
    done = subprocess.run(
        [sys.executable, "-m", "ptyhost.normal_priority", "no-such-program-here"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 127
    assert "no-such-program-here" in done.stderr
