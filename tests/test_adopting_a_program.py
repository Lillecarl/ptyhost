"""
A program that runs already, adopted on its master and its pid.

An upgrade execs the new build in the server's own process, so every
program stays its child, and the new image adopts it from the snapshot.
The one move from the holder takes programs this process never forked.
Lillecarl/pymux#736.
"""

from __future__ import annotations

import os
import signal
import sys

import anyio
import pytest
from test_a_held_program import ECHO, TIMEOUT, until

from ptyhost import Process
from ptyhost.backends.posix import PosixBackend, spawn_of
from ptyhost.held import HeldBackend, Holding
from ptyhost.spawn import Spawn, exec_pipe, fork_on_pipes, run_in_child, wait_for_exec


def _fork(program: str) -> tuple[int, int]:
    "A child on a pty of its own, forked as a server forks a pane: its pid and its master."
    master, slave = os.openpty()
    read_end, exec_end = exec_pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_end)
        run_in_child(Spawn([sys.executable, "-c", program], dict(os.environ)), master, slave, exec_end)
    os.close(exec_end)
    os.close(slave)
    wait_for_exec(read_end)
    return pid, master


async def _adopted(master: int, pid: int, parent: bool, said, ended, task_group) -> Process:
    process = Process(PosixBackend.adopt(master, pid, parent), receive=said.append, done_callback=ended.set)
    process.set_size(80, 24)
    await process.start(task_group)
    return process


async def test_an_adopted_child_talks_and_ends_with_its_code():
    pid, master = _fork(ECHO)
    said: list[str] = []
    ended = anyio.Event()
    async with anyio.create_task_group() as task_group:
        process = await _adopted(master, pid, True, said, ended, task_group)
        await until(said, "READY")
        process.write_input("ping\n")
        await until(said, "<ping>")
        process.write_input("exit 4\n")
        with anyio.fail_after(TIMEOUT):
            await ended.wait()
    assert process.backend.returncode == 4


async def test_a_child_that_ended_before_the_adoption_ends_at_once():
    "It ended during the exec: a zombie of the new image, reaped before any thread waits."
    pid, master = _fork("print('last words', flush=True)\nraise SystemExit(5)\n")
    with anyio.fail_after(TIMEOUT):
        while os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
            await anyio.sleep(0.01)
    said: list[str] = []
    ended = anyio.Event()
    async with anyio.create_task_group() as task_group:
        process = await _adopted(master, pid, True, said, ended, task_group)
        with anyio.fail_after(TIMEOUT):
            await ended.wait()
        await until(said, "last words")
    assert process.backend.returncode == 5


async def test_a_program_of_a_holder_that_is_gone_lives_on_under_its_adopter(holder):
    """
    The one move from the holder: copies of the masters, then SIGTERM,
    which ends the holder and none of its programs. They are not this
    process's children, so their end is seen and their code is not.
    """
    path, holder_process = holder
    holding = await Holding.connect(path)
    said: list[str] = []
    ended = anyio.Event()
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(holding.run)
        held = HeldBackend(holding, spawn_of([sys.executable, "-c", ECHO]))
        held.set_size(80, 24)
        reply = await holding.spawn(held.spawn(), held.master, held.slave)
        os.close(held.slave)
        os.close(held.master)
        master = await holding.master(reply["id"])
        holding.close()
        holder_process.send_signal(signal.SIGTERM)
        holder_process.wait(TIMEOUT)

        process = await _adopted(master, reply["pid"], False, said, ended, task_group)
        await until(said, "READY")
        process.write_input("ping\n")
        await until(said, "<ping>")
        process.kill()
        with anyio.fail_after(TIMEOUT):
            await ended.wait()
    assert process.backend.returncode is None


def test_an_adoption_refuses_what_is_no_pty_master():
    read, write = os.pipe()
    try:
        with pytest.raises(OSError):
            PosixBackend.adopt(read, os.getpid(), True)
    finally:
        os.close(read)
        os.close(write)


def _read_all(fd: int) -> bytes:
    data = b""
    while chunk := os.read(fd, 4096):
        data += chunk
    os.close(fd)
    return data


def test_a_program_on_pipes_is_this_process_child():
    pid, stdout, stderr = fork_on_pipes(Spawn(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"], dict(os.environ)))
    assert (_read_all(stdout), _read_all(stderr)) == (b"out\n", b"err\n")
    assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 3


def test_a_program_on_pipes_that_cannot_start_says_why():
    with pytest.raises(OSError, match="no-such-program"):
        fork_on_pipes(Spawn(["no-such-program"], dict(os.environ)))
