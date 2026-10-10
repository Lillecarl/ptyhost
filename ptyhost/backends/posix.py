from __future__ import annotations

import contextlib
import errno
import logging
import os
import signal
import sys
import warnings
from collections.abc import Callable
from typing import ClassVar

import anyio
import anyio.abc

from ..spawn import Spawn, exec_pipe, run_in_child, wait_for_exec
from .base import Backend
from .posix_utils import PtyReader, set_terminal_size

logger = logging.getLogger(__name__)

__all__ = ["PosixBackend", "spawn_of"]


def spawn_of(
    command: list[str],
    environment: Callable[[dict[str, str]], None] | None = None,
    directory: str | None = None,
) -> Callable[[], Spawn]:
    """
    What `start` calls for the `Spawn` of `command`, e.g.
    `['python', '-c', 'print("test")']`.

    :param environment: edits a copy of this process's environment into
        the program's. It runs here, when `start` forks, and not in the
        child.
    :param directory: where the program starts. One that is gone leaves
        the program where the fork was.
    """
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
    #: says. Each server gets its own copy of the master from the holder
    #: (`ptyhost.held`), so its number is no other server's; the reader
    #: is saved for the partial UTF-8 bytes its decoder holds.
    KEEP: ClassVar[dict[str, str]] = {
        "master": "rebuilt",
        "slave": "dropped",  # none once the program runs
        "pid": "saved",
        "_reader": "saved",
        "cell": "rebuilt",
        "_reading": "rebuilt",  # set or not from `Process.suspended`
        "_input_ready_callbacks": "rebuilt",
        "spawn": "dropped",  # the program is running already
        "ready_f": "dropped",  # a new reaper sets a new one
    }

    def __init__(self, spawn: Callable[[], Spawn] | None, cell=(0, 0), pty: tuple[int, int | None] | None = None):
        #: Called in this process when `start` forks. None for a program
        #: that runs already.
        self.spawn = spawn
        self.cell = cell

        # A pty of its own, unless the holder handed a master over. Each
        # end is `None` once it is closed.
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
        as alive.
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
        assert self.spawn is not None
        spawn = self.spawn()

        # CPython warns about a fork in a threaded process. The child
        # here execs at once, and between the fork and the exec it
        # touches only os-level calls; a pty child has no posix_spawn
        # route, because it needs setsid and its own controlling
        # terminal.
        #
        # SIGWINCH is blocked across the fork: until the child resets it,
        # the handler it inherited is the application's, and a resize of
        # the terminal the application runs in would run it in the child.
        # `run_in_child` resets it, then unblocks it.
        read_end, exec_end = exec_pipe()
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGWINCH})
        pid = -1
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                pid = os.fork()
        finally:
            if pid != 0:
                signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
                os.close(exec_end)
            if pid < 0:
                os.close(read_end)  # The fork raised.

        if pid == 0:
            os.close(read_end)
            assert self.master is not None and self.slave is not None
            run_in_child(spawn, self.master, self.slave, exec_end)
        elif pid > 0:
            self.pid = pid

            # Running only once it has exec'd: until then the pty's
            # foreground is this fork. Lillecarl/pymux#562. **A blocking
            # read, not an await**: a caller builds a pane around this
            # start, and a yield here lets a program that ends at once be
            # reaped, and its pane removed, before the caller has placed
            # it. An exec takes milliseconds.
            wait_for_exec(read_end)

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
                # A pause that came while it waited holds too: an upgrade
                # pauses here, and a byte read now is lost to the next server.
                if not self._reading.is_set():
                    continue
                for callback in self._input_ready_callbacks:
                    await callback()
        finally:
            self.close()

    async def _reap(self) -> None:
        """
        Wait for the child, then mark its end.

        **The slave side goes and the master stays.** Closing both at
        once drops whatever the kernel still holds: the error a build
        printed as it died was lost about one time in twelve, whenever
        the loop had not turned since the write. Lillecarl/pymux#121.

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
        pid = self.pid
        if pid is None:
            return
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)

    def get_name(self):
        "The name of the program in the foreground of this pty."
        if self.master is None:
            return "<unknown>"
        try:
            pgrp = os.tcgetpgrp(self.master)
        except OSError:
            # See: https://github.com/jonathanslenders/pymux/issues/46
            return None
        return _name_of_pid(pgrp)

    def get_cwd(self):
        if self.pid:
            return get_cwd_for_pid(self.pid)


if sys.platform.startswith("linux"):

    def _name_of_pid(pid: int) -> str | None:
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                return f.read().decode("utf-8", "ignore").partition("\0")[0]
        except OSError:
            return None

elif sys.platform == "darwin":
    from .darwin import get_proc_name

    def _name_of_pid(pid: int) -> str | None:
        try:
            return get_proc_name(pid)
        except OSError:
            return None

else:

    def _name_of_pid(pid: int) -> str | None:
        return None


def verify_pty(master: int, slave: int | None) -> None:
    """
    Raise `OSError` unless `master` is a pty master and `slave` its slave.

    `os.ptsname` names the slave of a master, on Linux and macOS alike,
    and refuses anything else. Every master stats as `/dev/ptmx`, so a
    stat cannot pair the two ends.
    """
    try:
        name = os.ptsname(master)
    except OSError as error:
        raise OSError(errno.ENOTTY, "fd %d is not a pty master" % master) from error
    if slave is None:
        return
    if not os.isatty(slave):
        raise OSError(errno.ENOTTY, "fd %d is not a terminal" % slave)
    if os.ttyname(slave) != name:
        raise OSError(errno.ENOTTY, "fd %d and fd %d are two ptys" % (master, slave))


def get_cwd_for_pid(pid):
    """
    Return the current working directory for a given process ID.
    """
    if sys.platform.startswith("linux"):
        try:
            return os.readlink("/proc/%s/cwd" % pid)
        except OSError:
            pass
