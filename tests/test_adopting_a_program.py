"""
A program that runs already, adopted on its master and its pid.

An upgrade execs the new build in the server's own process, so every
program stays its child, and the new image adopts it from the snapshot.
A program the server is not the parent of still runs: Carl's panes of
2026-10-22 were forked by the holder that is gone. Lillecarl/pymux#736.
"""

from __future__ import annotations

import os
import sys

import anyio
import pytest

from ptyhost import Process
from ptyhost.backends.posix import PosixBackend
from ptyhost.spawn import Spawn, exec_pipe, fork_on_pipes, run_in_child, wait_for_exec

#: How long a test may wait for a program, in seconds.
TIMEOUT = 5.0
TICK = 0.01

#: Echoes each line it reads, marked, until it reads "exit N".
ECHO = (
    "import sys\n"
    "print('READY', flush=True)\n"
    "for line in sys.stdin:\n"
    "    if line.startswith('exit '):\n"
    "        sys.exit(int(line.split()[1]))\n"
    "    print('<' + line.strip() + '>', flush=True)\n"
)


async def until(said, text: str) -> None:
    try:
        with anyio.fail_after(TIMEOUT):
            while text not in "".join(said):
                await anyio.sleep(TICK)
    except TimeoutError:
        raise AssertionError("waited for %r; the program wrote %r" % (text, "".join(said)))


def fork_on_a_pty(program: str, orphan: bool = False) -> tuple[int, int]:
    """
    A child on a pty of its own, forked as a server forks a pane: its pid
    and its master. An `orphan` is forked by a child that ends at once,
    so this process is not its parent.
    """
    master, slave = os.openpty()
    read_end, exec_end = exec_pipe()
    pid_read, pid_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_end)
        os.close(pid_read)
        if orphan and (grandchild := os.fork()):
            os.write(pid_write, b"%d" % grandchild)
            os._exit(0)
        os.close(pid_write)
        run_in_child(Spawn([sys.executable, "-c", program], dict(os.environ)), master, slave, exec_end)
    os.close(exec_end)
    os.close(slave)
    os.close(pid_write)
    if orphan:
        os.waitpid(pid, 0)
        pid = int(os.read(pid_read, 32))
    os.close(pid_read)
    wait_for_exec(read_end)
    return pid, master


async def _adopted(master: int, pid: int, parent: bool, said, ended, task_group) -> Process:
    process = Process(PosixBackend.adopt(master, pid, parent), receive=said.append, done_callback=ended.set)
    process.set_size(80, 24)
    await process.start(task_group)
    return process


async def test_an_adopted_child_talks_and_ends_with_its_code():
    pid, master = fork_on_a_pty(ECHO)
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
    pid, master = fork_on_a_pty("print('last words', flush=True)\nraise SystemExit(5)\n")
    # Ended, and left unreaped.
    with anyio.fail_after(TIMEOUT):
        await anyio.to_thread.run_sync(os.waitid, os.P_PID, pid, os.WEXITED | os.WNOWAIT)
    said: list[str] = []
    ended = anyio.Event()
    async with anyio.create_task_group() as task_group:
        process = await _adopted(master, pid, True, said, ended, task_group)
        with anyio.fail_after(TIMEOUT):
            await ended.wait()
        await until(said, "last words")
    assert process.backend.returncode == 5


async def test_a_program_of_another_parent_lives_on_under_its_adopter():
    "Not this process's child, so its end is seen and its code is not."
    pid, master = fork_on_a_pty(ECHO, orphan=True)
    said: list[str] = []
    ended = anyio.Event()
    async with anyio.create_task_group() as task_group:
        process = await _adopted(master, pid, False, said, ended, task_group)
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
