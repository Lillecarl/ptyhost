"""
Record what a real program writes to a terminal.

    ptyhost-record htop -- htop
    ptyhost-record vim --lines 24 --columns 80 -- vim README.md
    ptyhost-record claude --idle 3 -- claude

This is a program and not a test. A fault that only a real program
shows, in a real session, on the machine of the person who hit it, can
only get into a test by being recorded there first. So the recorder
ships here, where the pty is, runs anywhere, and the files it writes
are what a check replays later.

`--into DIR` says where the files go, and the current directory is the
default. `ptterm/tests/corpus` is where the comparisons of ptterm read
them from, and `pymux/tests/recordings` makes the recording a fixture
of the picture harness.

The program runs on a pty of its own and everything passes through, so
the session looks normal. Use it, reproduce the fault, and quit. Three
files come out.

**A program that does not end needs `--idle`.** Quitting is not always
possible, and it is often not what you want: a full screen program gives
the screen back as it dies, and those bytes wipe the very screen the
recording was made for. So drive the program to the screen you want,
let it settle, and the recording stops there.

The idle count starts at the first thing the program draws, not at the
start, so a program that takes a while to come up is not cut off before
it has drawn anything. `--timeout SECONDS` stops after that long
whatever is happening, which is what covers a program that draws
nothing at all. The two work together.

Nothing the program writes after that point is recorded. The recording
ends at the screen you were looking at.

The three files:

- `<name>.bin`, what the program wrote. This is the corpus that the
  comparisons replay.
- `<name>.reads.json`, the size of every read. A pty hands over what it
  has when it has it, so a sequence reaches the terminal in one read or
  in three, and the screen has to be the same either way.
- `<name>.session.json`, both directions with a time on each piece.
  This is where a query and its answer sit next to each other: the
  program asks what the terminal can do, and what it draws afterwards
  depends on the answer. Reading a capture without it is guessing.

**A recording belongs to the terminal it was made on.** A program asks
what the terminal can do and draws what the answers allow, so the
bytes carry the answers of that terminal with them. `TERM` therefore
defaults to a plain one. Record under something exotic only when the
capture is meant to hold that.

**A recording holds whatever was on the screen.** A path, a branch, a
piece of the work in front of you. Read one before it goes into a
repository.
"""
import argparse
import base64
import fcntl
import io
import json
import os
import pathlib
import pty
import select
import shutil
import signal
import struct
import sys
import termios
import time
import tty


#: How long to give a program to end after the recording stops, before
#: insisting. It is being asked to go away, not to save anything.
GRACE = 5.0


def _descriptor_of(stream) -> int | None:
    """
    The file descriptor behind a stream, or None when it has none.

    A stream that a test or a pipeline put there is not always a file.
    Nothing here needs one badly enough to fail over it: without a
    keyboard the program gets no keys, and without a screen what it
    draws is recorded and not shown.
    """
    try:
        return stream.fileno()
    except (AttributeError, ValueError, io.UnsupportedOperation):
        return None


def set_size(fd: int, lines: int, columns: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))


def stop_the_program(child: int, master: int) -> None:
    """
    End the program, and wait for it.

    Closing the master is what a person closing a terminal window does,
    and a program on a pty reads it as the end of its input. Most end
    there. SIGHUP says the same thing to one that does not, and SIGKILL
    is for one that ignores both.
    """
    os.close(master)
    try:
        os.kill(child, signal.SIGHUP)
    except (ProcessLookupError, PermissionError):
        pass

    deadline = time.monotonic() + GRACE
    while time.monotonic() < deadline:
        try:
            finished, _ = os.waitpid(child, os.WNOHANG)
        except ChildProcessError:
            return
        if finished:
            return
        time.sleep(0.05)

    try:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
    except (ProcessLookupError, ChildProcessError):
        pass


def record(
    name: str,
    command,
    lines: int,
    columns: int,
    term: str,
    into: pathlib.Path | None = None,
    timeout: float = 0.0,
    idle: float = 0.0,
) -> None:
    directory = into or pathlib.Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / ("%s.bin" % name)
    reads = directory / ("%s.reads.json" % name)
    session = directory / ("%s.session.json" % name)

    child, master = pty.fork()
    if child == 0:
        os.environ["TERM"] = term
        os.environ["LINES"] = str(lines)
        os.environ["COLUMNS"] = str(columns)
        # A capture is worth nothing when the colours come from a
        # theme that nobody else has.
        os.environ.pop("COLORTERM", None)
        # A pager waits for a key that a recording of a program has
        # nobody to press.
        os.environ["PAGER"] = "cat"
        os.environ["GIT_PAGER"] = "cat"
        os.execvp(command[0], command)

    set_size(master, lines, columns)

    written = []
    sizes = []
    events = []
    started = time.monotonic()
    saved = None
    # A recorder driven by a script has no keyboard and no screen. The
    # deadlines are what stop such a run, and it is the shape a test or
    # an automation uses, so neither may be assumed to be there.
    keyboard = _descriptor_of(sys.stdin)
    screen = _descriptor_of(sys.stdout)

    watching = [master]
    if keyboard is not None:
        watching.append(keyboard)
        if sys.stdin.isatty():
            saved = termios.tcgetattr(keyboard)
            tty.setraw(keyboard)

    # When to stop, whatever the program is doing. `timeout` counts from
    # the start.
    #
    # `idle` counts from the last thing the program drew, and it does
    # not start until the program has drawn something. A program takes a
    # while to come up, and one that is still starting has not settled;
    # counting from the start would end the recording before the program
    # ever wrote a byte. `timeout` is what covers a program that draws
    # nothing at all.
    ends_at = started + timeout if timeout else None
    settles_at = None
    reason = "the program ended"

    try:
        while True:
            now = time.monotonic()
            deadlines = [when for when in (ends_at, settles_at) if when is not None]
            if deadlines and now >= min(deadlines):
                reason = (
                    "it drew nothing for %gs" % idle
                    if settles_at is not None and now >= settles_at
                    else "the %gs were up" % timeout
                )
                break

            # A deadline is only reached while nothing happens, so the
            # wait has to end at the nearest one. Without a deadline the
            # wait is a plain block.
            wait = min(when - now for when in deadlines) if deadlines else None

            try:
                readable, _, _ = select.select(watching, [], [], wait)
            except InterruptedError:
                continue

            if keyboard is not None and keyboard in readable:
                # A page. What the keyboard gives is keystrokes and a
                # paste, so a read is nearly always far under this.
                keys = os.read(keyboard, 4096)
                if keys:
                    os.write(master, keys)
                    events.append(
                        [
                            round(time.monotonic() - started, 6),
                            "in",
                            base64.b64encode(keys).decode("ascii"),
                        ]
                    )
                else:
                    # The end of the keyboard. Watching it further
                    # would spin, because select calls it readable and
                    # every read gives nothing.
                    watching.remove(keyboard)

            if master in readable:
                try:
                    # Sixteen pages, and not the one that the keyboard
                    # reads. This side carries what the program draws,
                    # which arrives in bursts: a full screen of colour
                    # is tens of kilobytes, and a read that stops short
                    # of a burst costs another trip round `select`.
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                written.append(data)
                sizes.append(len(data))
                events.append(
                    [
                        round(time.monotonic() - started, 6),
                        "out",
                        base64.b64encode(data).decode("ascii"),
                    ]
                )
                if screen is not None:
                    os.write(screen, data)
                if idle:
                    settles_at = time.monotonic() + idle
    finally:
        if saved is not None and keyboard is not None:
            termios.tcsetattr(keyboard, termios.TCSAFLUSH, saved)
        # Nothing the program writes on its way out belongs in the
        # recording. A full screen program gives the screen back as it
        # dies, and those bytes would wipe the very screen this was
        # recorded for.
        stop_the_program(child, master)

    output.write_bytes(b"".join(written))
    reads.write_text(json.dumps({"lines": lines, "columns": columns, "sizes": sizes}))
    session.write_text(
        json.dumps(
            {"lines": lines, "columns": columns, "term": term, "events": events}
        )
    )
    keys = sum(1 for event in events if event[1] == "in")
    print(
        "\r\n%s: %d bytes in %d reads, %d from the keyboard (%s)"
        % (output, sum(sizes), len(sizes), keys, reason),
        file=sys.stderr,
    )


def main() -> None:
    # A console script gets no handler from the block at the end of
    # this file, and a recorder that ignores Ctrl-C is a recorder
    # nobody can stop.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("name", help="the name of the capture")
    parser.add_argument("--lines", type=int, default=24)
    parser.add_argument("--columns", type=int, default=80)
    parser.add_argument(
        "--term",
        default="xterm-256color",
        help="what the program is told the terminal is",
    )
    parser.add_argument(
        "--into",
        type=pathlib.Path,
        default=None,
        metavar="DIR",
        help="where the files go. The current directory by default.",
    )
    parser.add_argument(
        "--idle",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="stop when the program has drawn nothing for this long. "
        "This is the one to use for a program that does not end: drive "
        "it to the screen you want, and the recording stops when it "
        "settles there. The count starts at the first thing the program "
        "draws, so a slow start does not end the recording.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="stop this long after the start, whatever is happening.",
    )
    parser.add_argument("command", nargs="+", help="the program to run")
    arguments = parser.parse_args()

    command = arguments.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no program to run")
    if shutil.which(command[0]) is None:
        parser.error("%s is not on the path" % command[0])

    if arguments.idle < 0 or arguments.timeout < 0:
        parser.error("a time to wait cannot be negative")

    record(
        arguments.name,
        command,
        arguments.lines,
        arguments.columns,
        arguments.term,
        arguments.into,
        arguments.timeout,
        arguments.idle,
    )


if __name__ == "__main__":
    main()
