"""
The child process.
"""
import logging
import time
from asyncio import get_event_loop
from typing import Callable

from .backends import Backend

__all__ = ["Process"]

logger = logging.getLogger(__name__)

#: How long a pane that nobody is looking at may wait before its output
#: is parsed, in seconds. One second means that a saturated machine
#: still parses a thousand bytes a second for such a pane, which is
#: enough that the interface never feels stuck.
POSTPONE = 1.0


def _when_the_loop_is_free(work: Callable[[], None], deadline: float) -> None:
    """
    Run `work` when the event loop has nothing else to do, or at
    `deadline`, whichever comes first.

    asyncio runs what is scheduled in the order it arrives, and that is
    the wrong order here: parsing the output of a pane that nobody is
    looking at may wait, and drawing for the person who is looking may
    not. A deadline keeps the wait from becoming a starve.

    This was `prompt_toolkit.eventloop.call_soon_threadsafe` with a
    `max_postpone_time`. Nothing in it is a toolkit's: it reads
    asyncio's own queue. Lillecarl/pymux#85.
    """
    loop = get_event_loop()

    def again() -> None:
        # `_ready` is what asyncio has queued. uvloop has no such
        # attribute, and then there is nothing to wait for.
        if not getattr(loop, "_ready", []) or time.time() > deadline:
            work()
            return
        loop.call_soon_threadsafe(again)

    loop.call_soon_threadsafe(again)


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

    def __init__(
        self,
        backend: Backend,
        receive: Callable[[str], None],
        invalidate: Callable[[], None] | None = None,
        done_callback: Callable[[], None] | None = None,
        has_priority: Callable[[], bool] | None = None,
    ) -> None:
        self.loop = get_event_loop()
        self.receive = receive
        self.invalidate = invalidate or (lambda: None)
        self.backend = backend
        self.done_callback = done_callback
        self.has_priority = has_priority or (lambda: True)

        self.suspended = False
        self._reader_connected = False

        # Create terminal interface.
        self.backend.add_input_ready_callback(self._read)

        if done_callback is not None:
            self.backend.ready_f.add_done_callback(lambda _: done_callback())

        #: The size of the pty, in columns and rows. Nothing has said
        #: yet, and `start` picks a size if nothing ever does.
        self.sx = 0
        self.sy = 0

    def start(self) -> None:
        """
        Start the process: fork child.

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
        if (self.sx, self.sy) == (0, 0):
            self.set_size(120, 24)
        self.backend.start()
        self.backend.connect_reader()

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

    def _read(self) -> None:
        """
        Read callback, called by the loop.
        """
        d = self.backend.read_text(4096)
        assert isinstance(d, str), "got %r" % type(d)
        # Make sure not to read too much at once. (Otherwise, this
        # could block the event loop.)

        if not self.backend.closed:

            def process() -> None:
                try:
                    self.receive(d)
                except Exception:
                    # One sequence that the emulator cannot handle must
                    # not stop the pane: the program would then wait
                    # forever for a reply that never comes.
                    logger.exception("Feeding the terminal emulator failed.")
                self.invalidate()

            # Feed directly, if this process has priority. (That is when this
            # pane has the focus in any of the clients.)
            if self.has_priority():
                process()

            # Otherwise, postpone processing until we have CPU time available.
            else:
                self.backend.disconnect_reader()

                def do_asap():
                    "Process output and reconnect to event loop."
                    process()
                    if not self.suspended:
                        self.backend.connect_reader()

                _when_the_loop_is_free(do_asap, time.time() + POSTPONE)
        else:
            # End of stream. Remove child, and let the pty go.
            #
            # **The reap cannot do it.** A program writes its last
            # words and exits, and the kernel still holds them; they
            # are read on the turns of the loop after the reap, and a
            # close there threw them away. So the reap closes the
            # slave side, which is what makes this read reach the end
            # of the file, and this closes the rest.
            # Lillecarl/pymux#121.
            self.backend.close()

    def suspend(self) -> None:
        """
        Suspend process. Stop reading stdout. (Called when going into copy mode.)
        """
        if not self.suspended:
            self.suspended = True
            self.backend.disconnect_reader()

    def resume(self) -> None:
        """
        Resume from 'suspend'.
        """
        if self.suspended:
            self.backend.connect_reader()
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
