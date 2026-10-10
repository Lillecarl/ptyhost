"""
Run a program on a pty: start it, size it, pump its bytes, record them.

No parsing, no drawing, no toolkit. What the program writes goes to a
callback, and whoever built the `Process` decides what it means.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .process import Process

__all__ = ("Process",)


def __getattr__(name: str):
    # Not imported eagerly: `ptyhost.holder` runs on the standard
    # library alone, and `Process` brings anyio. Lillecarl/pymux#553.
    if name == "Process":
        from .process import Process

        return Process
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
