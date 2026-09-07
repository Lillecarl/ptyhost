import os
import resource
import signal
import sys
import time
import traceback
from asyncio import Future, get_event_loop

from .base import Backend
from .posix_utils import PtyReader, pty_make_controlling_tty, set_terminal_size

__all__ = ["PosixBackend"]


class PosixBackend(Backend):
    """
    A program on a pty of its own.

    :param cell: how many pixels wide and high one cell is, which goes
        into the size of the pty beside the rows and the columns. It is
        the screen's answer to "CSI 16 t", so whoever holds the screen
        passes it; nothing said means nothing claimed.
    """

    def __init__(self, exec_func, cell=(0, 0)):
        self.exec_func = exec_func
        self.cell = cell

        # Create pseudo terminal for this pane.
        self.master, self.slave = os.openpty()

        # Master side -> attached to terminal emulator.
        self._reader = PtyReader(self.master, errors="replace")
        self._reader_connected = False
        self._input_ready_callbacks = []

        self.ready_f = Future()
        self.loop = get_event_loop()
        self.pid = None

    def add_input_ready_callback(self, callback):
        self._input_ready_callbacks.append(callback)

    @classmethod
    def from_command(cls, command, before_exec_func=None, cell=(0, 0)):
        """
        Create Process from command,
        e.g. command=['python', '-c', 'print("test")']

        :param before_exec_func: Function that is called before `exec` in the
            process fork.
        :param cell: the size of one cell in pixels. See `__init__`.
        """
        assert isinstance(command, list)
        assert before_exec_func is None or callable(before_exec_func)

        def execv():
            if before_exec_func:
                before_exec_func()

            for p in os.environ["PATH"].split(":"):
                path = os.path.join(p, command[0])
                if os.path.exists(path) and os.access(path, os.X_OK):
                    os.execv(path, command)

        return cls(execv, cell=cell)

    def connect_reader(self):
        if self.master is not None and not self._reader_connected:

            def ready():
                for cb in self._input_ready_callbacks:
                    cb()

            self.loop.add_reader(self.master, ready)
            self._reader_connected = True

    @property
    def closed(self):
        return self._reader.closed

    def disconnect_reader(self):
        if self.master is not None and self._reader_connected:
            self.loop.remove_reader(self.master)
            self._reader_connected = False

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

    def start(self):
        """
        Create fork and start the child process.
        """
        pid = os.fork()

        if pid == 0:
            self._in_child()
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

            # Wait for the process to finish.
            self._waitpid()

    def kill(self):
        "Terminate process."
        self.send_signal(signal.SIGKILL)

    def send_signal(self, signal):
        "Send signal to running process."
        assert isinstance(signal, int), type(signal)

        if self.pid and not self.closed:
            try:
                os.kill(self.pid, signal)
            except OSError:
                pass  # [Errno 3] No such process.

    def _in_child(self):
        "Will be executed in the forked child."
        os.close(self.master)

        # Remove signal handler for SIGWINCH as early as possible.
        # (We don't want this to be triggered when execv has not been called
        # yet.)
        signal.signal(signal.SIGWINCH, 0)

        pty_make_controlling_tty(self.slave)

        # In the fork, set the stdin/out/err to our slave pty.
        os.dup2(self.slave, 0)
        os.dup2(self.slave, 1)
        os.dup2(self.slave, 2)

        # Execute in child.
        try:
            self._close_file_descriptors()
            self.exec_func()
        except Exception:
            traceback.print_exc()

            # The traceback went to the pty, which is the pane. Exiting
            # now would close the pane with it and the person would see
            # nothing. Five seconds is long enough to read that
            # something went wrong and to copy the first line of it.
            time.sleep(5)

            os._exit(1)
        os._exit(0)

    def _close_file_descriptors(self):
        # Do not allow child to inherit open file descriptors from parent.
        # (In case that we keep running Python code. We shouldn't close them.
        # because the garbage collector is still active, and he will close them
        # eventually.)
        max_fd = resource.getrlimit(resource.RLIMIT_NOFILE)[-1]

        try:
            os.closerange(3, max_fd)
        except OverflowError:
            # On OS X, max_fd can return very big values, than closerange
            # doesn't understand, e.g. 9223372036854775807. In this case, just
            # use 4096. This is what Linux systems report, and should be
            # sufficient. (I hope...)
            os.closerange(3, 4096)

    def _waitpid(self):
        """
        Create an executor that waits and handles process termination.
        """

        def wait_for_finished():
            "Wait for PID in executor."
            os.waitpid(self.pid, 0)
            self.loop.call_soon(done)

        def done():
            "PID received. Back in the main thread."
            # Close pty and remove reader.

            self.disconnect_reader()
            os.close(self.master)
            os.close(self.slave)

            self.master = None

            # Callback.
            self.ready_f.set_result(None)

        self.loop.run_in_executor(None, wait_for_finished)

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
            return

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
            return

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


def get_cwd_for_pid(pid):
    """
    Return the current working directory for a given process ID.
    """
    if sys.platform in ("linux", "linux2", "cygwin"):
        try:
            return os.readlink("/proc/%s/cwd" % pid)
        except OSError:
            pass
