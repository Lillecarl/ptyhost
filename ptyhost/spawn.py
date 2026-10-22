"""
What a child needs to start, and what it does between the fork and the
exec.

Standard library only: the holder (`ptyhost.holder`) forks through
this too, and it imports nothing it does not need. Lillecarl/pymux#553.
"""

from __future__ import annotations

import contextlib
import os
import resource
import selectors
import signal
import time
import traceback
import warnings
from typing import NamedTuple, NoReturn

from .backends.posix_utils import pty_make_controlling_tty

__all__ = (
    "Spawn",
    "at_normal_priority",
    "exec_failure",
    "exec_pipe",
    "fork_on_pipes",
    "run_in_child",
    "run_on_pipes",
    "wait_for_exec",
)


class Spawn(NamedTuple):
    """
    A start, as data. The child runs none of its caller's Python, so
    a process that holds the ptys can fork from one of these.

    `command[0]` is looked up on the PATH of `environment`, not on the
    PATH of whoever forks.
    """

    command: list[str]
    environment: dict[str, str]
    directory: str | None = None


def exec_pipe() -> tuple[int, int]:
    """
    A pipe that says when a child has exec'd: the end the child keeps
    closes on exec, because `os.pipe` makes both ends close-on-exec.

    **A caller reads it before it treats the program as running.**
    Until the exec, the pty's foreground group is the fork of the
    caller, so a name read off it is the caller's and not the
    program's. Lillecarl/pymux#562.
    """
    return os.pipe()


#: How long a spawn waits for its child's exec. An exec takes
#: milliseconds; this bounds a child stuck before it -- a `chdir` into a
#: hung mount -- so the holder's one loop does not stop with it.
EXEC_SECONDS = 5.0


def wait_for_exec(read_end: int, seconds: float = EXEC_SECONDS) -> None:
    "Block until the child that holds the other end has exec'd or gone, or `seconds` pass."
    # A selector and not `select.select`, which refuses an fd past 1024,
    # and a holder of many panes holds that many.
    try:
        with selectors.DefaultSelector() as waiting:
            waiting.register(read_end, selectors.EVENT_READ)
            if waiting.select(seconds):
                os.read(read_end, 1)
    finally:
        os.close(read_end)


def run_in_child(spawn: Spawn, master: int, slave: int, exec_end: int) -> NoReturn:
    """
    The child's half of a fork: make `slave` its terminal, and exec.

    `exec_end` is the write end of an `exec_pipe`, closed by the exec.
    """
    # **Nothing raised here may leave this function.** The caller's
    # stack is the holder's loop or the server's, and a child that
    # returns into it runs a second copy of that process.
    try:
        try:
            # The parent's handler would run here until the exec
            # replaces it. A parent that blocked SIGWINCH across the
            # fork gets it back after.
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGWINCH})
            os.close(master)

            pty_make_controlling_tty(slave)
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
        except BaseException:
            os._exit(1)  # Not on the pty yet, so nobody would read why.

        try:
            _close_file_descriptors(keep=exec_end)
            at_normal_priority()
            if spawn.directory is not None:
                with contextlib.suppress(OSError):
                    os.chdir(spawn.directory)
            os.execvpe(spawn.command[0], spawn.command, spawn.environment)
        except BaseException:
            # Before the traceback and the wait below: the parent waits
            # on this end, and five seconds of it would stall a spawn.
            with contextlib.suppress(OSError):
                os.close(exec_end)
            traceback.print_exc()

            # The traceback went to the pty, which is the pane. Exiting
            # now would close the pane with it and the person would see
            # nothing. Five seconds is long enough to read that
            # something went wrong and to copy the first line of it.
            time.sleep(5)
    finally:
        os._exit(1)


def exec_failure(read_end: int, seconds: float = EXEC_SECONDS) -> str | None:
    """
    Why the child that holds the other end could not exec, or None once
    it has. `run_on_pipes` writes the reason before it exits; an exec
    closes the end with nothing written.
    """
    said = b""
    deadline = time.monotonic() + seconds
    try:
        with selectors.DefaultSelector() as waiting:
            waiting.register(read_end, selectors.EVENT_READ)
            while (left := deadline - time.monotonic()) > 0 and waiting.select(left):
                chunk = os.read(read_end, 4096)
                if not chunk:
                    break
                said += chunk
    finally:
        os.close(read_end)
    return said.decode(errors="replace") if said else None


def run_on_pipes(spawn: Spawn, stdout: int, stderr: int, exec_end: int) -> NoReturn:
    """
    The child's half of a fork for a program with no terminal: nothing
    on stdin, `stdout` and `stderr` the write ends of two pipes, and a
    session of its own, so a hang-up reaches the whole group.

    A directory that is gone, or an exec that fails, writes its reason
    on `exec_end` for `exec_failure`: no terminal shows it here.
    """
    # **Nothing raised here may leave this function**, as in `run_in_child`.
    try:
        try:
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGWINCH})
            os.setsid()
            null = os.open(os.devnull, os.O_RDONLY)
            os.dup2(null, 0)
            os.dup2(stdout, 1)
            os.dup2(stderr, 2)
            _close_file_descriptors(keep=exec_end)
            at_normal_priority()
            if spawn.directory is not None:
                os.chdir(spawn.directory)
            os.execvpe(spawn.command[0], spawn.command, spawn.environment)
        except BaseException as error:
            with contextlib.suppress(OSError):
                os.write(exec_end, ("%s" % (error,)).encode(errors="replace") or b"failed")
    finally:
        os._exit(127)


def fork_on_pipes(spawn: Spawn) -> tuple[int, int, int]:
    """
    Start a program with no terminal, as this process's child: its pid,
    and the read ends of its stdout and stderr. A program that cannot
    start raises `OSError` with the child's reason, and leaves nothing
    behind. Blocks until the child has exec'd, which takes milliseconds.
    """
    made: list[int] = []
    try:
        for _ in range(3):
            made.extend(os.pipe())
        # SIGWINCH blocked across the fork, as `PosixBackend.start` says.
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGWINCH})
        pid = -1
        try:
            # The child execs at once, as `PosixBackend.start` says.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                pid = os.fork()
        finally:
            if pid != 0:
                signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
    except BaseException:
        for fd in made:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise
    out_read, out_write, err_read, err_write, read_end, exec_end = made
    if pid == 0:
        for fd in (out_read, err_read, read_end):
            os.close(fd)
        run_on_pipes(spawn, out_write, err_write, exec_end)
    for fd in (out_write, err_write, exec_end):
        os.close(fd)
    failure = exec_failure(read_end)
    if failure is not None:
        os.close(out_read)
        os.close(err_read)
        # It exits at once, and nothing else here knows its pid.
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)
        raise OSError(failure)
    return pid, out_read, err_read


def at_normal_priority() -> None:
    """
    Give back a raised priority, before an exec. `pymux renice` raises
    pymux's, and a program a person runs takes none of it. A lowered
    one stays: only root may raise it again.

    On Linux a niceness is the calling thread's, which is the one that
    execs.
    """
    with contextlib.suppress(OSError):
        if os.getpriority(os.PRIO_PROCESS, 0) < 0:
            os.setpriority(os.PRIO_PROCESS, 0, 0)


def _close_file_descriptors(keep: int) -> None:
    """
    Everything above stderr but `keep`: the program inherits none of the
    parent's files. `keep` closes on exec by itself.
    """
    max_fd = resource.getrlimit(resource.RLIMIT_NOFILE)[-1]
    try:
        os.closerange(keep + 1, max_fd)
    except OverflowError:
        # macOS can report a limit closerange does not take,
        # 9223372036854775807. 4096 is what Linux reports.
        os.closerange(keep + 1, 4096)
    os.closerange(3, keep)
