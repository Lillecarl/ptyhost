"""
A write that arrives in several pages asks for one redraw, not one per page.

A read that fills its page has more of the same write behind it, and a
redraw between the two shows the screen half updated. So a full page
asks for no redraw, unless a sixtieth of a second has gone by since the
last one, and a write that ends exactly on a page still gets its redraw
after that long. A short read, which is what a keystroke's echo is,
asks at once. Lillecarl/pymux#568.
"""

from __future__ import annotations

import anyio

import ptyhost.process
from ptyhost import Process


class _Pages:
    "A backend that hands out the pages it was given, and says if each was full."

    ready_f = None
    closed = False

    def __init__(self) -> None:
        self.pages: list[tuple[str, bool]] = []
        self.more_is_waiting = False

    def add_input_ready_callback(self, callback) -> None:
        pass

    def read_text(self, amount: int = 4096) -> str:
        text, full = self.pages.pop(0)
        self.more_is_waiting = full
        return text


def a_process(task_group=None):
    backend = _Pages()
    asked = []
    process = Process(backend=backend, receive=lambda text: None, invalidate=lambda: asked.append(True))
    process._task_group = task_group
    return process, backend, asked


async def test_a_short_read_asks_at_once():
    async with anyio.create_task_group() as task_group:
        process, backend, asked = a_process(task_group)
        backend.pages = [("echo", False)]
        await process._read()
        assert asked == [True]


async def test_full_pages_ask_once_at_the_end_of_the_write():
    async with anyio.create_task_group() as task_group:
        process, backend, asked = a_process(task_group)
        process._invalidated_at = ptyhost.process.time.monotonic()
        backend.pages = [("a" * 4096, True), ("b" * 4096, True), ("tail", False)]
        await process._read()
        await process._read()
        assert asked == []
        await process._read()
        assert asked == [True]


async def test_a_write_that_ends_on_a_page_is_still_drawn():
    with anyio.fail_after(5):
        async with anyio.create_task_group() as task_group:
            process, backend, asked = a_process(task_group)
            process._invalidated_at = ptyhost.process.time.monotonic()
            backend.pages = [("a" * 4096, True)]
            await process._read()
            assert asked == []
    assert asked == [True]


async def test_a_flood_still_draws(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(ptyhost.process.time, "monotonic", lambda: now[0])
    async with anyio.create_task_group() as task_group:
        process, backend, asked = a_process(task_group)
        process._invalidated_at = now[0]
        backend.pages = [("a" * 4096, True)] * 3
        await process._read()
        now[0] += 1
        await process._read()
        assert asked == [True]
        task_group.cancel_scope.cancel()


async def test_without_a_group_nothing_is_held_back():
    process, backend, asked = a_process(None)
    process._invalidated_at = ptyhost.process.time.monotonic()
    backend.pages = [("a" * 4096, True)]
    await process._read()
    assert asked == [True]
