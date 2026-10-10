from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
import signal
import stat
import struct
import sys
import time
import warnings
from collections.abc import Callable
from typing import ClassVar

import anyio
import anyio.abc

from ..spawn import Spawn, run_in_child
from .base import Backend
from .posix_utils import PtyReader, set_terminal_size

logger = logging.getLogger(__name__)

__all__ = ["PosixBackend", "spawn_of"]


def spawn_of(
    command: list[str],
    environment: Callable[[dict[str, str]], None] | None = None,
    directory: str | None = None,
) -> Callable[[], Spawn]:
    "What `start` calls for the `Spawn`: a copy of this environment, as `environment` edits it."
    assert isinstance(command, list)

    def spawn() -> Spawn:
        env = dict(os.environ)
        if environment is not None:
            environment(env)
        return Spawn(list(command), env, directory)

    return spawn


class PosixBackend(Backend):
    """
    A program on a pty of its own.

    :param cell: how many pixels wide and high one cell is, which goes
        into the size of the pty beside the rows and the columns. It is
        the screen's answer to "CSI 16 t", so whoever holds the screen
        passes it; nothing said means nothing claimed.
    """

    #: What a hot upgrade does with each attribute, as `Process.KEEP`
    #: says. The fds and the pid survive `execve` in the kernel; the
    #: reader is saved for the partial UTF-8 bytes its decoder holds.
    KEEP: ClassVar[dict[str, str]] = {
        "master": "saved",
        "slave": "saved",
        "pid": "saved",
        "_reader": "saved",
        "cell": "rebuilt",
        "_reading": "rebuilt",  # set or not from `Process.suspended`
        "_input_ready_callbacks": "rebuilt",
        "spawn": "dropped",  # the program is running already; `adopt` takes the rest
        "ready_f": "dropped",  # a new reaper sets a new one
    }

    def __init__(self, spawn: Callable[[], Spawn] | None, cell=(0, 0), pty: tuple[int, int] | None = None):
        #: Called in this process when `start` forks. None for an adopted program.
        self.spawn = spawn
        self.cell = cell

        # Create pseudo terminal for this pane, unless `adopt` hands one
        # over. Each end is `None` once it is closed.
        master, slave = pty if pty is not None else os.openpty()
        self.master: int | None = master
        self.slave: int | None = slave

        # Master side -> attached to terminal emulator.
        self._reader = PtyReader(self.master, errors="replace")
        self._input_ready_callbacks = []

        #: Set when the child has been reaped. `Process` waits on it
        #: to fire `done_callback`.
        self.ready_f: anyio.Event = anyio.Event()

        #: Whether the pump hands pages over. Copy mode parks it with
        #: `pause_reading`, and every pause hands it a fresh event, so
        #: a resume that lands between the check and the wait cannot be
        #: lost: the event the pump waits on is the one the resume set.
        self._reading = anyio.Event()
        self._reading.set()

        self.pid: int | None = None

    def add_input_ready_callback(self, callback):
        self._input_ready_callbacks.append(callback)

    @classmethod
    def from_command(
        cls,
        command: list[str],
        environment: Callable[[dict[str, str]], None] | None = None,
        directory: str | None = None,
        cell=(0, 0),
    ):
        """
        A backend that starts `command`, e.g. `['python', '-c', 'print("test")']`.

        :param environment: edits a copy of this process's environment
            into the program's. It runs here, when `start` forks, and
            not in the child.
        :param directory: where the program starts. One that is gone
            leaves the program where the fork was.
        :param cell: the size of one cell in pixels. See `__init__`.
        """
        return cls(spawn_of(command, environment, directory), cell=cell)

    @classmethod
    def adopt(cls, master: int, slave: int | None, pid: int, cell=(0, 0)):
        """
        A backend for a program another owner started: its pty and its
        pid, which survive `execve` in the kernel. `start` forks nothing
        for it, and pumps and reaps it as its own. Lillecarl/pymux#399.

        Raises `OSError` when the fds are not that pty. A number is only
        what the old owner wrote down, and a wrong one would send the
        pane's keystrokes into some other file.
        """
        verify_pty(master, slave)
        backend = cls(None, cell=cell, pty=(master, slave))
        backend.pid = pid
        return backend

    def release(self):
        """
        Give the pty and the child to another owner, and stop serving them.

        The fds stay open and the child is never signalled: the new owner
        holds them. The pump is woken off the fd, so it reads no byte the
        new owner should, and a reaper still waiting finds no pid to kill
        when its scope goes.
        """
        if self.master is not None:
            anyio.notify_closing(self.master)
        self.master = None
        self.slave = None
        self.pid = None

    def pause_reading(self):
        """
        Stop handing the program's output over, without losing any.

        The pump parks on a fresh event, so what the program writes
        waits in the kernel until `resume_reading`. Copy mode is the
        caller, and its promise is that no row of the screen changes
        while a person reads it.
        """
        self._reading = anyio.Event()

    def resume_reading(self):
        """
        Hand the program's output over again.

        Picks up what the program wrote while paused, the end of the
        file included. A resume that lands between the pump's check
        and its wait cannot be lost: the pump waits on this event, and
        this sets the one it holds.
        """
        self._reading.set()

    @property
    def closed(self):
        """
        Is there nothing more to read from this program?

        **Two things say so, and only one of them used to.** A read
        that gives back nothing sets the flag on the reader, and that
        is the end of the pty while the child may still be alive.
        Reaping the child is the other, and `_waitpid` marks it by
        resolving `ready_f`.

        Nothing reads after a reap: `_waitpid` takes the reader away
        and closes the pty. So the reader's flag stayed false for the
        life of the server, and a pane whose program had exited read
        as alive. `Win32Backend` already answers with `ready_f`.
        Lillecarl/pymux#120.
        """
        return self._reader.closed

    def read_text(self, amount=4096):
        "At most a page of what the program drew, decoded."
        return self._reader.read(amount)

    def write_text(self, text):
        # "surrogateescape" carries a byte that is not text. Two things
        # need it. A reply with eight bit controls holds a C1 byte such
        # as 0x9b, which UTF-8 would spell as two bytes and no program
        # would read. And prompt_toolkit's own stdin reader decodes with
        # the same handler, so a byte the user's terminal sent that is
        # not UTF-8 arrives here as a surrogate; without this it raises
        # and the keystroke is lost.
        self.write_bytes(text.encode("utf-8", "surrogateescape"))

    def write_bytes(self, data):
        while self.master is not None:
            try:
                os.write(self.master, data)
            except OSError as e:
                # This happens when the window resizes and a SIGWINCH was received.
                # We get 'Error: [Errno 4] Interrupted system call'
                if e.errno == 4:
                    continue
            return

    def set_size(self, width, height):
        """
        Set terminal size.
        """
        assert isinstance(width, int)
        assert isinstance(height, int)

        if self.master is not None:
            set_terminal_size(self.master, height, width, self.cell)

    async def start(self, task_group: anyio.abc.TaskGroup) -> None:
        """
        Create fork and start the child process, watched by `task_group`.

        The pump reads what the program writes and the reaper waits for
        it to end, both as tasks of the group. Abandoning the scope
        ends them: the pump lets the master side go, and the reaper
        kills a child the scope leaves behind rather than keeping it.
        """
        if self.pid is not None:
            # Adopted: the program runs already.
            task_group.start_soon(self._pump)
            task_group.start_soon(self._reap)
            return

        assert self.spawn is not None
        spawn = self.spawn()

        # CPython warns about a fork in a threaded process. The child
        # here execs at once, and between the fork and the exec it
        # touches only os-level calls; a pty child has no posix_spawn
        # route, because it needs setsid and its own controlling
        # terminal.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()

        if pid == 0:
            assert self.master is not None and self.slave is not None
            run_in_child(spawn, self.master, self.slave)
        elif pid > 0:
            # We wait a very short while, to be sure the child had the time to
            # call _exec. (Otherwise, we are still sharing signal handlers and
            # FDs.) Resizing the pty, when the child is still in our Python
            # code and has the signal handler from prompt_toolkit, but closed
            # the 'fd' for 'call_from_executor', will cause OSError.
            #
            # **No reason was found for a tenth of a second.** Nothing
            # measured it, and a sleep cannot close this race at all:
            # it only makes the window unlikely. Whatever the right fix
            # is, it is a handshake and not a longer number here.
            time.sleep(0.1)

            self.pid = pid

            # The pump reads what the program writes, and the reaper
            # waits for it to end. Both are tasks of the caller's
            # group, so this returns once they are started and the
            # scope owns them.
            task_group.start_soon(self._pump)
            task_group.start_soon(self._reap)

    def kill(self):
        "Terminate process."
        self.send_signal(signal.SIGKILL)

    def send_signal(self, signal):
        "Send signal to running process."
        assert isinstance(signal, int), type(signal)

        if self.pid and not self.closed:
            # [Errno 3] No such process.
            with contextlib.suppress(OSError):
                os.kill(self.pid, signal)

    def close(self):
        """
        Let the pty go, once there is nothing more to read from it.

        The pump calls this when a read says the file has ended. The
        reap cannot: what the kernel still holds for the master side
        is read on the turns of the loop after it, and closing there
        would throw that away. Lillecarl/pymux#121.
        """
        if self.master is not None:
            with contextlib.suppress(OSError):
                os.close(self.master)
            self.master = None

    async def _pump(self) -> None:
        """
        Read what the program writes, until the file ends.

        The pump parks while copy mode has paused the reading, and it
        parks on the descriptor otherwise: `wait_readable` wakes it
        with something to hand over, and the callbacks read it. When
        the master side goes away under it -- a teardown closing what
        the pump is parked on -- the wait raises, and the `close` in
        the `finally` is what keeps that from leaking the descriptor.
        """
        try:
            while self.master is not None and not self._reader.closed:
                await self._reading.wait()
                if self.master is None or self._reader.closed:
                    break
                try:
                    await anyio.wait_readable(self.master)
                except anyio.ClosedResourceError, OSError, ValueError:
                    break
                for callback in self._input_ready_callbacks:
                    await callback()
        finally:
            self.close()

    async def _reap(self) -> None:
        """
        Wait for the child, then mark its end.

        **The slave side goes and the master stays.** A reap used to
        close both at once, so whatever the kernel still held went
        with them: the error a build printed as it died was lost about
        one time in twelve, whenever the loop had not turned since the
        write. Lillecarl/pymux#121.

        Closing the slave is what makes the end of the file reachable.
        While this process holds it open a read of the master answers
        with nothing; with it closed the pump hands over what is left
        and then an `EIO`, which `PtyReader` takes as the end. That
        read is also what makes `is_terminated` true.
        Lillecarl/pymux#120.

        The `finally` covers the scope going away first: a child a
        cancelled scope leaves behind is killed rather than kept, so
        the thread the wait parks on is freed and no program outlives
        the test that started it. Only the cancelled path kills: on
        the ordinary path the child is already reaped, and signalling
        its number then could reach whatever the system gave it to
        next. `send_signal` only signals a child that is still there
        in any case.
        """
        try:
            await anyio.to_thread.run_sync(self._wait_for_child)
        except anyio.get_cancelled_exc_class():
            self.send_signal(signal.SIGKILL)
            raise
        finally:
            if self.slave is not None:
                with contextlib.suppress(OSError):
                    os.close(self.slave)
                self.slave = None

            # **The pump is left as it is.** Resuming it here would
            # read a pane that copy mode has stopped, and copy mode is
            # the promise that no row of the screen changes while a
            # person reads it. A resume picks the last words up, the
            # way it picks up anything else a program wrote while
            # nobody was reading.
            self.ready_f.set()

    def _wait_for_child(self) -> None:
        "Wait for PID in a worker thread."
        # A child the owner before an adoption also waited on may be
        # reaped there first; either way it has ended.
        pid = self.pid
        if pid is None:
            return  # released before this thread ran
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)

    def get_name(self):
        "Return the process name."
        result = "<unknown>"

        # Apparently, on a Linux system (like my Fedora box), I have to call
        # `tcgetpgrp` on the `master` fd. However, on te Window subsystem for
        # Linux, we have to use the `slave` fd.

        if self.master is not None:
            result = get_name_for_fd(self.master)

        if not result and self.slave is not None:
            result = get_name_for_fd(self.slave)

        return result

    def get_cwd(self):
        if self.pid:
            return get_cwd_for_pid(self.pid)


if sys.platform in ("linux", "linux2", "cygwin"):

    def get_name_for_fd(fd):
        """
        Return the process name for a given process ID.

        :param fd: Slave file descriptor. (Often the master fd works as well,
            but apparentsly on WSL only the slave FD works.)
        """
        try:
            pgrp = os.tcgetpgrp(fd)
        except OSError:
            # See: https://github.com/jonathanslenders/pymux/issues/46
            return None

        try:
            with open("/proc/%s/cmdline" % pgrp, "rb") as f:
                return f.read().decode("utf-8", "ignore").partition("\0")[0]
        except OSError:
            pass

elif sys.platform == "darwin":
    from .darwin import get_proc_name

    def get_name_for_fd(fd):
        """
        Return the process name for a given process ID.

        NOTE: on Linux, this seems to require the master FD.
        """
        try:
            pgrp = os.tcgetpgrp(fd)
        except OSError:
            return None

        try:
            return get_proc_name(pgrp)
        except OSError:
            pass

else:

    def get_name_for_fd(fd):
        """
        Return the process name for a given process ID.
        """
        return


#: `_IOR('T', 0x30, unsigned int)`: the index N of `/dev/pts/N` that a
#: pty master belongs to. The `termios` module does not export it.
TIOCGPTN = 0x80045430


def verify_pty(master: int, slave: int | None) -> None:
    """
    Raise `OSError` unless `master` is a pty master and `slave` its slave.

    Every master stats as `/dev/ptmx` (5:2), so two stats cannot pair the
    ends; on Linux the master's TIOCGPTN index has to equal the slave's
    minor number. Measured in `examples/exec_seam_poc.py` of pymux.
    """
    if not stat.S_ISCHR(os.fstat(master).st_mode):
        raise OSError(errno.ENOTTY, "fd %d is not a pty master" % master)
    if slave is None:
        return
    if not os.isatty(slave):
        raise OSError(errno.ENOTTY, "fd %d is not a terminal" % slave)
    if sys.platform.startswith("linux"):
        index = struct.unpack("I", fcntl.ioctl(master, TIOCGPTN, b"\0" * 4))[0]
        if os.minor(os.fstat(slave).st_rdev) != index:
            raise OSError(errno.ENOTTY, "fd %d and fd %d are two ptys" % (master, slave))


def get_cwd_for_pid(pid):
    """
    Return the current working directory for a given process ID.
    """
    if sys.platform in ("linux", "linux2", "cygwin"):
        try:
            return os.readlink("/proc/%s/cwd" % pid)
        except OSError:
            pass
