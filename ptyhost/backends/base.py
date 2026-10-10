from __future__ import annotations

import abc

__all__ = ["Backend"]


class Backend(metaclass=abc.ABCMeta):
    """
    Base class for the terminal backend-interface.

    Starting is async and supervised: `start` takes the caller's task
    group and spawns the pump and the reaper into it, so a program
    never runs unwatched and a cancelled scope takes its tasks with
    it. The rest of the surface stays synchronous on purpose. Reading
    a page, writing input, sizing, pausing and killing are one system
    call each on the hot path, and the callers -- prompt_toolkit
    renders, Textual mounts, copy-mode keys -- are synchronous. The
    callbacks stay synchronous for the same reason: `receive` feeds a
    parser on every page the program writes.
    """

    #: The process id of the program, or `None` when this backend has
    #: no number to give: a program at the other end of an ssh
    #: connection runs on another machine, and a program that has not
    #: started yet has no id.
    #:
    #: **It is here so that a reader can ask.** `pymux` read it with
    #: `getattr(backend, "pid", None)`, which answers the same for a
    #: backend that has no id and for a field somebody renamed.
    #: Lillecarl/pymux#138.
    pid: int | None = None

    #: Set when the program ends. An `anyio.Event`: `Process` waits on
    #: it to fire `done_callback`, and a test waits on it to say the
    #: program is gone. `None` is a backend with no end -- a fake that
    #: never starts a program has nothing to wait for, and `Process`
    #: starts no watcher for one.
    #:
    #: **It is here so that a reader can ask.** `pid` above is the same
    #: shape for the same reason: a field somebody renamed answers the
    #: same as a backend that has no id. Lillecarl/pymux#138.
    ready_f = None

    def close(self):
        """
        Let go of what carries the program's output, once a read says
        there is nothing more to come.

        A backend that has nothing to let go of does nothing here. The
        one that has is the pty: its master side outlives the program,
        because what the kernel still holds is read on the turns of
        the loop after the child is reaped. Lillecarl/pymux#121.
        """

    def add_input_ready_callback(self, callback):
        """
        Run `callback` with every page the program writes.

        The callback is a coroutine: the pump awaits it after every
        page, so feeding and postponing both happen in it. It stays a
        registration and not a return because the backends part ways
        on what wakes them -- a descriptor, a channel event, a pipe
        handle -- and the pump is where those live.
        """

    @abc.abstractmethod
    def kill(self):
        """
        Terminate the sub process.
        """

    @abc.abstractproperty
    def closed(self):
        """
        Return `True` if this is closed.
        """

    @abc.abstractmethod
    def read_text(self, amount):
        """
        Read terminal output and return it.
        """

    #: Whether the last `read_text` stopped at its limit rather than at
    #: the end of what was there, so more of the same write is very
    #: likely waiting. A backend that cannot tell says False, which is
    #: what every read meant before.
    more_is_waiting: bool = False

    @abc.abstractmethod
    def write_text(self, text):
        """
        Write text to the stdin of the process.
        """

    @abc.abstractmethod
    async def start(self, task_group):
        """
        Start the terminal process, watched by `task_group`.

        The fork is here and the supervision with it: the pump that
        reads what the program writes and the reaper that waits for it
        to end run as tasks of the group, so abandoning the scope ends
        them. A scope that outlives the program is the caller's to
        hold -- a test holds one for the test, a widget for the pane.
        """

    @abc.abstractmethod
    def pause_reading(self):
        """
        Stop handing the program's output over, without losing any.

        Copy mode is the caller: the screen must not change while a
        person reads it, so the pump parks until `resume_reading`.
        What the program writes in the meantime waits in the kernel,
        the way it waits when the reader is merely slow.
        """

    @abc.abstractmethod
    def resume_reading(self):
        """
        Hand the program's output over again.

        Picks up what the program wrote while paused, the end of the
        file included: a program that ended in the meantime does not
        get to break the promise that no row changes while a person
        reads it. Its last words wait for this, the way anything else
        it wrote does.
        """

    @abc.abstractmethod
    def set_size(self, width, height):
        """
        Set terminal size.
        """

    @abc.abstractmethod
    def get_name(self):
        """
        Return the name for this process, or `None` when unknown.
        """

    @abc.abstractmethod
    def get_cwd(self):
        """
        Return the current working directory of the process running in this
        terminal.
        """
