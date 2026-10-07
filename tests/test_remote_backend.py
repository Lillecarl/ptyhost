"""
What the remote backend does when the remote says no.

`AsyncSSHBackend.start` asks for a session in a task of the caller's
group. So a refusal -- the host, the credentials, the command, the
pty -- is heard where the opener runs: it is logged, and `ready_f` is
resolved either way. A caller waits on `ready_f` for the program to
end, and a session that was never made has ended as surely as one
that exited. Left pending, it is a pane that waits for ever with
nothing in any log. Lillecarl/pymux#265.

A task the group owns needs no holding, either: the reference the
old code kept so the collector would not take a pending task is the
group itself now, and leaving the scope cancels what is still asking.
"""

from __future__ import annotations

import logging

import anyio

from ptyhost.backends.asyncssh import AsyncSSHBackend


class RefusingConnection:
    "An ssh connection that will not make a session, the way a real one won't."

    def __init__(self, error):
        self.error = error

    async def create_session(self, **named):
        raise self.error


async def test_a_refused_session_ends_the_wait(caplog):
    error = ConnectionRefusedError("nobody listening")
    backend = AsyncSSHBackend(RefusingConnection(error))

    with caplog.at_level(logging.ERROR):
        async with anyio.create_task_group() as task_group:
            await backend.start(task_group)
            with anyio.fail_after(5.0):
                await backend.ready_f.wait()

    assert "nobody listening" in caplog.text, caplog.text


async def test_a_session_that_never_answers_dies_with_its_scope():
    """
    Leaving the scope cancels the opener, and nothing is left asking.

    The old code held the task in `backend._starting` so the collector
    would not take it while it still ran. The group holds it now, and
    the group going away cancels it: no channel was made, and no task
    of the backend outlives the scope that started it.
    """
    started = anyio.Event()

    class SlowConnection:
        async def create_session(self, **named):
            started.set()
            await anyio.sleep(10)

    backend = AsyncSSHBackend(SlowConnection())
    async with anyio.create_task_group() as task_group:
        await backend.start(task_group)
        with anyio.fail_after(5.0):
            await started.wait()
        # Leaving the scope waits for its tasks, and the opener is
        # still asking: cancel it first, the way abandoning a program
        # cancels its pump.
        task_group.cancel_scope.cancel()

    assert backend._channel is None
