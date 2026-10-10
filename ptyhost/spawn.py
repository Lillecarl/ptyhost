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
import signal
import time
import traceback
from typing import NamedTuple, NoReturn

from .backends.posix_utils import pty_make_controlling_tty

__all__ = ("Spawn", "run_in_child")


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


def run_in_child(spawn: Spawn, master: int, slave: int) -> NoReturn:
    "The child's half of a fork: make `slave` its terminal, and exec."
    # The parent's handler would run here until the exec replaces it. A
    # parent that blocked SIGWINCH across the fork gets it back after.
    signal.signal(signal.SIGWINCH, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGWINCH})
    os.close(master)

    pty_make_controlling_tty(slave)
    os.dup2(slave, 0)
    os.dup2(slave, 1)
    os.dup2(slave, 2)

    try:
        _close_file_descriptors()
        if spawn.directory is not None:
            with contextlib.suppress(OSError):
                os.chdir(spawn.directory)
        os.execvpe(spawn.command[0], spawn.command, spawn.environment)
    except Exception:
        traceback.print_exc()

        # The traceback went to the pty, which is the pane. Exiting
        # now would close the pane with it and the person would see
        # nothing. Five seconds is long enough to read that
        # something went wrong and to copy the first line of it.
        time.sleep(5)

    os._exit(1)


def _close_file_descriptors() -> None:
    "Everything above stderr: the program inherits none of the parent's files."
    max_fd = resource.getrlimit(resource.RLIMIT_NOFILE)[-1]
    try:
        os.closerange(3, max_fd)
    except OverflowError:
        # macOS can report a limit closerange does not take,
        # 9223372036854775807. 4096 is what Linux reports.
        os.closerange(3, 4096)
