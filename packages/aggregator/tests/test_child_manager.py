"""
Regression tests for ChildState's supervisor-task lifecycle and exception
handling in _supervise() -- this exact code took five review rounds to get
right (see docs/superpowers/plans/2026-08-02-proxy-server-type.md), all as
one-off scratch scripts until now. Each test below corresponds to a
specific bug that was found, fixed, and previously only manually verified.

Not covered here: KeyboardInterrupt/SystemExit surviving a failing cleanup
inside _supervise(). That scenario was manually verified during the fix
that added it (raise exc, not a bare raise, in the signal branch), but a
KeyboardInterrupt raised inside a background asyncio.Task crosses the
Task boundary outside of pytest.raises' reach -- it aborts the whole test
session instead of being catchable, since asyncio (and pytest's own runner
on top of it) treats KeyboardInterrupt specially rather than as an
ordinary task exception. If this needs re-verifying, do it the same way
the original fix was verified: a standalone `uv run python -c "..."`
script, not a pytest test.
"""

import asyncio
import contextlib

import pytest

from aggregator.child_manager import ChildManager, ChildState
from aggregator.models import Server, ServerType


def _make_server(name: str, type_: ServerType, package: str, **kwargs) -> Server:
    return Server(name=name, type=type_, package=package, **kwargs)


async def test_bad_proxy_target_sets_real_error_and_raises_catchable_exception():
    """Connecting to nothing (closed port) must be catchable by a plain
    `except Exception:` -- matching every real caller (main.py's boot
    lifespan, api/routers.py's add/enable handlers) -- and state.error must
    describe the real failure, not anyio's internal cancel signal."""
    cfg = _make_server("bad-proxy", ServerType.PROXY, "http://127.0.0.1:1/mcp")
    state = ChildState(config=cfg)

    with pytest.raises(Exception) as exc_info:
        await state.start()

    assert not isinstance(exc_info.value, asyncio.CancelledError)
    assert state.error
    assert "cancel scope" not in state.error.lower()
    assert state.running is False


async def test_good_proxy_target_round_trip(proxy_target_url):
    cfg = _make_server("good-proxy", ServerType.PROXY, proxy_target_url)
    state = ChildState(config=cfg)

    await state.start()
    try:
        assert state.running is True
        assert state.error is None
        assert {t.name for t in state.tools} == {"echo", "add"}

        result = await state.session.call_tool("add", {"a": 2, "b": 40})
        assert result.content[0].text == "42"
    finally:
        await state.stop()

    assert state.running is False
    assert state.tools == []


async def test_genuine_outer_cancellation_propagates_as_cancelled_error():
    """A task running start() that's cancelled from *outside* (e.g. app
    shutdown) must see a real CancelledError, not have it converted into a
    catchable exception -- that conversion exists only for anyio's own
    *internal* task-group cancellation on a failed connection, which is a
    different thing that happens to look identical at the moment it's first
    caught (see the comment in child_manager.py's _supervise())."""

    connection_tasks: list[asyncio.Task] = []

    async def hold_open(_reader, writer):
        connection_tasks.append(asyncio.current_task())
        try:
            await asyncio.sleep(60)
        finally:
            writer.close()

    server = await asyncio.start_server(hold_open, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        cfg = _make_server("slow-proxy", ServerType.PROXY, f"http://127.0.0.1:{port}/mcp")
        state = ChildState(config=cfg)
        start_task = asyncio.create_task(state.start())
        await asyncio.sleep(0.5)
        assert not start_task.done(), "connection failed before we could cancel it -- flaky repro"

        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task
    finally:
        serve_task.cancel()
        for task in connection_tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task
        for task in connection_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        server.close()


async def test_cmd_type_nonexistent_binary_raises_original_exception_type():
    """The stdio path must be unaffected by the proxy-driven exception
    handling added to the shared _supervise()."""
    cfg = _make_server("bad-cmd", ServerType.CMD, "/no/such/binary")
    state = ChildState(config=cfg)

    with pytest.raises(Exception) as exc_info:
        await state.start()

    assert not isinstance(exc_info.value, RuntimeError)
    assert state.error


async def test_concurrent_add_for_same_name_leaves_exactly_one_supervisor(proxy_target_url):
    """Two concurrent add() calls for the same server name used to race to
    win self._children[name] -- the loser's supervisor task was left
    running with nothing able to call stop() on it, leaking a child
    process indefinitely. ChildManager._lock_for() serializes this."""
    manager = ChildManager()
    cfg = _make_server("racy", ServerType.PROXY, proxy_target_url)

    try:
        await asyncio.gather(manager.add(cfg), manager.add(cfg))

        supervisors = {
            t
            for t in asyncio.all_tasks()
            if t.get_name() == "child-supervisor-racy" and not t.done()
        }
        assert len(supervisors) == 1
    finally:
        await manager.remove("racy")
