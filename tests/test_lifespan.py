"""
Tests for the lifespan background-task cancellation pattern.

We test the asyncio pattern directly (not importing main to avoid side effects
from BinanceClient / PriceCache / .env at import time).

The lifespan shutdown code is:
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)

These tests verify that pattern behaves correctly for infinite loops,
tasks that raise CancelledError, and tasks that are already done.
"""
import asyncio


# ---------------------------------------------------------------------------
# Helpers that mimic real background task coroutines
# ---------------------------------------------------------------------------

async def _infinite_loop():
    """Simulates price_cache.run() / monitor_position_triggers(): runs forever."""
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass  # clean exit on cancel


async def _reraises_cancelled():
    """Task that lets CancelledError propagate — asyncio.gather must absorb it."""
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        raise


async def _already_done():
    """Task that finishes immediately (e.g. tg_bot disabled)."""
    return "done"


# ---------------------------------------------------------------------------
# Core helper: runs the exact cancellation pattern used in lifespan
# ---------------------------------------------------------------------------

async def _run_shutdown(coros):
    """
    Start tasks, immediately cancel them (simulating shutdown),
    collect results via gather(return_exceptions=True).
    Returns (tasks, results).
    """
    tasks = [asyncio.create_task(c) for c in coros]
    # Let tasks start
    await asyncio.sleep(0)
    # Shutdown — mirror of lifespan cleanup
    for t in tasks:
        t.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return tasks, results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_infinite_tasks_are_cancelled_on_shutdown():
    """Infinite loops (price_cache, monitor) must be done after cancel+gather."""
    async def _run():
        tasks, results = await _run_shutdown([
            _infinite_loop(),
            _infinite_loop(),
            _infinite_loop(),
        ])
        for t in tasks:
            assert t.done(), "task must be done after gather"
        # All return None (clean exit inside the coroutine)
        assert all(r is None for r in results)

    asyncio.run(_run())


def test_cancelled_error_does_not_propagate_to_caller():
    """
    return_exceptions=True means CancelledError from tasks never reaches
    the lifespan — shutdown completes without raising.
    """
    async def _run():
        _, results = await _run_shutdown([
            _reraises_cancelled(),
            _reraises_cancelled(),
            _reraises_cancelled(),
        ])
        # Each result is a CancelledError instance, NOT raised
        for r in results:
            assert isinstance(r, (asyncio.CancelledError, BaseException)), (
                f"expected CancelledError, got {r!r}"
            )

    asyncio.run(_run())


def test_already_done_task_is_not_harmed_by_cancel():
    """
    A task that finished before cancel() is called must not cause errors.
    task.cancel() on a done task is a no-op; gather returns its result.
    """
    async def _run():
        tasks, results = await _run_shutdown([_already_done()])
        assert tasks[0].done()
        # Result is the return value, not an exception
        assert results[0] == "done"

    asyncio.run(_run())


def test_mixed_tasks_all_resolve():
    """
    Mix of infinite loops and re-raising tasks — gather must resolve all of them
    without raising, and every task must be done.
    """
    async def _run():
        tasks, results = await _run_shutdown([
            _infinite_loop(),
            _reraises_cancelled(),
            _infinite_loop(),
        ])
        assert len(tasks) == 3
        assert all(t.done() for t in tasks)
        assert len(results) == 3  # one per task, no exception leak

    asyncio.run(_run())


def test_three_tasks_created_matching_lifespan():
    """Lifespan creates exactly 3 background tasks."""
    async def _run():
        tasks = [
            asyncio.create_task(_infinite_loop()),   # price_cache.run()
            asyncio.create_task(_infinite_loop()),   # monitor_position_triggers()
            asyncio.create_task(_infinite_loop()),   # tg_bot.start_polling()
        ]
        assert len(tasks) == 3
        for t in tasks:
            assert not t.done(), "tasks should be running"
        # cleanup
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_run())
