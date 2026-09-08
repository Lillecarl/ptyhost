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

import asyncio
import sys

import pytest

from ptyhost import Process
from ptyhost.backends.posix import PosixBackend

#: How long a test may wait for a program to say something, in seconds.
#: Every one of these is a fork and a write, so this is generous and a
#: run that reaches it has gone wrong.
TIMEOUT = 5.0

#: How often the loop is turned while waiting. The reader runs on it.
TICK = 0.01

#: What keeps a program alive after it has said its piece. The test
#: kills it, so the number only has to be longer than the test.
LINGER = "\nimport time\ntime.sleep(30)\n"


async def until(said, text: str) -> None:
    "Wait for `text` to turn up in what the program wrote."
    deadline = asyncio.get_event_loop().time() + TIMEOUT
    while text not in "".join(said):
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(
                "waited %g seconds for %r; the program wrote %r"
                % (TIMEOUT, text, "".join(said))
            )
        await asyncio.sleep(TICK)


def running(program: str, said, ended=None, linger: bool = True, priority=None):
    "A `Process` on a python program, sized and started."
    command = [sys.executable, "-c", program + (LINGER if linger else "")]
    backend = PosixBackend.from_command(command)
    process = Process(
        backend=backend,
        receive=said.append,
        done_callback=ended,
        has_priority=priority,
    )
    process.set_size(80, 24)
    process.start()
    return process


async def test_what_a_program_writes_reaches_the_callback():
    said = []
    process = running("print('hello from the pty')", said)
    try:
        await until(said, "hello from the pty")
    finally:
        process.kill()


async def test_what_a_caller_writes_reaches_the_program():
    "The program reads a line and writes it back."
    said = []
    process = running("print('<' + input() + '>')", said)
    try:
        # The pty echoes as well, so the marks are what the program made.
        process.write_input("ping\n")
        await until(said, "<ping>")
    finally:
        process.kill()


async def test_the_program_is_told_how_big_the_pty_is():
    said = []
    process = running(
        "import os\n"
        "size = os.get_terminal_size()\n"
        "print('SIZE %d %d' % (size.columns, size.lines))",
        said,
    )
    try:
        await until(said, "SIZE 80 24")
    finally:
        process.kill()


async def test_a_resize_reaches_a_program_that_is_already_running():
    said = []
    process = running(
        "import os, signal, sys\n"
        "def report(*_):\n"
        "    size = os.get_terminal_size()\n"
        "    print('SIZE %d %d' % (size.columns, size.lines), flush=True)\n"
        "signal.signal(signal.SIGWINCH, report)\n"
        "print('READY', flush=True)",
        said,
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
    ended = asyncio.Event()
    process = running("pass", said, ended.set, linger=False)
    try:
        await asyncio.wait_for(ended.wait(), TIMEOUT)
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
    ended = asyncio.Event()
    process = running(
        "import sys; sys.stdout.write('the last word'); sys.stdout.flush()",
        said,
        ended.set,
        linger=False,
    )
    try:
        await asyncio.wait_for(ended.wait(), TIMEOUT)
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
    ended = asyncio.Event()
    process = running("pass", said, ended.set, linger=False)
    try:
        await asyncio.wait_for(ended.wait(), TIMEOUT)
        deadline = asyncio.get_event_loop().time() + 1.0
        while not process.is_terminated:
            assert asyncio.get_event_loop().time() < deadline
            await asyncio.sleep(TICK)
    finally:
        process.kill()


async def test_a_suspended_program_is_not_read():
    """
    Copy mode suspends a pane. The program keeps running and nothing
    reads it, and a resume picks up what it wrote in the meantime.
    """
    said = []
    process = running("import time\ntime.sleep(0.2)\nprint('late', flush=True)", said)
    try:
        process.suspend()
        await asyncio.sleep(0.4)
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
    process = running("print('last', flush=True)", said, linger=False)
    try:
        process.suspend()
        await asyncio.sleep(0.4)
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
    nobody_is_looking = lambda: False  # noqa: E731
    programs = [
        running(
            "print('pane %d', flush=True)" % number,
            said,
            priority=nobody_is_looking,
        )
        for number, said in enumerate(watched)
    ]
    try:
        for number, said in enumerate(watched):
            await until(said, "pane %d" % number)
    finally:
        for process in programs:
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
    process = running(
        "import sys, time\n"
        "for byte in %r:\n"
        "    sys.stdout.buffer.write(bytes([byte]))\n"
        "    sys.stdout.buffer.flush()\n"
        "    time.sleep(0.01)\n" % (text.encode("utf-8"),),
        said,
    )
    try:
        await until(said, text)
    finally:
        process.kill()
