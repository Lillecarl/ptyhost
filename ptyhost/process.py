"""
The child process.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import ClassVar

import anyio
import anyio.abc

from .backends import Backend

__all__ = ["Process"]

logger = logging.getLogger(__name__)

#: How long a write that is still arriving may go without a redraw. A
#: read that fills its page means more of the same write is waiting,
#: and a redraw between the two shows the screen half updated
#: (Lillecarl/pymux#568). A flood still draws this often.
_REDRAW_AT_LEAST_EVERY = 1 / 60


class Process:
    """
    A program on a pty: start it, size it, pump its bytes, stop it.

    **It parses nothing and draws nothing.** What the program writes
    goes to `receive`, and whoever built this decides what that means.
    A screen is one answer, and it is not this layer's.

    Usage:

        p = Process(backend, receive=screen_of_mine.feed, ...)
        p.start()

    :param receive: Called with the text that the program wrote.
    :param invalidate: Called after `receive`, when there may be
        something new to draw.
    :param done_callback: Called when the program ends.
    :param has_priority: Returns True when this program's output should
        be read at once. Otherwise it waits for a turn of the event
        loop that nothing else wants.
    """

    #: What a hot upgrade does with each attribute: "saved", "rebuilt"
    #: or "dropped", as `pyte.keep.Keep` defines them. Strings, because
    #: ptyhost does not import pyte. Lillecarl/pymux#399.
    KEEP: ClassVar[dict[str, str]] = {
        "backend": "saved",
        "sx": "saved",
        "sy": "saved",
        "suspended": "saved",
        "_started": "rebuilt",  # an adopted program is already started
        "receive": "rebuilt",
        "invalidate": "rebuilt",
        "done_callback": "rebuilt",
        "has_priority": "rebuilt",
        "_invalidated_at": "dropped",
        "_owed": "dropped",
        "_task_group": "dropped",
    }

    def __init__(
        self,
        backend: Backend,
        receive: Callable[[str], None],
        invalidate: Callable[[], None] | None = None,
        done_callback: Callable[[], None] | None = None,
        has_priority: Callable[[], bool] | None = None,
    ) -> None:
        self.receive = receive
        self.invalidate = invalidate or (lambda: None)
        self.backend = backend
        self.done_callback = done_callback
        self.has_priority = has_priority or (lambda: True)

        self.suspended = False
        self._started = False

        # When the last redraw was asked for, whether one is owed to a
        # page that was held back, and the group that pays it.
        self._invalidated_at = 0.0
        self._owed = False
        self._task_group: anyio.abc.TaskGroup | None = None

        # Create terminal interface.
        self.backend.add_input_ready_callback(self._read)

        #: The size of the pty, in columns and rows. Nothing has said
        #: yet, and `start` picks a size if nothing ever does.
        self.sx = 0
        self.sy = 0

    async def start(self, task_group: anyio.abc.TaskGroup | None = None) -> None:
        """
        Start the process, watched by `task_group`.

        The fork is in the backend, and the supervision with it: the
        pump, the reaper and the watcher that fires `done_callback`
        run as tasks of the group. Starting twice is refused -- two
        pumps would feed every page twice.

        Without a group only a backend with no end starts: there is
        nothing to watch, so there is nothing the group would hold. A
        program that ends unwatched would outlive the scope that
        started it, and that is refused rather than run.

        The size the pane already has wins. A render sets the size and
        then starts the program, so the child forks onto a pty of the
        size it will really have. Without this the child forked at 120
        by 24, the pty resized one frame later, and a program that drew
        before the resize reached it drew at the wrong width. That is a
        race: the child starts writing as soon as it is forked, and the
        next render is a turn of the event loop away.

        A size of nothing means nobody has said, which is an embedder
        that starts the program before it draws. It gets what it always
        got.
        """
        if self._started:
            raise RuntimeError("this process is already started")
        self._started = True

        if task_group is None and self.backend.ready_f is not None:
            raise RuntimeError("a process with an end needs a task group to watch it")

        if (self.sx, self.sy) == (0, 0):
            self.set_size(120, 24)
        self._task_group = task_group
        await self.backend.start(task_group)

        if self.done_callback is not None and self.backend.ready_f is not None:
            task_group.start_soon(self._watch_end)

    def after_thaw(self) -> None:
        "A thawed process that was suspended parks its pump again."
        if self.suspended:
            self.backend.pause_reading()

    def set_size(self, width: int, height: int) -> None:
        """
        Tell the pty how big it is.

        Whatever reads this program is a size behind until it hears the
        same number, and that is the caller's to do: a screen is not
        this layer's.
        """
        if (self.sx, self.sy) != (width, height):
            self.backend.set_size(width, height)

        self.sx = width
        self.sy = height

    def write_input(self, data: str) -> None:
        """
        Write text to the program.

        It goes as it stands. A key that a mode encodes and a paste
        that brackets ask for are both decided by the screen, which
        knows the modes; `BetterScreen.encode_key` and `wrap_paste` are
        those two.
        """
        self.backend.write_text(data)

    async def _read(self) -> None:
        """
        Read callback, awaited by the pump with every page.

        A page is at most a page: reading a great deal at once gives
        the event loop one long turn, and everything else waits for it.

        A program that nobody is looking at waits for a turn of the
        event loop that nothing else wants. One checkpoint is that
        yield: the pump is rescheduled behind everything queued now,
        and everything queued before it runs before it.

        **It used to poll for an idle loop, and an idle loop never
        comes.** The test was the loop's queue empty, retried on every
        turn until a deadline. Anything that animates keeps the queue
        full, so two pollers waited for each other and neither ever
        won. With cmatrix in a window nobody looked at and one
        animating pane in the window somebody did, polling took 100%
        of a core for 5 frames in 5s, and one yield took 12% for 31.
        Lillecarl/pymux#253.
        """
        page = self.backend.read_text(4096)
        assert isinstance(page, str), "got %r" % type(page)

        # Feed directly, if this process has priority. (That is when this
        # pane has the focus in any of the clients.)
        if self.has_priority():
            self._feed(page)

        # Otherwise, postpone processing until we have CPU time available.
        else:
            await anyio.lowlevel.checkpoint()
            self._feed(page)

    def _feed(self, page: str) -> None:
        try:
            self.receive(page)
        except Exception:
            # One sequence that the emulator cannot handle must
            # not stop the pane: the program would then wait
            # forever for a reply that never comes.
            logger.exception("Feeding the terminal emulator failed.")

        # A page that filled its read has more of the same write behind
        # it, so the redraw waits for the rest, up to a limit. A write
        # that ends exactly on a page leaves nothing behind to read, so
        # a held-back redraw is owed and paid after the same limit. With
        # no group to pay it from, nothing is held back.
        now = time.monotonic()
        if (
            self.backend.more_is_waiting
            and self._task_group is not None
            and now - self._invalidated_at < _REDRAW_AT_LEAST_EVERY
        ):
            if not self._owed:
                self._owed = True
                self._task_group.start_soon(self._pay_what_is_owed)
            return
        self._invalidated_at = now
        self._owed = False
        self.invalidate()

    async def _pay_what_is_owed(self) -> None:
        await anyio.sleep(_REDRAW_AT_LEAST_EVERY)
        if self._owed:
            self._owed = False
            self._invalidated_at = time.monotonic()
            self.invalidate()

    async def _watch_end(self) -> None:
        """
        Fire `done_callback` when the program ends.

        A `done_callback` that raises must not take the scope with it:
        it is the embedder's bug, and the group holds the programs of
        every pane. So it is logged the way a feed failure is, and the
        watcher ends.
        """
        await self.backend.ready_f.wait()
        try:
            self.done_callback()
        except Exception:
            logger.exception("The done callback failed.")

    def suspend(self) -> None:
        """
        Suspend process. Stop reading stdout. (Called when going into copy mode.)
        """
        if not self.suspended:
            self.suspended = True
            self.backend.pause_reading()

    def resume(self) -> None:
        """
        Resume from 'suspend'.
        """
        if self.suspended:
            self.backend.resume_reading()
            self.suspended = False

    def get_cwd(self) -> str:
        """
        The current working directory for this process. (Or `None` when
        unknown.)
        """
        return self.backend.get_cwd()

    def get_name(self) -> str:
        """
        The name for this process. (Or `None` when unknown.)
        """
        # TODO: Maybe cache for short time.
        return self.backend.get_name()

    def kill(self) -> None:
        """
        Kill process.
        """
        self.backend.kill()

    @property
    def is_terminated(self) -> bool:
        return self.backend.closed
