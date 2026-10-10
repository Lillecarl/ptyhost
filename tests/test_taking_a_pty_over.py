"""
What a server checks and keeps when it takes a pty over from the holder.

A number is only what the holder handed over, and a wrong one would send
a pane's keystrokes into some other file. Lillecarl/pymux#399,
Lillecarl/pymux#553.
"""

from __future__ import annotations

import os

import pytest

from ptyhost.backends.posix import verify_pty
from ptyhost.backends.posix_utils import PtyReader


def test_only_the_two_ends_of_one_pty_pass():
    first, second = os.openpty(), os.openpty()
    read, write = os.pipe()
    try:
        verify_pty(first[0], first[1])
        verify_pty(first[0], None)
        for master, slave in ((first[0], second[1]), (read, first[1]), (first[0], write), (first[1], first[1])):
            with pytest.raises(OSError):
                verify_pty(master, slave)
    finally:
        for fd in (*first, *second, read, write):
            os.close(fd)


def test_a_character_cut_in_two_survives_a_new_reader():
    read, write = os.pipe()
    try:
        first = PtyReader(read)
        os.write(write, "é".encode()[:1])
        assert first.read() == ""

        second = PtyReader(read)
        second.decoder_state = first.decoder_state
        os.write(write, "é".encode()[1:])
        assert second.read() == "é"
    finally:
        os.close(read)
        os.close(write)
