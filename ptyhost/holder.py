"""
Hold the ptys of one server, so that its programs outlive it.

The holder forks every program, keeps the master side of each pty open
and reaps each child. A server asks for a master over a unix socket and
gets a copy of the fd. The bytes go between the server and the pty
directly, never through here, and nothing here parses them. A server
that crashes or upgrades loses its copies and nothing else: the next
one asks again. Lillecarl/pymux#553.

Standard library only, one thread, one selector. This changes rarely,
and must never need the code that changes often.

**Everything is imported at start.** A holder outlives the build it came
from, and `nix store gc` roots what a process has mapped, not a module
it has not imported yet.

The wire: each message is a 4 byte big-endian length, then that much
UTF-8 JSON. The fds a message carries ride on its first bytes. A reply
holds `error` when the holder refused. An `event` can arrive between a
request and its reply.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import selectors
import signal
import socket
import struct
import sys
import time
from collections.abc import Sequence
from typing import Any

from .spawn import Spawn, run_in_child

__all__ = ("VERSION", "HolderClient", "HolderError", "main", "receive", "send", "spawn_request")

#: Bumped when a message changes. A newer server meets an older holder
#: after every upgrade, so the first message says which one it speaks.
VERSION = 1

#: How long a holder with no program and no server waits before it exits.
IDLE_SECONDS = 5.0

#: How long programs wait for a server when none is connected. A server
#: that upgrades or restarts after a crash comes back well within it; a
#: crash that nothing restarts ends the programs instead of leaving them
#: running with no way to reach them. Then the holder hangs up every pty
#: and exits.
ORPHAN_SECONDS = 30.0

#: How long one message may take. A server that stops reading in the
#: middle of one is dropped rather than left to stall every other.
MESSAGE_SECONDS = 5.0

#: A reply carries the master of one program and never a list of them:
#: Linux takes at most 253 fds in one message (SCM_MAX_FD). The room
#: past one catches a peer that sends more, so that they get closed.
MAX_FDS = 4

MAX_MESSAGE = 1 << 20

_HEADER = struct.Struct(">I")


class HolderError(Exception):
    "The holder refused, or the conversation with it broke."


def send(sock: socket.socket, message: dict[str, Any], fds: Sequence[int] = ()) -> None:
    body = json.dumps(message).encode("utf-8")
    data = _HEADER.pack(len(body)) + body
    sent = socket.send_fds(sock, [data], list(fds)) if fds else sock.send(data)
    sock.sendall(data[sent:])


def receive(sock: socket.socket) -> tuple[dict[str, Any], list[int]] | None:
    "One message and the fds it carried, or None when the peer closed between two."
    header = b""
    fds: list[int] = []
    while len(header) < _HEADER.size:
        data, got, _flags, _address = socket.recv_fds(sock, _HEADER.size - len(header), MAX_FDS)
        # `recv_fds` leaves them inheritable, and every fork after would
        # carry a pty into a program that has no business with it.
        for fd in got:
            os.set_inheritable(fd, False)
        fds.extend(got)
        if not data:
            _close_all(fds)
            if header:
                raise HolderError("the peer closed in the middle of a message")
            return None
        header += data
    (length,) = _HEADER.unpack(header)
    if length > MAX_MESSAGE:
        _close_all(fds)
        raise HolderError("a message of %d bytes" % (length,))
    body = b""
    while len(body) < length:
        data = sock.recv(length - len(body))
        if not data:
            _close_all(fds)
            raise HolderError("the peer closed in the middle of a message")
        body += data
    return json.loads(body), fds


def spawn_request(spawn: Spawn) -> dict[str, Any]:
    "The message that starts `spawn`. The pty rides along as `[master, slave]`."
    return {"op": "spawn", "command": spawn.command, "environment": spawn.environment, "directory": spawn.directory}


def _close_all(fds: Sequence[int]) -> None:
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


class _Program:
    __slots__ = ("master", "pid", "program_id", "released", "status")

    def __init__(self, program_id: int, pid: int, master: int) -> None:
        self.program_id = program_id
        self.pid = pid
        self.master: int | None = master
        #: The exit code once reaped, as `os.waitstatus_to_exitcode` says it.
        self.status: int | None = None
        #: A server let it go. It stays until the child is reaped.
        self.released = False

    def describe(self) -> dict[str, Any]:
        return {"id": self.program_id, "pid": self.pid, "status": self.status}


class Holder:
    "The loop. `run` returns once it has been idle for `IDLE_SECONDS`."

    def __init__(self, listener: socket.socket) -> None:
        self.listener = listener
        self.listener.setblocking(False)
        self.selector = selectors.DefaultSelector()
        self.programs: dict[int, _Program] = {}
        #: Each connected server, and whether it said hello.
        self.servers: dict[socket.socket, bool] = {}
        #: A server asked to end it all: the server itself is ending.
        self.quitting = False
        self._next_id = 1

        # A SIGCHLD wakes the selector through this pair.
        self._wake, self._woken = socket.socketpair()
        self._wake.setblocking(False)
        self._woken.setblocking(False)
        signal.set_wakeup_fd(self._wake.fileno())
        signal.signal(signal.SIGCHLD, lambda *_: None)

        self.selector.register(self.listener, selectors.EVENT_READ)
        self.selector.register(self._woken, selectors.EVENT_READ)

    def run(self) -> None:
        idle_since = alone_since = time.monotonic()
        while True:
            for key, _ in self.selector.select(IDLE_SECONDS / 4):
                if key.fileobj is self.listener:
                    self._accept()
                elif key.fileobj is self._woken:
                    with contextlib.suppress(BlockingIOError):
                        self._woken.recv(4096)
                else:
                    self._serve(key.fileobj)  # type: ignore[arg-type]
            self._reap()
            if self.quitting:
                self._hang_up()
                return

            now = time.monotonic()
            if self.servers:
                idle_since = alone_since = now
            elif self.programs:
                idle_since = now
                if now - alone_since >= ORPHAN_SECONDS:
                    self._hang_up()
                    return
            elif now - idle_since >= IDLE_SECONDS:
                return

    def _hang_up(self) -> None:
        "Close every master. The kernel hangs up each program's terminal."
        for program in self.programs.values():
            if program.master is not None:
                os.close(program.master)
                program.master = None

    def _accept(self) -> None:
        with contextlib.suppress(BlockingIOError):
            server, _ = self.listener.accept()
            server.settimeout(MESSAGE_SECONDS)
            self.servers[server] = False
            self.selector.register(server, selectors.EVENT_READ)

    def _drop(self, server: socket.socket) -> None:
        self.selector.unregister(server)
        del self.servers[server]
        server.close()

    def _serve(self, server: socket.socket) -> None:
        try:
            got = receive(server)
        except OSError, HolderError, ValueError:
            self._drop(server)
            return
        if got is None:
            self._drop(server)
            return
        message, fds = got
        # Any exception and not a list: one that escapes ends the holder,
        # and that hangs up every program it holds.
        try:
            reply, carried = self._answer(server, message, fds)
        except Exception as error:
            reply, carried = {"error": "%s: %s" % (type(error).__name__, error)}, []
        finally:
            _close_all(fds)
        try:
            send(server, reply, carried)
        except OSError:
            self._drop(server)

    def _answer(
        self, server: socket.socket, message: dict[str, Any], fds: list[int]
    ) -> tuple[dict[str, Any], list[int]]:
        op = message.get("op")
        if op == "hello":
            if message.get("version") != VERSION:
                return {"error": "this holder speaks version %d" % (VERSION,), "version": VERSION}, []
            self.servers[server] = True
            return {"version": VERSION, "pid": os.getpid()}, []
        if not self.servers[server]:
            return {"error": "say hello first"}, []
        if op == "spawn":
            return self._spawn(message, fds).describe(), []
        if op == "quit":
            self.quitting = True
            return {}, []
        if op == "programs":
            return {"programs": [p.describe() for p in self.programs.values() if not p.released]}, []
        program = self.programs.get(message.get("id"))  # type: ignore[arg-type]
        if program is None or program.released:
            return {"error": "no program %r" % (message.get("id"),)}, []
        if op == "master":
            assert program.master is not None
            return program.describe(), [program.master]
        if op == "release":
            self._release(program)
            return {}, []
        return {"error": "no op %r" % (op,)}, []

    def _spawn(self, message: dict[str, Any], fds: list[int]) -> _Program:
        """
        Fork onto the pty the server opened and sized, which arrives as
        `[master, slave]`. The holder keeps a copy of the master and lets
        the slave go: a slave left open here would hide the end of the
        program from every reader.
        """
        if len(fds) != 2:
            raise HolderError("a spawn carries the master and the slave of a pty, not %d fds" % (len(fds),))
        spawn = Spawn(list(message["command"]), dict(message["environment"]), message.get("directory"))
        master, slave = (os.dup(fd) for fd in fds)
        try:
            pid = os.fork()
        except BaseException:
            _close_all((master, slave))
            raise
        if pid == 0:
            run_in_child(spawn, master, slave)
        os.close(slave)
        program = _Program(self._next_id, pid, master)
        self._next_id += 1
        self.programs[program.program_id] = program
        return program

    def _release(self, program: _Program) -> None:
        program.released = True
        if program.master is not None:
            os.close(program.master)
            program.master = None
        if program.status is not None:
            del self.programs[program.program_id]

    def _reap(self) -> None:
        while True:
            try:
                pid, raw = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            for program in list(self.programs.values()):
                if program.pid == pid:
                    program.status = os.waitstatus_to_exitcode(raw)
                    if program.released:
                        del self.programs[program.program_id]
                    else:
                        self._tell({"event": "exited", **program.describe()})

    def _tell(self, event: dict[str, Any]) -> None:
        for server, greeted in list(self.servers.items()):
            if greeted:
                try:
                    send(server, event)
                except OSError:
                    self._drop(server)


class HolderClient:
    "A server's side of the conversation. Blocking, and standard library only."

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        #: Events that arrived while a reply was awaited, oldest first.
        self.events: list[dict[str, Any]] = []

    @classmethod
    def connect(cls, path: str, timeout: float = MESSAGE_SECONDS) -> HolderClient:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(path)
            client = cls(sock)
            client.request({"op": "hello", "version": VERSION})
        except BaseException:
            sock.close()
            raise
        return client

    def request(self, message: dict[str, Any], fds: Sequence[int] = ()) -> tuple[dict[str, Any], list[int]]:
        send(self.sock, message, fds)
        while True:
            got = receive(self.sock)
            if got is None:
                raise HolderError("the holder closed the connection")
            reply, fds = got
            if "event" in reply:
                self.events.append(reply)
                continue
            if "error" in reply:
                _close_all(fds)
                raise HolderError(reply["error"])
            return reply, fds

    def spawn(self, spawn: Spawn, master: int, slave: int) -> dict[str, Any]:
        "Start a program on the pty `master` and `slave` are, opened and sized here."
        reply, _ = self.request(spawn_request(spawn), [master, slave])
        return reply

    def programs(self) -> list[dict[str, Any]]:
        reply, _ = self.request({"op": "programs"})
        return reply["programs"]

    def master(self, program_id: int) -> int:
        _, fds = self.request({"op": "master", "id": program_id})
        (master,) = fds
        return master

    def release(self, program_id: int) -> None:
        self.request({"op": "release", "id": program_id})

    def quit(self) -> None:
        "Hang up every program and end the holder: the server is ending."
        self.request({"op": "quit"})

    def next_event(self) -> dict[str, Any]:
        "The oldest event, waiting for one when none has arrived yet."
        if self.events:
            return self.events.pop(0)
        got = receive(self.sock)
        if got is None:
            raise HolderError("the holder closed the connection")
        event, fds = got
        _close_all(fds)
        return event

    def close(self) -> None:
        self.sock.close()


def listen(path: str) -> socket.socket:
    "The holder's socket. Refuses when a live holder answers on `path` already."
    if os.path.exists(path):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(path)
        except ConnectionRefusedError, FileNotFoundError:
            os.unlink(path)
        else:
            raise HolderError("a holder answers on %s already" % (path,))
        finally:
            probe.close()
    # Bound under another name and renamed once it listens: the path
    # appears at `bind`, and a connect before `listen` is refused.
    bound = "%s.%d" % (path, os.getpid())
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(bound)
    os.chmod(bound, 0o600)
    listener.listen(16)
    os.rename(bound, path)
    return listener


def _detach() -> None:
    """
    Leave the caller and its session behind. `main` binds the socket
    first, so a caller that waits for this process to exit can connect
    at once.
    """
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir("/")
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    os.close(null)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pymux-holder", description=__doc__.splitlines()[1])
    parser.add_argument("--socket", required=True, help="where servers reach this holder")
    parser.add_argument("--detach", action="store_true", help="run on, apart from the caller")
    args = parser.parse_args(argv)
    try:
        listener = listen(args.socket)
    except HolderError as error:
        print("pymux-holder: %s" % (error,), file=sys.stderr)
        return 1
    if args.detach:
        _detach()
    try:
        Holder(listener).run()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(args.socket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
