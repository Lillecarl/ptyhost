"""
Some utilities.
"""

import array
import fcntl
import os
import select
import termios
from codecs import getincrementaldecoder

#: The pixel fields of `struct winsize` are unsigned shorts, and the
#: array that carries them is signed. Anything above this is not
#: reported.
MAX_WINSIZE_PIXELS = 32767

__all__ = (
    "PtyReader",
    "pty_make_controlling_tty",
    "set_terminal_size",
    "nonblocking",
)


class PtyReader:
    """
    The side of a pty that a program writes to, read as text.

    A read gives back what is there and never blocks. It can give back
    an empty string while the pty is still open, because a chunk can
    end in the middle of a character, so `closed` is the only thing
    that says the program has gone.

    This was `prompt_toolkit.input.posix_utils.PosixStdinReader`, which
    reads the keyboard of a person. It is the same arithmetic and a
    different job, and running a program on a pty is not a toolkit's.
    Lillecarl/pymux#85.

    :param fd: the file descriptor to read.
    :param errors: what to do with bytes that are not the encoding.
        "replace" draws the character that says so, which is what a
        terminal does with a program that writes rubbish.
    """

    def __init__(
        self, fd: int, errors: str = "replace", encoding: str = "utf-8"
    ) -> None:
        self.fd = fd
        self.errors = errors

        # A chunk can end in the middle of a character, so the decoder
        # keeps what it cannot finish until the rest arrives.
        self._decoder = getincrementaldecoder(encoding)(errors=errors)

        #: True when there is nothing more to read, ever.
        self.closed = False

    def read(self, count: int = 1024) -> str:
        """
        What the program has written, as text.

        The count is small on purpose. Reading a great deal at once
        gives the event loop one long turn, and everything else waits
        for it.
        """
        if self.closed:
            return ""

        # `os.read` would block, and the caller is a callback that the
        # loop fires when the descriptor is ready. It still happens
        # that nothing is there, so this asks first.
        try:
            if not select.select([self.fd], [], [], 0)[0]:
                return ""
        except OSError:
            # The descriptor was closed. It became ready, a callback
            # was scheduled, and another callback closed it before this
            # one ran.
            self.closed = True

        try:
            data = os.read(self.fd, count)
            if data == b"":
                self.closed = True
                return ""
        except InterruptedError:
            # SIGWINCH interrupts the read.
            data = b""
        except OSError:
            # **This is the end of the file on a pty.** A master whose
            # slave side is all closed answers `EIO` rather than with
            # zero bytes, so the test above never fires for one, and
            # `closed` stayed false for the life of the server. A pane
            # whose program had exited read as alive.
            # Lillecarl/pymux#120.
            self.closed = True
            return ""

        return self._decoder.decode(data)


def pty_make_controlling_tty(tty_fd):
    """
    This makes the pseudo-terminal the controlling tty. This should be
    more portable than the pty.fork() function. Specifically, this should
    work on Solaris.

    Thanks to pexpect:
    http://pexpect.sourceforge.net/pexpect.html
    """
    child_name = os.ttyname(tty_fd)

    # Disconnect from controlling tty. Harmless if not already connected.
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        if fd >= 0:
            os.close(fd)
    # which exception, shouldnt' we catch explicitly .. ?
    except:
        # Already disconnected. This happens if running inside cron.
        pass

    os.setsid()

    # Verify we are disconnected from controlling tty
    # by attempting to open it again.
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        if fd >= 0:
            os.close(fd)
            raise Exception(
                "Failed to disconnect from controlling "
                "tty. It is still possible to open /dev/tty."
            )
    # which exception, shouldnt' we catch explicitly .. ?
    except:
        # Good! We are disconnected from a controlling tty.
        pass

    # Verify we can open child pty.
    fd = os.open(child_name, os.O_RDWR)
    if fd < 0:
        raise Exception("Could not open child pty, " + child_name)
    else:
        os.close(fd)

    # Verify we now have a controlling tty.
    if os.name != "posix":
        # Skip this on BSD-like systems since it will break.
        fd = os.open("/dev/tty", os.O_WRONLY)
        if fd < 0:
            raise Exception("Could not open controlling tty, /dev/tty")
        else:
            os.close(fd)


def set_terminal_size(stdout_fileno, rows, cols, cell=(0, 0)):
    """
    Set terminal size.

    (This is also mainly for internal use. Setting the terminal size
    automatically happens when the window resizes. However, sometimes the
    process that created a pseudo terminal, and the process that's attached to
    the output window are not the same, e.g. in case of a telnet connection, or
    unix domain socket, and then we have to sync the sizes by hand.)

    `cell` is how many pixels wide and high one cell is, and the size in
    pixels goes into the same structure. A program that draws images
    reads it, and a zero there says "I do not know", which leaves the
    program with no size to draw.

    **The number is not this layer's.** How big a cell is, is what the
    screen answers to "CSI 14 t" and "CSI 16 t", so whoever holds the
    screen passes it and the two answers agree. Nothing said means
    nothing claimed.
    """
    cell_width, cell_height = cell

    # Buffer for the C call
    # (The first parameter of 'array.array' needs to be 'str' on both Python 2
    # and Python 3.)
    buf = array.array(
        "h",
        [
            rows,
            cols,
            _pixels(cols, cell_width),
            _pixels(rows, cell_height),
        ],
    )

    # Do: TIOCSWINSZ (Set)
    fcntl.ioctl(stdout_fileno, termios.TIOCSWINSZ, buf)


def _pixels(cells, cell_size):
    """
    The size of a number of cells in pixels, in the range that the C
    structure holds. A terminal too wide to count says nothing rather
    than a wrong number.
    """
    if cells <= 0:
        return 0
    size = cells * cell_size
    return size if size <= MAX_WINSIZE_PIXELS else 0


class nonblocking:
    """
    Make fd non blocking.
    """

    def __init__(self, fd):
        self.fd = fd

    def __enter__(self):
        self.orig_fl = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, self.orig_fl | os.O_NONBLOCK)

    def __exit__(self, *args):
        fcntl.fcntl(self.fd, fcntl.F_SETFL, self.orig_fl)
