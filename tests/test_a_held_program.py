"""
`HeldBackend`: a `Process` whose program the holder forked and reaps.

The holder is real and runs in a process of its own (the `holder`
fixture). Lillecarl/pymux#553.
"""

from __future__ import annotations

import os
import shutil
import sys

import anyio
import pytest

from ptyhost import Process
from ptyhost.backends.posix import spawn_of
from ptyhost.held import HeldBackend, Holding
from ptyhost.holder import HolderError

#: How long a test may wait for a program or the holder, in seconds.
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


async def held(holding: Holding, said, ended=None, *, task_group) -> Process:
    backend = HeldBackend(holding, spawn_of([sys.executable, "-c", ECHO]))
    process = Process(backend=backend, receive=said.append, done_callback=ended)
    process.set_size(80, 24)
    await process.start(task_group)
    return process


async def test_a_held_program_talks_and_ends(holder):
    path, _ = holder
    holding = await Holding.connect(path)
    said: list[str] = []
    ended = anyio.Event()
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(holding.run)
        process = await held(holding, said, ended.set, task_group=task_group)
        await until(said, "READY")
        process.write_input("ping\n")
        await until(said, "<ping>")

        process.write_input("exit 4\n")
        with anyio.fail_after(TIMEOUT):
            await ended.wait()

        assert holding.statuses == {process.backend.program_id: 4}
        assert await holding.programs() == []
        holding.close()


async def test_a_held_program_outlives_the_scope_that_started_it(holder):
    "A server that ends without a word leaves the program to the next one."
    path, _ = holder
    first = await Holding.connect(path)
    said: list[str] = []
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(first.run)
        process = await held(first, said, task_group=task_group)
        await until(said, "READY")
        backend = process.backend
        program_id, pid = backend.program_id, backend.pid
        task_group.cancel_scope.cancel()
    first.close()

    os.kill(pid, 0)  # raises when the program is gone
    second = await Holding.connect(path)
    again: list[str] = []
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(second.run)
        (listed,) = await second.programs()
        assert (listed["id"], listed["pid"], listed["status"]) == (program_id, pid, None)

        adopted = HeldBackend.adopt_held(second, program_id, pid, await second.master(program_id))
        process = Process(backend=adopted, receive=again.append)
        await process.start(task_group)
        process.write_input("still here\n")
        await until(again, "<still here>")

        await second.quit()
        task_group.cancel_scope.cancel()
    second.close()


async def test_a_request_to_a_holder_that_went_says_so(holder):
    "Any request: a reaper releases after the holder may have gone."
    path, holder_process = holder
    holding = await Holding.connect(path)
    holder_process.kill()
    holder_process.wait()

    with pytest.raises(HolderError, match="gone"):
        for _ in range(3):  # the first send may still fit in the buffer
            await holding.release(1)
    holding.close()


async def test_a_program_outlives_its_holder_and_ends_with_its_file(holder):
    "Losing the holder loses the exit code, and not the pane."
    path, holder_process = holder
    holding = await Holding.connect(path)
    said: list[str] = []
    ended = anyio.Event()
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(holding.run)
        process = await held(holding, said, ended.set, task_group=task_group)
        await until(said, "READY")

        holder_process.kill()
        holder_process.wait()
        process.write_input("after\n")
        await until(said, "<after>")
        assert holding.lost and not ended.is_set()

        process.write_input("exit 0\n")
        with anyio.fail_after(TIMEOUT):
            await ended.wait()
        holding.close()


async def test_a_held_program_has_exec_d_by_the_reply(holder):
    "The holder answers a spawn once the exec is done. Lillecarl/pymux#562."
    sleep = shutil.which("sleep")
    if sleep is None:
        pytest.skip("no sleep on the PATH")
    path, _ = holder
    holding = await Holding.connect(path)
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(holding.run)
        backend = HeldBackend(holding, spawn_of([sleep, "30"]))
        process = Process(backend=backend, receive=lambda data: None)
        process.set_size(80, 24)
        await process.start(task_group)
        try:
            assert os.path.basename(backend.get_name() or "") == "sleep"
        finally:
            process.kill()
            holding.close()
