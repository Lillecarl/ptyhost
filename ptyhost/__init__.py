"""
Run a program on a pty: start it, size it, pump its bytes, record them.

No parsing, no drawing, no toolkit. What the program writes goes to a
callback, and whoever built the `Process` decides what it means.
"""

from .process import Process

__all__ = ("Process",)
