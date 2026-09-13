"""
What the remote backend does when the remote says no.

`AsyncSSHBackend.start` asks for a session in a task, and nothing awaits
that task. So a refusal -- the host, the credentials, the command, the
pty -- had nowhere to go: the exception was delivered when the loop shut
down, if ever, and `ready_f` stayed pending for ever. A caller waits on
`ready_f` through a done callback that means "the program ended", so a
pane waited with nothing in any log. Lillecarl/pymux#265.
"""

import asyncio
import logging

from ptyhost.backends.asyncssh import AsyncSSHBackend


class RefusingConnection:
    "An ssh connection that will not make a session, the way a real one won't."

    def __init__(self, error):
        self.error = error

    async def create_session(self, **named):
        raise self.error


async def _started(backend):
    "Wait for `ready_f`, and say whether it arrived."
    try:
        await asyncio.wait_for(asyncio.shield(backend.ready_f), 5.0)
    except asyncio.TimeoutError:
        return False
    return True


async def test_a_refused_session_ends_the_wait(caplog):
    error = ConnectionRefusedError("nobody listening")
    backend = AsyncSSHBackend(RefusingConnection(error))

    with caplog.at_level(logging.ERROR):
        backend.start()
        assert await _started(backend), "ready_f never resolved"

    assert "nobody listening" in caplog.text, caplog.text


async def test_the_task_is_held_while_it_runs():
    """
    A task nobody holds may be collected while it is still pending.

    The reference is what stops that, so this says the reference is
    there rather than trying to provoke the collector.
    """
    started = asyncio.Event()

    class SlowConnection:
        async def create_session(self, **named):
            started.set()
            await asyncio.sleep(10)

    backend = AsyncSSHBackend(SlowConnection())
    backend.start()
    await asyncio.wait_for(started.wait(), 5.0)

    assert backend._starting is not None
    assert not backend._starting.done()

    backend._starting.cancel()
