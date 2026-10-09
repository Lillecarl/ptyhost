"""
A program another owner started, adopted with its pty and its pid.

A hot upgrade hands every pane's program to the new build this way: the
fds and the child survive `execve`, and the new build pumps and reaps
them as its own. Lillecarl/pymux#399.
"""

from __future__ import annotations

import os

import anyio
from test_running_a_program import TIMEOUT, until

from ptyhost import Process
from ptyhost.backends.posix import PosixBackend
from ptyhost.backends.posix_utils import PtyReader

#: Says hello, then echoes each line in brackets until it is killed.
ECHO = "print('hello')\nwhile True:\n    print('<' + input() + '>')\n"


async def test_an_adopted_program_talks_to_its_new_owner_alone():
    import sys

    before, after = [], []
    ended = anyio.Event()
    async with anyio.create_task_group() as task_group:
        old = Process(PosixBackend.from_command([sys.executable, "-c", ECHO]), receive=before.append)
        old.set_size(80, 24)
        await old.start(task_group)
        await until(before, "hello")

        backend = old.backend
        master, slave, pid = backend.master, backend.slave, backend.pid
        backend.release()

        new = Process(PosixBackend.adopt(master, slave, pid), receive=after.append, done_callback=ended.set)
        new.set_size(80, 24)
        await new.start(task_group)
        try:
            new.write_input("again\r")
            await until(after, "<again>")
            assert "<again>" not in "".join(before)
        finally:
            new.kill()
        # The reap ends it, and the pump then reads the end of the file.
        with anyio.fail_after(TIMEOUT):
            await ended.wait()
            while not new.is_terminated:
                await anyio.sleep(0.01)


async def test_a_released_backend_leaves_the_pty_open():
    import sys

    said = []
    async with anyio.create_task_group() as task_group:
        process = Process(PosixBackend.from_command([sys.executable, "-c", ECHO]), receive=said.append)
        process.set_size(80, 24)
        await process.start(task_group)
        await until(said, "hello")
        backend = process.backend
        master, pid = backend.master, backend.pid
        backend.release()
        await anyio.sleep(0.05)
        try:
            os.fstat(master)
        finally:
            os.kill(pid, 9)
            os.close(master)


def test_a_character_cut_in_two_survives_a_new_reader():
    read, write = os.pipe()
    try:
        first = PtyReader(read)
        os.write(write, "é".encode()[:1])
        assert first.read() == ""

        second = PtyReader(read)
        second.decoder_state = first.decoder_state
        os.write(write, "é".encode()[1:])
        assert second.read() == "é"
    finally:
        os.close(read)
        os.close(write)
