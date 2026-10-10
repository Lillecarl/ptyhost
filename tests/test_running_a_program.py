"""
A program on a pty, from end to end.

`Process` starts a program, pumps what it writes to `receive`, and
writes back what a caller sends. Nothing here parses any of it, which
is the whole shape of this package.

**Most programs below wait at the end instead of exiting.** They were
written that way because what a program wrote just before it exited
was sometimes lost: the reaper closed the pty without draining it, so
a test that ended its program at once failed about once in a dozen
runs for a reason that had nothing to do with what it asked. The reap
drains now (Lillecarl/pymux#121), and `linger=False` says so where it
is the point.
"""

from __future__ import annotations

import os
import signal
import sys

import anyio
import pytest

from ptyhost import Process
from ptyhost.backends.posix import PosixBackend, spawn_of

#: How long a test may wait for a program to say something, in seconds.
#: Every one of these is a fork and a write, so this is generous and a
#: run that reaches it has gone wrong.
TIMEOUT = 5.0

#: How often to look while waiting. The pump runs on its own task, so
#: this only sets how fast the test notices.
TICK = 0.01

#: What keeps a program alive after it has said its piece. The test
#: kills it, so the number only has to be longer than the test.
LINGER = "\nimport time\ntime.sleep(30)\n"


async def until(said, text: str) -> None:
    "Wait for `text` to turn up in what the program wrote."
    try:
        with anyio.fail_after(TIMEOUT):
            while text not in "".join(said):
                await anyio.sleep(TICK)
    except TimeoutError:
        raise AssertionError("waited %g seconds for %r; the program wrote %r" % (TIMEOUT, text, "".join(said)))


async def running(program: str, said, ended=None, linger: bool = True, priority=None, *, task_group):
    "A `Process` on a python program, sized and started, watched by the group."
    command = [sys.executable, "-c", program + (LINGER if linger else "")]
    backend = PosixBackend(spawn_of(command))
    process = Process(
        backend=backend,
        receive=said.append,
        done_callback=ended,
        has_priority=priority,
    )
    process.set_size(80, 24)
    await process.start(task_group)
    return process


async def test_what_a_program_writes_reaches_the_callback():
    said = []
    async with anyio.create_task_group() as task_group:
        process = await running("print('hello from the pty')", said, task_group=task_group)
        try:
            await until(said, "hello from the pty")
        finally:
            process.kill()


async def test_what_a_caller_writes_reaches_the_program():
    "The program reads a line and writes it back."
    said = []
    async with anyio.create_task_group() as task_group:
        process = await running("print('<' + input() + '>')", said, task_group=task_group)
        try:
            # The pty echoes as well, so the marks are what the program made.
            process.write_input("ping\n")
            await until(said, "<ping>")
        finally:
            process.kill()


async def test_the_program_is_told_how_big_the_pty_is():
    said = []
    async with anyio.create_task_group() as task_group:
        process = await running(
            "import os\nsize = os.get_terminal_size()\nprint('SIZE %d %d' % (size.columns, size.lines))",
            said,
            task_group=task_group,
        )
        try:
            await until(said, "SIZE 80 24")
        finally:
            process.kill()


async def test_a_resize_reaches_a_program_that_is_already_running():
    said = []
    async with anyio.create_task_group() as task_group:
        process = await running(
            "import os, signal, sys\n"
            "def report(*_):\n"
            "    size = os.get_terminal_size()\n"
            "    print('SIZE %d %d' % (size.columns, size.lines), flush=True)\n"
            "signal.signal(signal.SIGWINCH, report)\n"
            "print('READY', flush=True)",
            said,
            task_group=task_group,
        )
        try:
            await until(said, "READY")
            process.set_size(40, 10)
            await until(said, "SIZE 40 10")
        finally:
            process.kill()


async def test_the_end_of_a_program_is_reported():
    "The one program that ends. Nothing here reads what it wrote."
    said = []
    ended = anyio.Event()
    async with anyio.create_task_group() as task_group:
        process = await running("pass", said, ended.set, linger=False, task_group=task_group)
        try:
            with anyio.fail_after(TIMEOUT):
                await ended.wait()
        finally:
            process.kill()


@pytest.mark.parametrize("run", range(12))
async def test_the_last_thing_a_program_writes_arrives(run):
    """
    A program writes and exits at once, and every character of it
    reaches the screen.

    It did not. The reaper closed the pty without reading it, so what
    the kernel still held went with it whenever the loop had not
    turned since the write. Eleven of twelve runs arrived, and the one
    that failed lost the lot.

    That is the last thing a program draws before it quits, which is
    often the thing a person wanted: the error a build printed as it
    died, the summary a test runner writes on its last line. Twelve
    runs, because once is not a measurement of a race.
    Lillecarl/pymux#121.
    """
    said = []
    ended = anyio.Event()
    async with anyio.create_task_group() as task_group:
        process = await running(
            "import sys; sys.stdout.write('the last word'); sys.stdout.flush()",
            said,
            ended.set,
            linger=False,
            task_group=task_group,
        )
        try:
            with anyio.fail_after(TIMEOUT):
                await ended.wait()
            await until(said, "the last word")
        finally:
            process.kill()


async def test_a_program_that_ended_is_terminated():
    """
    `is_terminated` says there is nothing more to read.

    It never became true. The reader sets its flag on a read that
    gives back nothing, and nothing reads after the child is reaped:
    `_waitpid` takes the reader away and closes the pty. So a pane
    whose program had exited read as alive for the life of the server,
    and `#{pane_dead}` answered "0" forever.
    Lillecarl/pymux#120.
    """
    said = []
    ended = anyio.Event()
    async with anyio.create_task_group() as task_group:
        process = await running("pass", said, ended.set, linger=False, task_group=task_group)
        try:
            with anyio.fail_after(TIMEOUT):
                await ended.wait()
            with anyio.fail_after(1.0):
                while not process.is_terminated:
                    await anyio.sleep(TICK)
        finally:
            process.kill()


async def test_a_suspended_program_is_not_read():
    """
    Copy mode suspends a pane. The program keeps running and nothing
    reads it, and a resume picks up what it wrote in the meantime.
    """
    said = []
    async with anyio.create_task_group() as task_group:
        process = await running("import time\ntime.sleep(0.2)\nprint('late', flush=True)", said, task_group=task_group)
        try:
            process.suspend()
            await anyio.sleep(0.4)
            assert "late" not in "".join(said)
            process.resume()
            await until(said, "late")
        finally:
            process.kill()


async def test_a_program_that_ends_while_suspended_waits_for_the_resume():
    """
    Copy mode stops a pane, and the promise is that no row of the
    screen changes while a person reads it. A program that ends in the
    meantime does not get to break that: its last words wait for the
    resume, the way anything else it wrote does.

    The reap makes the end of the file reachable (Lillecarl/pymux#121),
    so the reader has something to give the moment it goes back on the
    loop. It may not go back by itself.
    """
    said = []
    async with anyio.create_task_group() as task_group:
        process = await running("print('last', flush=True)", said, linger=False, task_group=task_group)
        try:
            process.suspend()
            await anyio.sleep(0.4)
            assert "last" not in "".join(said)
            process.resume()
            await until(said, "last")
        finally:
            process.kill()


async def test_several_programs_that_nobody_watches_are_still_read():
    """
    A program that nobody is looking at waits for a turn of the event
    loop that nothing else wants, or for its deadline, whichever comes
    first. **The deadline is what makes this safe**, and it is easy to
    lose: the re-queues that each waiting program makes are what keeps
    the loop busy for the others, so with more than one of them an idle
    loop never comes.

    pymux froze entirely on two panes of Claude Code for exactly that
    reason, for as long as a deadline of `time.time() + 1` was read as
    a duration and landed fifty-seven years out. Lillecarl/pymux#122.
    """
    watched = [[] for _ in range(4)]

    def nobody_is_looking():
        return False

    async with anyio.create_task_group() as task_group:
        programs = [
            await running(
                "print('pane %d', flush=True)" % number,
                said,
                priority=nobody_is_looking,
                task_group=task_group,
            )
            for number, said in enumerate(watched)
        ]
        try:
            for number, said in enumerate(watched):
                await until(said, "pane %d" % number)
        finally:
            for process in programs:
                process.kill()


async def test_a_program_starts_in_the_environment_and_directory_it_was_given(tmp_path):
    """
    The edit runs here and not in the child, so this process keeps its
    own environment, and the program is found on the PATH it was given.
    Lillecarl/pymux#553.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "only-here").symlink_to(sys.executable)
    program = "import os, time; print('%s in %s' % (os.environ['GIVEN'], os.getcwd()), flush=True); time.sleep(30)"

    def environment(env: dict[str, str]) -> None:
        env["GIVEN"] = "given"
        env["PATH"] = str(bin_dir)

    before = dict(os.environ)
    said: list[str] = []
    backend = PosixBackend(spawn_of(["only-here", "-c", program], environment, str(tmp_path)))
    async with anyio.create_task_group() as task_group:
        process = Process(backend=backend, receive=said.append)
        process.set_size(400, 24)
        await process.start(task_group)
        try:
            await until(said, "given in %s" % (tmp_path,))
            assert dict(os.environ) == before
        finally:
            process.kill()


async def test_a_program_gets_sigwinch_and_this_process_keeps_its_mask():
    "The fork blocks SIGWINCH until the child has reset it; neither side stays blocked."
    program = (
        "blocked = int(next(l for l in open('/proc/self/status') if l.startswith('SigBlk:')).split()[1], 16)\n"
        "print('WINCH BLOCKED' if blocked & (1 << (%d - 1)) else 'WINCH FREE', flush=True)" % signal.SIGWINCH
    )
    said: list[str] = []
    async with anyio.create_task_group() as task_group:
        process = await running(program, said, task_group=task_group)
        try:
            await until(said, "WINCH")
            assert "WINCH FREE" in "".join(said)
            assert signal.SIGWINCH not in signal.pthread_sigmask(signal.SIG_BLOCK, [])
        finally:
            process.kill()


@pytest.mark.parametrize("text", ["ä", "日本", "🙂"])
async def test_a_character_split_across_two_reads_survives(text):
    """
    A read can end in the middle of a character. `PtyReader` keeps what
    it cannot finish until the rest arrives.
    """
    said = []
    # The bytes and not the character: the source of the child is plain
    # ASCII this way, so what a locale would do to it is not part of
    # the question.
    async with anyio.create_task_group() as task_group:
        process = await running(
            "import sys, time\n"
            "for byte in %r:\n"
            "    sys.stdout.buffer.write(bytes([byte]))\n"
            "    sys.stdout.buffer.flush()\n"
            "    time.sleep(0.01)\n" % (text.encode("utf-8"),),
            said,
            task_group=task_group,
        )
        try:
            await until(said, text)
        finally:
            process.kill()
