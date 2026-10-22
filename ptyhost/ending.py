"""
Say in the log which signal ended this process. Lillecarl/pymux#728.

A server or holder that a signal ends wrote nothing about it: the log
stopped, and only an empty stack file said it was no crash. Now each
writes "Ended by SIGTERM." and then dies of that same signal, so what
waits for it sees the same end as before. SIGKILL cannot be caught,
so a log that stops with no such line is SIGKILL, or a power cut.

**Not who sent it.** Only `sigwaitinfo` sees the sender, and it needs
the signal blocked in every thread. A blocked mask survives `exec`, so
every pane program the holder or the server starts would then ignore
SIGTERM and SIGHUP, and a closed terminal would no longer hang up its
shell.
"""

from __future__ import annotations

import logging
import os
import signal
import threading

__all__ = ["ENDING_SIGNALS", "say_how_it_ends"]

#: The signals that end a process by default and that a person or a
#: service manager sends to end one: `kill`, `systemctl stop`, a
#: terminal that closed.
ENDING_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def say_how_it_ends(logger: logging.Logger, signals=ENDING_SIGNALS) -> None:
    """
    Log the signal that ends this process, then die of it.

    Only a signal left at its default action is taken: one that the
    process ignores or handles keeps what it had. On the main thread
    only, because Python installs handlers nowhere else.
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def ended(number: int, frame) -> None:
        logger.warning("Ended by %s.", signal.Signals(number).name)
        signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)

    for number in signals:
        if signal.getsignal(number) is signal.SIG_DFL:
            signal.signal(number, ended)
