"""
`hang_up`: a pane that a person closes sends its program SIGHUP, the
way closing a terminal does.

SIGHUP first, so a shell runs its handler and writes its history, and
SIGKILL `GRACE` seconds later for a program that ignores it. A killed
shell lost the line that killed it from its history: `pymux wl` in a
pane, which turns the pane into the window, never reached
`~/.bash_history`.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import time

import anyio
import pytest
from test_running_a_program import until

from ptyhost import Process
from ptyhost.backends import posix
from ptyhost.backends.posix import PosixBackend, spawn_of
from ptyhost.held import HeldBackend, Holding

#: How long a test may wait for a program to end, in seconds.
TIMEOUT = 5.0

#: Ignores SIGHUP, says so, and waits to be killed.
IGNORES = (
    "import signal, time\nsignal.signal(signal.SIGHUP, signal.SIG_IGN)\nprint('READY', flush=True)\ntime.sleep(30)\n"
)


def bash() -> str:
    found = shutil.which("bash")
    if found is None:
        pytest.skip("no bash on the PATH")
    return found


async def started(backend: PosixBackend, said: list[str], *, task_group) -> Process:
    process = Process(backend=backend, receive=said.append)
    process.set_size(80, 24)
    await process.start(task_group)
    return process


async def ended(process: Process) -> None:
    with anyio.fail_after(TIMEOUT):
        await process.backend.ready_f.wait()


async def test_a_shell_runs_its_sighup_trap(tmp_path):
    "A `trap ... HUP` gets to run, which a SIGKILL never let it."
    handled = tmp_path / "handled"
    script = "trap 'echo handled > %s; exit 0' HUP; echo READY; while :; do sleep 0.05; done" % handled
    said: list[str] = []
    async with anyio.create_task_group() as task_group:
        process = await started(PosixBackend(spawn_of([bash(), "-c", script])), said, task_group=task_group)
        await until(said, "READY")
        process.hang_up()
        await ended(process)

    assert handled.read_text() == "handled\n"
    assert process.backend.returncode == 0


async def test_an_interactive_shell_writes_its_history(tmp_path):
    "The bug itself: the line typed before the pane went reaches HISTFILE."
    history = tmp_path / "history"

    def environment(env: dict[str, str]) -> None:
        env.update(HISTFILE=str(history), HISTSIZE="100", HISTFILESIZE="100", PS1="PROMPT$ ")

    said: list[str] = []
    backend = PosixBackend(spawn_of([bash(), "--norc", "--noprofile", "-i"], environment))
    async with anyio.create_task_group() as task_group:
        process = await started(backend, said, task_group=task_group)
        await until(said, "PROMPT$ ")
        process.write_input("echo marker-$((6 * 7))\n")
        await until(said, "marker-42")
        process.hang_up()
        await ended(process)

    assert "echo marker-$((6 * 7))" in history.read_text().splitlines()
    assert backend.returncode == -signal.SIGHUP


async def test_a_program_that_ignores_sighup_is_killed_after_the_grace(monkeypatch):
    monkeypatch.setattr(posix, "GRACE", 0.5)
    said: list[str] = []
    async with anyio.create_task_group() as task_group:
        command = [sys.executable, "-c", IGNORES]
        process = await started(PosixBackend(spawn_of(command)), said, task_group=task_group)
        await until(said, "READY")
        hung_up_at = time.monotonic()
        process.hang_up()
        await ended(process)
        took = time.monotonic() - hung_up_at

    assert process.backend.returncode == -signal.SIGKILL
    # Not before the grace: SIGHUP alone left it running.
    assert took >= 0.5


async def test_a_reaped_program_is_never_signalled(monkeypatch):
    "Its number may be another process's by now."
    said: list[str] = []
    async with anyio.create_task_group() as task_group:
        command = [sys.executable, "-c", "print('BYE')"]
        process = await started(PosixBackend(spawn_of(command)), said, task_group=task_group)
        await ended(process)

        sent = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
        process.hang_up()
        process.kill()

    assert sent == []


async def test_a_held_program_that_ignores_sighup_is_killed_too(holder, monkeypatch):
    "The holder reaps it, and the server still insists after the grace."
    monkeypatch.setattr(posix, "GRACE", 0.5)
    path, _ = holder
    holding = await Holding.connect(path)
    said: list[str] = []
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(holding.run)
        backend = HeldBackend(holding, spawn_of([sys.executable, "-c", IGNORES]))
        process = await started(backend, said, task_group=task_group)
        await until(said, "READY")
        process.hang_up()
        await ended(process)
        assert holding.statuses == {backend.program_id: -signal.SIGKILL}
        holding.close()
