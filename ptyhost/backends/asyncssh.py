from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import anyio
import anyio.abc
from asyncssh import SSHClientChannel, SSHClientConnection, SSHClientSession

from .base import Backend

__all__ = ["AsyncSSHBackend"]

logger = logging.getLogger(__name__)


class AsyncSSHBackend(Backend):
    """
    Display asyncssh client session.

    asyncssh speaks asyncio: the channel calls back on the asyncio
    loop, and `create_session` needs one running. The group this
    starts into runs on it wherever a remote pane runs, the way
    pymux's ssh forwarding does. What this backend adds around it --
    the end event, the feeder, the supervision -- is anyio.
    """

    def __init__(
        self,
        ssh_client_connection: SSHClientConnection,
        command: str | None = None,
    ) -> None:
        self.ssh_client_connection = ssh_client_connection
        self.command = command

        self._channel: SSHClientChannel | None = None
        self._session: SSHClientSession | None = None

        self._input_ready_callbacks: list[Callable[[], Awaitable[None]]] = []
        self._receive_buffer: list[str] = []
        self.ready_f: anyio.Event = anyio.Event()

        #: Wakes the feeder when the channel hands something over. The
        #: buffer is the truth and this is only the wakeup: anything
        #: that arrives between the wake and the drain is in the
        #: buffer, so the drain takes it anyway.
        self._arrived = anyio.Event()

        #: Whether the feeder hands pages over. Copy mode parks it;
        #: pausing the channel as well stops the remote from sending.
        self._reading = anyio.Event()
        self._reading.set()

    async def start(self, task_group: anyio.abc.TaskGroup) -> None:
        """
        Ask for the session, watched by `task_group`.

        The opener resolves `ready_f` either way: `create_session`
        raises for everything a remote can refuse -- the host, the
        credentials, the command, the pty -- and a session that was
        never made has ended as surely as one that exited. Left
        pending, a caller waiting for the program to end waits for
        ever with nothing in any log. Lillecarl/pymux#265.
        """
        task_group.start_soon(self._open_session)
        task_group.start_soon(self._feed_arrived)

    async def _open_session(self) -> None:
        class Session(SSHClientSession):
            def connection_made(_, chan):
                pass

            def connection_lost(_, exc):
                self.ready_f.set()
                self._arrived.set()

            def session_started(_):
                pass

            def data_received(_, data, datatype):
                self._receive_buffer.append(data)
                self._arrived.set()

            def exit_signal_received(self, signal, core_dumped, msg, lang):
                pass

        try:
            (
                self._channel,
                self._session,
            ) = await self.ssh_client_connection.create_session(
                session_factory=lambda: Session(),
                command=self.command,
                request_pty=True,
                term_type="xterm",
                # The size a VT100 had, which is what a terminal that
                # has not been measured yet reports everywhere. The
                # pane sends its own size as soon as it knows it.
                term_size=(24, 80),
                encoding="utf-8",
            )
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as error:
            logger.error("Could not start the remote session: %s", error)
            self.ready_f.set()
            # Wake the feeder so it sees the end. Nothing will ever
            # arrive, and a feeder that waits for it waits for ever.
            self._arrived.set()

    async def _feed_arrived(self) -> None:
        """
        Hand over everything the channel holds, as it arrives.

        The drain takes the whole buffer every time, so nothing the
        wakeup raced with is left behind for the next one. When the
        session is over and the buffer is empty there is nothing left
        to wait for, so the feeder ends: leaving the scope must not
        wait on a task that never ends.
        """
        while True:
            await self._reading.wait()
            await self._arrived.wait()
            # A fresh wakeup for the next turn. Anything that arrives
            # between the wake and this line is in the buffer, so the
            # drain below takes it anyway.
            self._arrived = anyio.Event()
            for callback in self._input_ready_callbacks:
                await callback()
            if self.ready_f.is_set() and not self._receive_buffer:
                return

    @property
    def closed(self) -> bool:
        return False  # TODO
        # return self._reader.closed

    def add_input_ready_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._input_ready_callbacks.append(callback)

    def pause_reading(self) -> None:
        if self._channel is not None:
            self._channel.pause_reading()
        self._reading = anyio.Event()

    def resume_reading(self) -> None:
        if self._channel is not None:
            self._channel.resume_reading()
        self._reading.set()

    def read_text(self, amount: int = 4096) -> str:
        """
        Everything that has arrived, decoded.

        `amount` is ignored. It is there because the other backends
        take it, and asyncssh hands over whole reads rather than a
        stream this can cut. The default matches the posix backend's
        page so that a caller reading either one sees the same
        signature.
        """
        result = "".join(self._receive_buffer)
        self._receive_buffer = []
        return result

    def write_text(self, text: str) -> None:
        if self._channel:
            try:
                self._channel.write(text)
            except BrokenPipeError:
                return

    def write_bytes(self, data: bytes) -> None:
        raise NotImplementedError

    def set_size(self, width: int, height: int) -> None:
        """
        Set terminal size.
        """
        if self._channel:
            self._channel.change_terminal_size(width, height)

    def kill(self) -> None:
        "Terminate process."
        if self._channel:
            # self._channel.kill()
            self._channel.terminate()

    def send_signal(self, signal: int) -> None:
        "Send signal to running process."
        if self._channel:
            self._channel.send_signal(signal)

    def get_name(self) -> str:
        "Return the process name."
        if self._channel:
            command = self._channel.get_command()
            return f"asyncssh: {command}"
        return ""

    def get_cwd(self) -> str | None:
        if self._channel:
            return self._channel.getcwd()
        return None
