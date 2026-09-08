"""
When the recorder stops.

`ptyhost-record` runs a program on a pty and keeps every byte it wrote.
A program that ends stops it. A program that does not end needs a
deadline, and there are two: `--idle` waits for the program to settle,
and `--timeout` counts from the start.

Both matter for the reason the recorder exists. Claude Code does not
end, and quitting it would draw the screen it gives back over the screen
that was being recorded.
"""

import json
import sys
import time

import pytest

from ptyhost import record

#: The programs are shell scripts and not python ones. `unit` is the
#: suite somebody runs while working, and starting an interpreter nine
#: times costs more than everything these tests wait for.

#: How long a program that "never ends" really waits. It has to outlive
#: the recording, which is under a second here, and nothing more. A wait
#: with no bound is a process left behind when something goes wrong.
NEVER_ENDS = 30

#:
#: A program that draws and then waits forever. `--idle` is what stops
#: a recording of one of these.
FOREVER = "printf %s\nsleep " + str(NEVER_ENDS) + "\n"

#: A program that draws, waits, draws again, and then waits forever.
#: The second write pushes an idle deadline back.
TWICE = "printf one\nsleep %f\nprintf two\nsleep " + str(NEVER_ENDS) + "\n"


def run(tmp_path, source, shell=True, **arguments):
    "Record a program, and return what it wrote and how long it took."
    program = tmp_path / "program"
    program.write_text(source)
    command = ["sh", str(program)] if shell else [sys.executable, str(program)]

    started = time.monotonic()
    record.record(
        "capture",
        command,
        lines=24,
        columns=80,
        term="xterm-256color",
        into=tmp_path,
        **arguments,
    )
    took = time.monotonic() - started
    return (tmp_path / "capture.bin").read_bytes(), took


# ----------------------------------------------------------------------
# A program that ends.


def test_a_program_that_ends_stops_the_recording(tmp_path):
    written, took = run(tmp_path, "printf done\n")
    assert b"done" in written
    assert took < 10


def test_the_files_say_what_size_it_was_made_at(tmp_path):
    "The picture harness reads this and refuses a recording of another size."
    run(tmp_path, "printf x\n")
    beside = json.loads((tmp_path / "capture.reads.json").read_text())
    assert (beside["lines"], beside["columns"]) == (24, 80)


# ----------------------------------------------------------------------
# A program that does not.


def test_idle_stops_a_program_that_never_ends(tmp_path):
    written, took = run(tmp_path, FOREVER % "settled", idle=0.2)
    assert b"settled" in written
    assert took < 10


def test_timeout_stops_a_program_that_never_ends(tmp_path):
    written, took = run(tmp_path, FOREVER % "settled", timeout=0.4)
    assert b"settled" in written
    assert 0.2 < took < 10


def test_something_drawn_pushes_the_idle_deadline_back(tmp_path):
    """
    The point of `--idle`: it waits for the program to settle, and a
    program that is still drawing has not settled.
    """
    written, _took = run(tmp_path, TWICE % 0.3, idle=0.6)
    assert b"one" in written
    assert b"two" in written


def test_the_idle_count_waits_for_the_first_thing_drawn(tmp_path):
    """
    A program takes a while to come up, and one that is still starting
    has not settled. Counting from the start of the run would end a
    recording of a slow program before it drew anything at all, and the
    recording would be empty.
    """
    slow = "sleep 0.4\nprintf late\nsleep %d\n" % NEVER_ENDS
    written, _took = run(tmp_path, slow, idle=0.2)
    assert b"late" in written


def test_the_timeout_wins_when_it_comes_first(tmp_path):
    "It stops whatever is happening, which is what it is for."
    written, took = run(tmp_path, TWICE % 2.0, idle=10.0, timeout=0.4)
    assert b"one" in written
    assert b"two" not in written
    assert took < 10


def test_a_timeout_ends_a_program_that_never_draws(tmp_path):
    "The idle count never starts, so the timeout is the only stop."
    written, took = run(tmp_path, "sleep %d\n" % NEVER_ENDS, idle=0.2, timeout=0.4)
    assert written == b""
    assert 0.2 < took < 10


# ----------------------------------------------------------------------
# What is left behind.


def test_nothing_the_program_writes_on_its_way_out_is_recorded(tmp_path):
    """
    A full screen program gives the screen back as it dies, and those
    bytes would wipe the screen the recording was made for.
    """
    # This one is python: a POSIX shell runs a trap only after the
    # command in front of it returns, and that command is a long sleep.
    source = (
        "import signal, sys, time\n"
        "def bye(*a):\n"
        "    sys.stdout.write('GOODBYE')\n"
        "    sys.stdout.flush()\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGHUP, bye)\n"
        "sys.stdout.write('the screen')\n"
        "sys.stdout.flush()\n"
        "time.sleep(%d)\n" % NEVER_ENDS
    )
    written, _took = run(tmp_path, source, shell=False, idle=0.2)
    assert b"the screen" in written
    assert b"GOODBYE" not in written


def test_the_program_does_not_outlive_the_recording(tmp_path, monkeypatch):
    "A recorder that leaves a process behind is a leak, once per run."
    # The wait is what is being tested, not how long it is. `unit` is
    # the suite somebody runs while working, and five seconds of it
    # would be five seconds of every run.
    monkeypatch.setattr(record, "GRACE", 0.5)
    source = "trap '' HUP\nprintf stubborn\nsleep %d\n" % NEVER_ENDS
    written, took = run(tmp_path, source, idle=0.2)
    assert b"stubborn" in written
    # `stop_the_program` waits `GRACE` and then insists.
    assert took < record.GRACE + 10


# ----------------------------------------------------------------------
# The command line.


@pytest.mark.parametrize("flag", ["--idle", "--timeout"])
def test_a_negative_wait_is_refused(tmp_path, flag, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ptyhost-record", "x", flag, "-1", "--", "true"])
    with pytest.raises(SystemExit) as refused:
        record.main()
    assert refused.value.code != 0
