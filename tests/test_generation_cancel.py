"""Phase G5: a cancel button that actually cancels the in-flight
generation task, not just a spinner that hides itself. Tests
``_run_cancellable`` directly against a fake ``Request``-like object
(a real client disconnect is awkward to simulate through
``TestClient``), which is exactly the seam the real endpoint calls
through -- see its own docstring in ``app.py`` for the "a thread can't
be forcibly killed" honesty note this verifies isn't glossed over
anywhere except that one documented spot.
"""

import asyncio

import pytest

from crdt_cad.server import app as app_module


class _FakeRequest:
    """`is_disconnected()` returns False until the `disconnect_after`'th
    call, then True forever after -- simulates a client that's connected
    for a while and then aborts."""

    def __init__(self, disconnect_after: int | None = None):
        self.calls = 0
        self.disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        # A real `Request.is_disconnected()` awaits the ASGI receive
        # channel -- a genuine yield point that gives other tasks (like
        # the generation task this wraps) a chance to actually start
        # running. A no-await stub wouldn't: asyncio only hands control
        # to a freshly-scheduled task at a real yield point, so an
        # instant `return` here could plausibly cancel `task` before its
        # coroutine body ever executes a single line -- an artifact of
        # this fake being *unrealistically* fast, not real behavior.
        await asyncio.sleep(0)
        self.calls += 1
        if self.disconnect_after is not None and self.calls >= self.disconnect_after:
            return True
        return False


async def test_run_cancellable_returns_the_result_when_never_disconnected():
    async def quick():
        return "done"

    result = await app_module._run_cancellable(quick(), _FakeRequest(), poll_interval=0.02)
    assert result == "done"


async def test_run_cancellable_propagates_the_coroutines_own_exception():
    async def boom():
        raise ValueError("simulated failure")

    with pytest.raises(ValueError, match="simulated failure"):
        await app_module._run_cancellable(boom(), _FakeRequest(), poll_interval=0.02)


async def test_run_cancellable_cancels_the_task_on_disconnect():
    cancelled = False

    async def slow():
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled = True
            raise

    with pytest.raises(app_module.GenerationCancelledError):
        await app_module._run_cancellable(slow(), _FakeRequest(disconnect_after=1), poll_interval=0.02)
    assert cancelled


async def test_run_cancellable_does_not_cancel_a_task_that_finishes_before_disconnect():
    async def quick():
        await asyncio.sleep(0.01)
        return "finished first"

    # disconnect would only be observed after several polls -- the task
    # finishes well before that
    result = await app_module._run_cancellable(quick(), _FakeRequest(disconnect_after=1000), poll_interval=0.02)
    assert result == "finished first"


async def test_run_cancellable_polls_at_the_given_interval_not_faster():
    async def slow():
        await asyncio.sleep(0.3)
        return "done"

    request = _FakeRequest()
    await app_module._run_cancellable(slow(), request, poll_interval=0.1)
    # ~3 polls over 0.3s at a 0.1s interval -- generous bounds, just
    # confirming it isn't busy-polling hundreds of times per second
    assert 1 <= request.calls <= 10
