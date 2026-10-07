from __future__ import annotations

from collections.abc import Awaitable, Callable

import anyio
from yawinpty import Pty, SpawnConfig

from .base import Backend
from .win32_pipes import PipeReader, PipeWriter

__all__ = [
    "Win32Backend",
]


class Win32Backend(Backend):
    """
    Terminal backend for Windows, on top of winpty.

    The pipe reader below is still asyncio: anyio cannot wait on a
    Windows handle, so the overlapped reads stay where they were and
    only the signal crossed over. This path is unverified on Linux --
    it is kept compiling and honest, not migrated.
    """

    def __init__(self):
        self.pty = Pty()
        self.ready_f: anyio.Event = anyio.Event()
        self._input_ready_callbacks: list[Callable[[], Awaitable[None]]] = []

        # Open input/output pipes.
        async def received_data(data):
            self._buffer.append(data)
            self._flushed.set()

        def finished():
            self.ready_f.set()
            # Wake the feeder so it sees the end. Nothing will ever
            # arrive, and a feeder that waits for it waits for ever.
            self._flushed.set()

        self.stdout_pipe_reader = PipeReader(
            self.pty.conout_name(),
            read_callback=received_data,
            done_callback=finished,
        )

        self.stdin_pipe_writer = PipeWriter(self.pty.conin_name())

        # Buffer in which we read + reading flag.
        self._buffer = []

        #: Wakes the feeder when the pipe hands something over. The
        #: buffer is the truth and this is only the wakeup.
        self._flushed = anyio.Event()

    def add_input_ready_callback(self, callback: Callable[[], Awaitable[None]]):
        """
        Add a new callback to be called for when there's input ready to read.
        """
        self._input_ready_callbacks.append(callback)
        if self._buffer:
            self._flushed.set()

    async def _feed_flushed(self) -> None:
        """
        Hand over everything the pipe holds, as it arrives.

        The drain takes the whole buffer every time, so nothing the
        wakeup raced with is left behind for the next one. When the
        program is over and the buffer is empty there is nothing left
        to wait for, so the feeder ends: leaving the scope must not
        wait on a task that never ends.
        """
        while True:
            await self._flushed.wait()
            # A fresh wakeup for the next turn. Anything that arrives
            # between the wake and this line is in the buffer, so the
            # drain below takes it anyway.
            self._flushed = anyio.Event()
            for callback in self._input_ready_callbacks:
                await callback()
            if self.ready_f.is_set() and not self._buffer:
                return

    def read_text(self, amount):
        "Read terminal output and return it."
        result = "".join(self._buffer)
        self._buffer = []
        return result

    def write_text(self, text):
        "Write text to the stdin of the process."
        self.stdin_pipe_writer.write(text)

    def pause_reading(self):
        """
        Stop handing the program's output over, without losing any.
        """
        self.stdout_pipe_reader.stop_reading()

    def resume_reading(self):
        """
        Hand the program's output over again.
        """
        self.stdout_pipe_reader.start_reading()

    @property
    def closed(self):
        return self.ready_f.is_set()

    def set_size(self, width, height):
        "Set terminal size."
        self.pty.set_size(width, height)

    async def start(self, task_group: anyio.TaskGroup) -> None:
        """
        Start the terminal process, watched by `task_group`.

        Only the feeder is supervised: the pipe reader below spawns
        itself on the asyncio loop, the way it always has, because
        anyio cannot wait on a Windows handle.
        """
        self.pty.spawn(SpawnConfig(SpawnConfig.flag.auto_shutdown, cmdline=r"C:\windows\system32\cmd.exe"))
        task_group.start_soon(self._feed_flushed)

    def kill(self):
        "Terminate the process."
        self.pty.close()

    def get_name(self):
        """
        Return the name for this process, or `None` when unknown.
        """
        return "cmd.exe"

    def get_cwd(self):
        return
