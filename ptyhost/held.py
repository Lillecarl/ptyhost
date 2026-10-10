"""
A server's side of the holder (`ptyhost.holder`), for a server on anyio.

`Holding` is the connection: one task reads everything the holder
says, a request waits for its reply, and an exit wakes whoever waits on
that program. `HeldBackend` is a `PosixBackend` whose program the holder
forked and reaps. Lillecarl/pymux#553.

**A held program outlives the scope that started it.** That is the
point: a server that ends without saying so, by a crash or an upgrade,
leaves its programs to the next one. A pane ends when the holder says
its program ended, or when the server asks the holder to quit.
"""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

import anyio
import anyio.abc

from .backends.posix import PosixBackend, verify_pty
from .holder import MESSAGE_SECONDS, HolderClient, HolderError, receive, send, spawn_request
from .spawn import Spawn

__all__ = ("HeldBackend", "Holding")


class Holding:
    "One server's connection to its holder. `run` must run while it is used."

    def __init__(self, sock: socket.socket) -> None:
        # The reader waits for as long as the holder is quiet.
        sock.settimeout(None)
        self.sock = sock
        self._lock = anyio.Lock()
        self._reply: tuple[dict[str, Any], list[int]] | None = None
        self._replied = anyio.Event()
        self._ended: dict[int, anyio.Event] = {}
        #: The exit code of each program the holder said ended.
        self.statuses: dict[int, int | None] = {}
        #: The holder went away. Every program it held is gone with it.
        self.lost = False

    @classmethod
    async def connect(cls, path: str) -> Holding:
        client = await anyio.to_thread.run_sync(HolderClient.connect, path)
        return cls(client.sock)

    async def run(self) -> None:
        "Read the holder until it goes away."
        try:
            while True:
                got = await anyio.to_thread.run_sync(receive, self.sock, abandon_on_cancel=True)
                if got is None:
                    break
                message, fds = got
                if message.get("event") == "exited":
                    _close_all(fds)
                    self._end(message["id"], message["status"])
                else:
                    self._reply = (message, fds)
                    self._replied.set()
        except OSError, HolderError, ValueError:
            pass
        finally:
            self.lost = True
            self._replied.set()
            for program_id in list(self._ended):
                self._end(program_id, None)

    def _end(self, program_id: int, status: int | None) -> None:
        self.statuses[program_id] = status
        ended = self._ended.pop(program_id, None)
        if ended is not None:
            ended.set()

    async def request(self, message: dict[str, Any], fds: Sequence[int] = ()) -> tuple[dict[str, Any], list[int]]:
        async with self._lock:
            if self.lost:
                raise HolderError("the holder is gone")
            self._reply = None
            self._replied = anyio.Event()
            send(self.sock, message, fds)
            with anyio.fail_after(MESSAGE_SECONDS):
                await self._replied.wait()
            if self._reply is None:
                raise HolderError("the holder is gone")
            reply, got = self._reply
            if "error" in reply:
                _close_all(got)
                raise HolderError(reply["error"])
            return reply, got

    async def spawn(self, spawn: Spawn, master: int, slave: int) -> dict[str, Any]:
        reply, _ = await self.request(spawn_request(spawn), [master, slave])
        return reply

    async def programs(self) -> list[dict[str, Any]]:
        reply, _ = await self.request({"op": "programs"})
        return reply["programs"]

    async def master(self, program_id: int) -> int:
        _, fds = await self.request({"op": "master", "id": program_id})
        (master,) = fds
        return master

    async def release(self, program_id: int) -> None:
        await self.request({"op": "release", "id": program_id})

    async def quit(self) -> None:
        "Hang up every program and end the holder: the server is ending."
        await self.request({"op": "quit"})

    async def ended(self, program_id: int) -> int | None:
        "Wait for the program to end. Its exit code, or None when the holder went away."
        if program_id not in self.statuses and not self.lost:
            await self._ended.setdefault(program_id, anyio.Event()).wait()
        return self.statuses.get(program_id)

    def close(self) -> None:
        # The shutdown wakes the reader's thread out of its `recv`.
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()


def _close_all(fds: Sequence[int]) -> None:
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


class HeldBackend(PosixBackend):
    """
    A program on a pty that this process opened and the holder forked.

    The server reads and writes the master as for any `PosixBackend`.
    The holder reaps the child and keeps a copy of the master, so a
    cancelled scope signals nothing: the program waits for the next
    server.
    """

    KEEP: ClassVar[dict[str, str]] = {
        **PosixBackend.KEEP,
        "holding": "rebuilt",  # the next server connects for itself
        "program_id": "saved",
    }

    def __init__(
        self,
        holding: Holding,
        spawn: Callable[[], Spawn] | None,
        cell=(0, 0),
        pty: tuple[int, int | None] | None = None,
    ) -> None:
        super().__init__(spawn, cell=cell, pty=pty)  # type: ignore[arg-type]
        self.holding = holding
        #: The holder's name for the program, once it runs.
        self.program_id: int | None = None
        #: The pump read the end of the file.
        self._drained = anyio.Event()

    @classmethod
    def adopt_held(cls, holding: Holding, program_id: int, pid: int, master: int, cell=(0, 0)) -> HeldBackend:
        "A program the holder kept from an earlier server, on the master it handed over."
        verify_pty(master, None)
        backend = cls(holding, None, cell=cell, pty=(master, None))
        backend.pid = pid
        backend.program_id = program_id
        return backend

    async def start(self, task_group: anyio.abc.TaskGroup) -> None:
        if self.pid is None:
            assert self.spawn is not None and self.master is not None and self.slave is not None
            reply = await self.holding.spawn(self.spawn(), self.master, self.slave)
            self.pid = reply["pid"]
            self.program_id = reply["id"]
            # The child has its own copy now, and one here would hide the end.
            _close_all([self.slave])
            self.slave = None
        task_group.start_soon(self._pump)
        task_group.start_soon(self._reap)

    async def _pump(self) -> None:
        try:
            await super()._pump()
        finally:
            self._drained.set()

    async def _reap(self) -> None:
        """
        Wait for the holder to say the program ended, then let the holder
        forget it.

        A holder that goes away first takes the exit code with it and
        nothing else: this process holds the master too, so the program
        keeps its terminal, and its end shows as the end of the file.
        """
        try:
            assert self.program_id is not None
            await self.holding.ended(self.program_id)
            if self.holding.lost:
                await self._drained.wait()
            else:
                with contextlib.suppress(HolderError):
                    await self.holding.release(self.program_id)
        finally:
            self.ready_f.set()
