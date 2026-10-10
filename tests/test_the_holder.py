"""
The holder: programs that outlive the server that started them.

Each test runs a real holder in a process of its own and talks to it
over its socket, the way a server does. Lillecarl/pymux#553.
"""

from __future__ import annotations

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

    assert listed == [{"id": program["id"], "pid": program["pid"], "status": 3}]


def test_a_server_hears_of_an_exit(holder):
    path, _ = holder
    client = HolderClient.connect(path)
    program, master = echo(client)
    until(master, "READY")

    os.write(master, b"exit 5\n")

    assert client.next_event() == {"event": "exited", "id": program["id"], "pid": program["pid"], "status": 5}


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


def test_the_holder_imports_no_toolkit_and_no_anyio():
    "The small code path stays small. Lillecarl/pymux#553."
    loaded = subprocess.run(
        [sys.executable, "-c", "import sys, ptyhost.holder; print(' '.join(sorted(sys.modules)))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    tops = {name.split(".")[0] for name in loaded}

    assert not tops & {"anyio", "prompt_toolkit", "pyte", "pymux", "ptterm", "txterm", "textual"}
