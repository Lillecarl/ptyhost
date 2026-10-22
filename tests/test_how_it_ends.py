"""
A process that a signal ends says which, and dies of it all the same.
Lillecarl/pymux#728.
"""

from __future__ import annotations

import signal
import subprocess
import sys

import pytest

#: Says it is ready, then waits to be ended.
WAITS = (
    "import logging, signal, sys, time\n"
    "from ptyhost.ending import say_how_it_ends\n"
    "logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(message)s')\n"
    "{before}"
    "say_how_it_ends(logging.getLogger('it'))\n"
    "print('ready', flush=True)\n"
    "time.sleep(30)\n"
)


def ended_by(number: int, before: str = "") -> subprocess.CompletedProcess:
    child = subprocess.Popen(
        [sys.executable, "-c", WAITS.format(before=before)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline() == "ready\n"
    child.send_signal(number)
    out, err = child.communicate(timeout=10)
    return subprocess.CompletedProcess(child.args, child.returncode, out, err)


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP], ids=["SIGTERM", "SIGHUP"])
def test_it_says_the_signal_and_dies_of_it(number):
    done = ended_by(number)

    assert "Ended by %s." % (signal.Signals(number).name,) in done.stderr
    # What waits for it sees the same end as without the line.
    assert done.returncode == -number


def test_a_holder_says_it(socket_dir):
    "The holder took the panes of the server of 2026-10-22 with it and said nothing."
    path = str(socket_dir / "holder.sock")
    child = subprocess.Popen(
        [sys.executable, "-m", "ptyhost.holder", "--socket", path],
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stderr is not None
    # The line after the handler, not the socket: that is bound first.
    assert "Holding on" in child.stderr.readline()
    child.send_signal(signal.SIGTERM)
    _, err = child.communicate(timeout=10)

    assert "Ended by SIGTERM." in err
    assert child.returncode == -signal.SIGTERM


def test_a_signal_it_handles_itself_keeps_its_handler():
    done = ended_by(signal.SIGTERM, before="signal.signal(signal.SIGTERM, lambda *a: sys.exit(7))\n")

    assert "Ended by" not in done.stderr
    assert done.returncode == 7
