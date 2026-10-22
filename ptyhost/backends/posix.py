from __future__ import annotations

import contextlib
import errno
import logging
import os
import signal
import sys
import time
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

#: How long the name of the foreground program is kept, in seconds, while
#: the foreground process group stays the same.
_NAME_KEPT_FOR = 0.25

#: How long a program sent SIGHUP by `hang_up` has to end, in seconds,
#: before it is sent SIGKILL. A shell writes its history in that time; a
#: program that ignores SIGHUP would otherwise run on with no pane to
#: show it.
GRACE = 5.0

#: How often the end of a program that is not this process's child is
#: looked for, in seconds. No `waitpid` reaches it, so the pid is asked.
STRANGER_SECONDS = 1.0


def reaped_now(pid: int) -> tuple[bool, int | None]:
    """
    Whether this process's child `pid` has ended, reaping it if so, and
    how. A child that is not there -- reaped by an earlier image of this
    process, across an exec -- has ended with no code to tell.
    """
    try:
        found, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True, None
    if found == 0:
        return False, None
    return True, os.waitstatus_to_exitcode(status)


def is_gone(pid: int) -> bool:
    "Whether no process has the pid. One of another user's is there."
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


async def stranger_ended(pid: int, drained: anyio.Event) -> None:
    """
    Wait for a program this process did not fork: until its pid is gone,
    or `drained` says what it wrote has ended. A zombie that its new
    parent leaves unreaped still has its pid, and the end of its output
    covers that.
    """
    while not is_gone(pid):
        with anyio.move_on_after(STRANGER_SECONDS):
            await drained.wait()
        if drained.is_set():
            return


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
        "_unwritten": "dropped",  # keys still on their way when it paused
        "_more_to_write": "rebuilt",
        "_name_kept": "dropped",
        "returncode": "dropped",  # the program is running, so there is none
        "_task_group": "dropped",  # the next server's `start` names its own
        "parent": "rebuilt",  # the snapshot's own column says it
        "_drained": "rebuilt",
    }

    def __init__(self, spawn: Callable[[], Spawn] | None, cell=(0, 0), pty: tuple[int, int | None] | None = None):
        #: Called in this process when `start` forks. None for a program
        #: that runs already.
        self.spawn = spawn
        self.cell = cell

        # A pty of its own, unless the holder handed a master over. Each
        # end is `None` once it is closed.
        master, slave = pty if pty is not None else os.openpty()
        # **Non-blocking, so nothing here waits on the program.** A
        # program that stops reading fills the pty, and a blocking write
        # holds the whole event loop until it reads again. What the pty
        # does not take waits in `_unwritten`. Lillecarl/pymux#563.
        os.set_blocking(master, False)
        self.master: int | None = master
        self.slave: int | None = slave

        #: What the program has not read yet, written as it makes room.
        self._unwritten = bytearray()

        #: The foreground process group, its name, and when it was read.
        self._name_kept: tuple[int, str | None, float] | None = None
        self._more_to_write = anyio.Event()

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
        #: How the program ended, as `subprocess` says it: negative for a
        #: signal. None while it runs, and for a program no `waitpid`
        #: here could reach.
        self.returncode: int | None = None
        #: Whether this process is the program's parent, and so can wait
        #: for it. An upgrade's exec keeps the pid, so a program stays its
        #: child; one taken over from the holder never is.
        self.parent = True
        #: The pump read the end of the file.
        self._drained = anyio.Event()

        #: The group that watches the program, which `hang_up` starts
        #: the SIGKILL that follows its SIGHUP in.
        self._task_group: anyio.abc.TaskGroup | None = None

    @classmethod
    def adopt(cls, master: int, pid: int, parent: bool, cell=(0, 0)) -> PosixBackend:
        """
        A program that runs already, on the pty `master` is the master of:
        one an earlier image of this process forked, across an exec, or one
        another process forked. `start` forks nothing for it. Lillecarl/pymux#736.

        Raises `OSError` when `master` is no pty master. A number is only
        what the snapshot wrote down, and a wrong one would send the
        pane's keystrokes into some other file.
        """
        verify_pty(master, None)
        backend = cls(None, cell=cell, pty=(master, None))
        backend.pid = pid
        backend.parent = parent
        return backend

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

        The reader says so once a read hits the end of the pty, which
        the reap makes reachable by closing the slave (`_reap`). The
        child may still be alive then, and nothing more comes from it.
        Lillecarl/pymux#120.
        """
        return self._reader.closed

    def read_text(self, amount=4096):
        "At most a page of what the program drew, decoded."
        return self._reader.read(amount)

    def read_bytes(self, amount=4096):
        "At most a page of what the program drew, as the pty gave it."
        return self._reader.read_bytes(amount)

    @property
    def more_is_waiting(self) -> bool:
        return self._reader.filled

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
        """
        Give the program `data`, in order. What the pty has no room for
        now waits for `_write_the_rest`. An error is a program that is
        gone, and its pane ends on its own.
        """
        if self.master is None:
            return
        if self._unwritten:
            self._unwritten += data  # Behind what waits already.
            return
        try:
            written = os.write(self.master, data)
        except BlockingIOError:
            written = 0
        except OSError:
            return
        if written < len(data):
            self._unwritten += data[written:]
            self._more_to_write.set()

    async def _write_the_rest(self) -> None:
        "Write what `write_bytes` could not, as the program makes room."
        while self.master is not None:
            await self._more_to_write.wait()
            self._more_to_write = anyio.Event()
            while self._unwritten and self.master is not None:
                try:
                    await anyio.wait_writable(self.master)
                    if self.master is None:
                        return  # The pump closed it while this waited.
                    written = os.write(self.master, self._unwritten)
                except BlockingIOError:
                    continue
                except anyio.ClosedResourceError, OSError, ValueError:
                    self._unwritten.clear()
                    return
                del self._unwritten[:written]

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
            self._task_group = task_group
            task_group.start_soon(self._pump)
            task_group.start_soon(self._write_the_rest)
            task_group.start_soon(self._reap)
            return

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
            self._task_group = task_group
            task_group.start_soon(self._pump)
            task_group.start_soon(self._write_the_rest)
            task_group.start_soon(self._reap)

    def kill(self):
        "Terminate process."
        self.send_signal(signal.SIGKILL)

    def hang_up(self):
        """
        Send the program SIGHUP, and SIGKILL if it is still there
        `GRACE` seconds later.

        **SIGHUP is what closing a terminal says**, and a shell answers
        it by writing its history and hanging up its jobs. SIGKILL gives
        it no chance to: a pane killed that way lost the line that
        killed it from the history. tmux ends a pane's program with
        SIGHUP too.

        The SIGKILL that follows is a task of the group that watches the
        program, so a scope that goes first takes it along, and the
        reaper's own kill covers the program then.
        """
        if self.reaped:
            return
        self.send_signal(signal.SIGHUP)
        if self._task_group is not None:
            self._task_group.start_soon(self._kill_after_grace)

    async def _kill_after_grace(self) -> None:
        "SIGKILL the program unless the reap says it ended within `GRACE`."
        with anyio.move_on_after(GRACE):
            await self.ready_f.wait()
        self.send_signal(signal.SIGKILL)

    @property
    def reaped(self) -> bool:
        """
        Has the program been waited for?

        **A reaped pid is not the program's any more.** The system may
        have given the number to another process, so nothing signals it
        from here on.
        """
        return self.returncode is not None or self.ready_f.is_set()

    def send_signal(self, signal):
        "Send signal to running process."
        assert isinstance(signal, int), type(signal)

        if self.pid and not self.reaped:
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
            # A task parked on the fd -- the writer, waiting for the
            # program to make room -- wakes to `ClosedResourceError`.
            # Closed under it without this, epoll forgets the fd and the
            # task waits for ever. Outside a loop nothing can be parked.
            with contextlib.suppress(RuntimeError):
                anyio.notify_closing(self.master)
            with contextlib.suppress(OSError):
                os.close(self.master)
            self.master = None
        # The writer waits for more; it wakes to find nothing to write to.
        self._unwritten.clear()
        self._more_to_write.set()

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
            self._drained.set()

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

        A program this process did not fork has no `waitpid` here, so it
        ends when its pid goes or its output does, with no exit code.
        """
        try:
            if not self.parent:
                assert self.pid is not None
                await stranger_ended(self.pid, self._drained)
            elif self.spawn is not None or not self._reaped_now():
                # Abandoned on a cancel, so the kill below runs at once and
                # the thread's wait then returns: a job no `stop` kills
                # first held its server's scope open until it ended.
                await anyio.to_thread.run_sync(self._wait_for_child, abandon_on_cancel=True)
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

    def _reaped_now(self) -> bool:
        """
        Reap an adopted child that ended already, without a thread. A
        program can end while an upgrade's exec runs, and is a zombie of
        the new image, or gone with no code when the old image reaped it
        first. Only an adopted one: a fresh child reaped this soon takes
        its pane before the caller of `start` has placed it.
        """
        if self.pid is None:
            return True
        ended, self.returncode = reaped_now(self.pid)
        if ended:
            logger.info("Program %d ended before it was adopted, with %s.", self.pid, self.returncode)
        return ended

    def _wait_for_child(self) -> None:
        "Wait for PID in a worker thread."
        pid = self.pid
        if pid is None:
            return
        with contextlib.suppress(ChildProcessError):
            self.returncode = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])

    def get_name(self):
        """
        The name of the program in the foreground of this pty.

        A title bar asks on every frame. A new program in the foreground
        is a new process group and is read at once; the same group keeps
        its name for `_NAME_KEPT_FOR` seconds, which is how long an
        `exec` inside it may take to show.
        """
        if self.master is None:
            return "<unknown>"
        try:
            pgrp = os.tcgetpgrp(self.master)
        except OSError:
            # See: https://github.com/jonathanslenders/pymux/issues/46
            return None
        now = time.monotonic()
        kept = self._name_kept
        if kept is not None and kept[0] == pgrp and now - kept[2] < _NAME_KEPT_FOR:
            return kept[1]
        name = _name_of_pid(pgrp)
        self._name_kept = (pgrp, name, now)
        return name

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
    elif sys.platform == "darwin":
        from .darwin import get_proc_cwd

        return get_proc_cwd(pid)
