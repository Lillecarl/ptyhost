"""
The size that a pty reports.

A program that draws images asks the pty how big the terminal is in
pixels. A zero there says "I do not know", and leaves the program with
no size to draw with.

**The number is not this package's.** How big a cell is, is what a
screen answers to "CSI 16 t", so whoever holds the screen passes it
down. The cell here is a number for the test to count with, and not a
claim about anything.
"""
import array
import fcntl
import os
import pty
import termios

import pytest

from ptyhost.backends.posix_utils import MAX_WINSIZE_PIXELS, set_terminal_size

CELL_WIDTH, CELL_HEIGHT = 10, 20
CELL = (CELL_WIDTH, CELL_HEIGHT)


@pytest.fixture
def pty_pair():
    master, slave = pty.openpty()
    yield master, slave
    os.close(master)
    os.close(slave)


def read_size(fileno):
    "The (rows, columns, width, height) that the pty reports."
    buf = array.array("h", [0, 0, 0, 0])
    fcntl.ioctl(fileno, termios.TIOCGWINSZ, buf, True)
    return tuple(buf)


def test_the_size_in_pixels_is_reported(pty_pair):
    master, slave = pty_pair
    set_terminal_size(master, 24, 80, CELL)
    assert read_size(slave) == (24, 80, 80 * CELL_WIDTH, 24 * CELL_HEIGHT)


def test_a_terminal_too_large_to_count_reports_no_pixels(pty_pair):
    "A wrong number is worse than none."
    master, slave = pty_pair
    columns = MAX_WINSIZE_PIXELS // CELL_WIDTH + 1
    set_terminal_size(master, 24, columns, CELL)
    _rows, _columns, width, height = read_size(slave)
    assert width == 0
    assert height == 24 * CELL_HEIGHT


def test_an_empty_terminal_reports_no_pixels(pty_pair):
    master, slave = pty_pair
    set_terminal_size(master, 0, 0, CELL)
    assert read_size(slave) == (0, 0, 0, 0)


def test_a_caller_that_says_nothing_claims_nothing(pty_pair):
    """
    The default. A pty whose pixel fields are zero tells a program that
    the size of a cell is unknown, which is the honest answer when
    nobody has said.
    """
    master, slave = pty_pair
    set_terminal_size(master, 24, 80)
    assert read_size(slave) == (24, 80, 0, 0)
