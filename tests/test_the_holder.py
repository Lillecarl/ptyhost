"""
The holder: programs that outlive the server that started them.

Each test runs a real holder in a process of its own and talks to it
over its socket, the way a server does. Lillecarl/pymux#553.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import socket
import subprocess
import sys
import time

import pytest

from ptyhost.backends.posix_utils import set_terminal_size
from ptyhost.holder import VERSION, HolderClient, HolderError, receive, send
from ptyhost.spawn import Spawn

#: How long a test may wait for a program or the holder, in seconds.
TIMEOUT = 5.0

#: A program that echoes each line it reads, marked, until it reads "exit N".
ECHO = (
    "import sys\n"
    "print('READY', flush=True)\n"
    "for line in sys.stdin:\n"
    "    if line.startswith('exit '):\n"
    "        sys.exit(int(line.split()[1]))\n"
    "    print('<' + line.strip() + '>', flush=True)\n"
)


def until(master: int, text: str) -> str:
    "Read the master until `text` turns up. Everything read, as text."
    seen = b""
    deadline = time.monotonic() + TIMEOUT
    while text.encode() not in seen:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError("waited for %r; the program wrote %r" % (text, seen))
        if select.select([master], [], [], left)[0]:
            try:
                seen += os.read(master, 4096)
            except OSError:
                break
    assert text.encode() in seen, seen
    return seen.decode(errors="replace")


def started(client: HolderClient, program: str, columns: int = 80, rows: int = 24) -> tuple[dict, int]:
    "A python program on a pty this process opened, the way a server starts one."
    master, slave = os.openpty()
    set_terminal_size(master, rows, columns)
    try:
        return client.spawn(Spawn([sys.executable, "-c", program], dict(os.environ)), master, slave), master
    finally:
        os.close(slave)


def echo(client: HolderClient) -> tuple[dict, int]:
    return started(client, ECHO)


def test_a_program_the_holder_starts_talks_to_the_server(holder):
    path, _ = holder
    client = HolderClient.connect(path)
    program, master = echo(client)
    until(master, "READY")

    os.write(master, b"ping\n")

    until(master, "<ping>")
    assert program["status"] is None


def test_the_program_outlives_the_server_that_started_it(holder):
    "A server that crashes loses its copy of the master and nothing else."
    path, _ = holder
    first = HolderClient.connect(path)
    program, master = echo(first)
    until(master, "READY")
    first.close()
    os.close(master)

    second = HolderClient.connect(path)
    (listed,) = second.programs()
    assert listed["id"] == program["id"]
    master = second.master(program["id"])
    os.write(master, b"still here\n")

    until(master, "<still here>")


def test_an_exit_with_no_server_attached_is_kept_for_the_next(holder):
    path, _ = holder
    first = HolderClient.connect(path)
    program, master = echo(first)
    until(master, "READY")
    os.write(master, b"exit 3\n")
    first.close()
    os.close(master)

    second = HolderClient.connect(path)
    deadline = time.monotonic() + TIMEOUT
    while (listed := second.programs())[0]["status"] is None:
        assert time.monotonic() < deadline, listed
        time.sleep(0.01)

    assert listed == [
        {"id": program["id"], "pid": program["pid"], "status": 3, "pipes": False, "ended": True, "adopted": False}
    ]


def test_a_server_hears_of_an_exit(holder):
    path, _ = holder
    client = HolderClient.connect(path)
    program, master = echo(client)
    until(master, "READY")

    os.write(master, b"exit 5\n")

    assert client.next_event() == {
        "event": "exited",
        "id": program["id"],
        "pid": program["pid"],
        "status": 5,
        "pipes": False,
        "ended": True,
        "adopted": False,
    }


def test_a_released_program_is_forgotten(holder):
    path, _ = holder
    client = HolderClient.connect(path)
    program, master = echo(client)
    until(master, "READY")

    client.release(program["id"])

    assert client.programs() == []
    with pytest.raises(HolderError):
        client.master(program["id"])


def test_a_program_starts_with_the_size_it_was_given(holder):
    path, _ = holder
    client = HolderClient.connect(path)
    size = "import os; s = os.get_terminal_size(); print('SIZE %d %d' % (s.columns, s.lines), flush=True); input()"
    _, master = started(client, size, columns=100, rows=30)

    until(master, "SIZE 100 30")


def test_a_spawn_without_its_pty_is_refused(holder):
    path, _ = holder
    client = HolderClient.connect(path)

    with pytest.raises(HolderError, match="master and the slave"):
        client.request({"op": "spawn", "command": ["true"], "environment": {}})


def test_quit_hangs_up_every_program_and_ends_the_holder(holder):
    path, process = holder
    client = HolderClient.connect(path)
    program, master = echo(client)
    until(master, "READY")
    os.close(master)

    client.quit()

    assert process.wait(TIMEOUT) == 0
    deadline = time.monotonic() + TIMEOUT
    while _alive(program["pid"]):
        assert time.monotonic() < deadline, "the program outlived its holder"
        time.sleep(0.01)


def on_pipes(client: HolderClient, program: str, directory: str | None = None) -> tuple[dict, int, int]:
    "A python program with no terminal: the reply, and the read ends of its stdout and stderr."
    reply, fds = client.request(
        {
            "op": "spawn-pipes",
            "command": [sys.executable, "-c", program],
            "environment": dict(os.environ),
            "directory": directory,
        }
    )
    out, err = fds
    return reply, out, err


def all_of(fd: int) -> bytes:
    "Everything a pipe gives until its end."
    seen = b""
    deadline = time.monotonic() + TIMEOUT
    while select.select([fd], [], [], max(0.0, deadline - time.monotonic()))[0]:
        chunk = os.read(fd, 4096)
        if not chunk:
            return seen
        seen += chunk
    raise AssertionError("the pipe never ended; it gave %r" % (seen,))


def test_the_hello_names_what_the_holder_answers(holder):
    "A new op is no new version: a server reads the list. Lillecarl/pymux#556."
    path, _ = holder
    assert "spawn-pipes" in HolderClient.connect(path).ops


def test_a_program_on_pipes_keeps_its_streams_apart(holder):
    "A job's shape: nothing on stdin, stdout and stderr each a pipe. Lillecarl/pymux#556."
    path, _ = holder
    client = HolderClient.connect(path)
    program = (
        "import os, sys; print('out', sys.stdin.read() == '', os.isatty(1)); print('err', file=sys.stderr); sys.exit(4)"
    )
    reply, out, err = on_pipes(client, program)

    assert (all_of(out), all_of(err)) == (b"out True False\n", b"err\n")
    assert client.next_event() == {
        "event": "exited",
        "id": reply["id"],
        "pid": reply["pid"],
        "status": 4,
        "pipes": True,
        "ended": True,
        "adopted": False,
    }


def test_what_a_program_on_pipes_writes_waits_for_the_next_server(holder):
    "The holder keeps the read ends, so nothing is lost between two servers."
    path, _ = holder
    first = HolderClient.connect(path)
    reply, out, err = on_pipes(first, "import time; print('before', flush=True); time.sleep(60)")
    first.close()
    os.close(out)
    os.close(err)

    second = HolderClient.connect(path)
    out, err = second.request({"op": "fds", "id": reply["id"]})[1]
    try:
        assert until(out, "before") == "before\n"
        with pytest.raises(HolderError, match="no terminal"):
            second.master(reply["id"])
    finally:
        second.quit()
        os.close(out)
        os.close(err)


def test_a_program_on_pipes_that_cannot_start_says_why(holder):
    path, _ = holder
    client = HolderClient.connect(path)

    with pytest.raises(HolderError, match="No such file"):
        on_pipes(client, "pass", directory="/no-such-directory")
    assert client.programs() == []


def test_quit_hangs_up_a_program_on_pipes(holder):
    "No terminal hangs it up, so the holder sends the SIGHUP one would."
    path, process = holder
    client = HolderClient.connect(path)
    reply, out, err = on_pipes(client, "import time; print('up', flush=True); time.sleep(60)")
    until(out, "up")
    os.close(out)
    os.close(err)

    client.quit()

    assert process.wait(TIMEOUT) == 0
    deadline = time.monotonic() + TIMEOUT
    while _alive(reply["pid"]):
        assert time.monotonic() < deadline, "the program outlived its holder"
        time.sleep(0.01)


def test_another_version_is_refused(holder):
    path, _ = holder
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT)
    sock.connect(path)
    send(sock, {"op": "hello", "version": VERSION + 1})

    reply, _ = receive(sock)

    assert reply["version"] == VERSION
    assert "error" in reply


def test_a_broken_request_does_not_end_the_holder(holder):
    "One exception that escaped would hang up every program it holds."
    path, process = holder
    client = HolderClient.connect(path)
    with pytest.raises(HolderError):
        client.request({"op": "spawn"})

    assert process.poll() is None
    assert client.programs() == []


def test_a_message_that_is_not_an_object_does_not_end_the_holder(holder):
    "Its `.get` raised inside the handler meant to catch it. Lillecarl/pymux#731."
    path, process = holder
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT)
    sock.connect(path)
    send(sock, ["not", "an", "object"])  # type: ignore[arg-type]
    assert receive(sock) is None
    sock.close()

    client = HolderClient.connect(path)
    assert client.programs() == []
    assert process.poll() is None


def test_an_error_in_a_step_of_the_loop_does_not_end_it(socket_dir, monkeypatch):
    "Only `quit`, the timers or a signal end the loop. Lillecarl/pymux#731."
    from ptyhost.holder import Holder, listen

    holder = Holder(listen(str(socket_dir / "holder.sock")))
    calls = []

    def broken() -> None:
        calls.append(1)
        holder.quitting = True
        raise RuntimeError("a fault in the reaper")

    monkeypatch.setattr(holder, "_reap", broken)
    holder.run()

    assert calls == [1]


def test_an_accept_that_keeps_failing_is_unheard_for_a_while(socket_dir):
    "No fd left leaves the listener readable, and the loop would spin on it."
    import errno
    import selectors

    from ptyhost.holder import Holder, listen

    real = listen(str(socket_dir / "holder.sock"))

    class OutOfFds:
        def fileno(self):
            return real.fileno()

        def setblocking(self, flag):
            real.setblocking(flag)

        def accept(self):
            raise OSError(errno.EMFILE, "Too many open files")

    holder = Holder(OutOfFds())  # type: ignore[arg-type]
    holder._accept()

    assert holder._listen_again_at is not None
    with pytest.raises(KeyError):
        holder.selector.get_key(holder.listener)

    holder._listen_again_at = time.monotonic()
    holder._listen_again()
    assert holder.selector.get_key(holder.listener).events == selectors.EVENT_READ


def test_a_detached_holder_writes_on_after_its_log_rotates(socket_dir):
    "The server rotates the file; the holder's lines went to the old one. Lillecarl/pymux#731."
    path = str(socket_dir / "holder.sock")
    log = socket_dir / "server.log"
    starter = subprocess.Popen(
        [sys.executable, "-m", "ptyhost.holder", "--socket", path, "--detach", "--log", str(log)]
    )
    assert starter.wait(TIMEOUT) == 0
    client = HolderClient.connect(path)
    try:
        log.rename(socket_dir / "server.log.1")
        program, master = started(client, "print('done')")
        os.close(master)
        deadline = time.monotonic() + TIMEOUT
        while not log.exists() or "Program %d is pid" % (program["id"],) not in log.read_text():
            assert time.monotonic() < deadline, "the holder wrote nothing to the new file"
            time.sleep(0.01)
    finally:
        client.quit()
        client.close()


def test_a_new_holder_adopts_a_program_whose_holder_is_gone(socket_dir):
    """
    The server keeps its own master, so the program never notices its
    holder go; a new holder takes it over, says when it ends, with no
    exit code, and forgets it on release. Lillecarl/pymux#730.
    """
    path = str(socket_dir / "holder.sock")
    first = subprocess.Popen([sys.executable, "-m", "ptyhost.holder", "--socket", path])
    try:
        deadline = time.monotonic() + TIMEOUT
        while not os.path.exists(path):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        old = HolderClient.connect(path)
        program, master = echo(old)
        until(master, "READY")
        old.close()
        first.send_signal(signal.SIGTERM)
        first.wait(TIMEOUT)

        os.write(master, b"still here\n")
        until(master, "<still here>")

        second = subprocess.Popen([sys.executable, "-m", "ptyhost.holder", "--socket", path])
        try:
            deadline = time.monotonic() + TIMEOUT
            new = None
            while new is None:
                try:
                    new = HolderClient.connect(path)
                except OSError:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
            reply, _ = new.request({"op": "adopt", "pid": program["pid"], "pipes": False}, [master])
            adopted_id = reply["id"]
            (listed,) = new.programs()
            assert (listed["id"], listed["pid"], listed["adopted"], listed["ended"]) == (
                adopted_id,
                program["pid"],
                True,
                False,
            )

            os.write(master, b"exit 3\n")
            deadline = time.monotonic() + TIMEOUT
            while not new.events:
                assert time.monotonic() < deadline, "the new holder never said it ended"
                new.programs()
                time.sleep(0.05)
            (event,) = new.events
            assert (event["event"], event["id"], event["ended"], event["status"]) == ("exited", adopted_id, True, None)

            new.request({"op": "release", "id": adopted_id})
            assert new.programs() == []
            new.quit()
        finally:
            second.kill()
            second.wait()
    finally:
        first.kill()
        first.wait()
        with contextlib.suppress(OSError):
            os.close(master)


def test_adopt_wants_a_pid_and_its_fds(holder):
    path, process = holder
    client = HolderClient.connect(path)
    with pytest.raises(HolderError):
        client.request({"op": "adopt", "pid": "not a pid", "pipes": False})

    assert process.poll() is None
    assert client.programs() == []


def test_a_second_holder_on_the_same_socket_is_refused(holder):
    path, _ = holder
    second = subprocess.run(
        [sys.executable, "-m", "ptyhost.holder", "--socket", path], capture_output=True, text=True, timeout=TIMEOUT
    )
    assert second.returncode == 1
    assert "already" in second.stderr


def test_an_idle_holder_ends(socket_dir):
    path = str(socket_dir / "holder.sock")
    quickly = "import sys, ptyhost.holder as h; h.IDLE_SECONDS = 0.2; sys.exit(h.main(sys.argv[1:]))"
    process = subprocess.Popen([sys.executable, "-c", quickly, "--socket", path])
    try:
        assert process.wait(TIMEOUT) == 0
        assert not os.path.exists(path)
    finally:
        process.kill()


def test_programs_no_server_comes_back_for_are_hung_up(socket_dir):
    path = str(socket_dir / "holder.sock")
    shortly = "import sys, ptyhost.holder as h; h.ORPHAN_SECONDS = 0.3; sys.exit(h.main(sys.argv[1:]))"
    process = subprocess.Popen([sys.executable, "-c", shortly, "--socket", path])
    try:
        deadline = time.monotonic() + TIMEOUT
        while not os.path.exists(path):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        client = HolderClient.connect(path)
        program, master = echo(client)
        until(master, "READY")
        client.close()
        os.close(master)

        assert process.wait(TIMEOUT) == 0
        deadline = time.monotonic() + TIMEOUT
        while _alive(program["pid"]):
            assert time.monotonic() < deadline, "the program outlived its holder"
            time.sleep(0.01)
    finally:
        process.kill()


def _alive(pid: int) -> bool:
    "Whether `pid` runs, and is not a zombie that its new parent has yet to reap."
    try:
        with open("/proc/%d/stat" % (pid,)) as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def test_a_detached_holder_is_reachable_once_its_starter_returns(socket_dir):
    path = str(socket_dir / "holder.sock")
    started = subprocess.Popen([sys.executable, "-m", "ptyhost.holder", "--socket", path, "--detach"])
    assert started.wait(TIMEOUT) == 0

    client = HolderClient.connect(path)
    reply, _ = client.request({"op": "hello", "version": VERSION})
    client.close()
    os.kill(reply["pid"], signal.SIGTERM)

    assert reply["pid"] != started.pid


def test_a_detached_holder_writes_what_it_does_to_its_log(socket_dir):
    "Its stderr went to /dev/null, and its faults with it. Lillecarl/pymux#558."
    path = str(socket_dir / "holder.sock")
    log = socket_dir / "server.log"
    starter = subprocess.Popen(
        [sys.executable, "-m", "ptyhost.holder", "--socket", path, "--detach", "--log", str(log)]
    )
    assert starter.wait(TIMEOUT) == 0

    client = HolderClient.connect(path)
    try:
        program, master = started(client, "print('done')")
        os.close(master)
        deadline = time.monotonic() + TIMEOUT
        while "ended with" not in log.read_text():
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.01)
    finally:
        client.quit()
        client.close()

    written = log.read_text()
    assert "pymux-holder[" in written
    assert "Holding on %s." % path in written
    assert "Program %d is pid %d" % (program["id"], program["pid"]) in written
    "The small code path stays small. Lillecarl/pymux#553."
    loaded = subprocess.run(
        [sys.executable, "-c", "import sys, ptyhost.holder; print(' '.join(sorted(sys.modules)))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    tops = {name.split(".")[0] for name in loaded}

    assert not tops & {"anyio", "prompt_toolkit", "pyte", "pymux", "ptterm", "txterm", "textual"}
