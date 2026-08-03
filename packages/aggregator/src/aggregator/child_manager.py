import asyncio
import contextlib
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

from . import log_capture
from .installer import build_command, install
from .models import Server, ServerType

logger = logging.getLogger(__name__)


def _child_logger(name: str) -> logging.LoggerAdapter:
    """Return a logger that tags records with the child server name."""
    return logging.LoggerAdapter(logger, extra={"server": name})


@dataclass
class ChildState:
    """
    A running (or starting/stopping) child MCP server.

    The child's transport (stdio subprocess, or a streamable-HTTP proxy
    connection) keeps an anyio task group open for as long as the process
    runs -- anyio requires that group to be entered and exited by the same
    task. start()/stop() can be called from whatever request happens to
    trigger them (a REST handler, a live MCP tool-call handler, the app's
    own startup/shutdown), so the group can't just be opened inline in
    start() and closed inline in stop(): those two calls are not guaranteed
    to run in the same task, and a live MCP tool call in particular runs
    nested inside the JSON-RPC dispatcher's own task group, where leaving
    the child's group open past the handler's own task aborts the whole
    session. A dedicated supervisor task owns the group for the child's
    entire lifetime instead -- the same task opens it (in _supervise) and
    closes it (when _stop_event fires) -- and start()/stop() just signal
    that task and await its outcome via ordinary asyncio primitives, which
    have no such same-task constraint.
    """

    config: Server
    session: ClientSession | None = None
    tools: list[Tool] = field(default_factory=list)
    error: str | None = None
    _supervisor: asyncio.Task | None = field(default=None, repr=False)
    _stop_event: asyncio.Event | None = field(default=None, repr=False)
    _log_fh: object = field(default=None, repr=False)  # file handle for child stderr

    @property
    def running(self) -> bool:
        return self.session is not None

    # How long to wait for the supervisor to notice _stop_event before
    # escalating to cancelling it directly -- see the except block in
    # start() below.
    _STOP_GRACE_SECONDS = 5.0

    async def start(self) -> None:
        if self.config.type != ServerType.PROXY:
            try:
                await install(self.config)
            except Exception as exc:
                self.error = str(exc) or repr(exc)
                _child_logger(self.config.name).error("Install failed: %s", exc)
                raise
        started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._stop_event = asyncio.Event()
        self._supervisor = asyncio.create_task(
            self._supervise(started), name=f"child-supervisor-{self.config.name}"
        )
        try:
            await started  # raises whatever _supervise raised on startup failure
        except BaseException:
            # e.g. this task itself got cancelled while waiting. Tear the
            # (possibly already-started) child down rather than leaking it
            # with nothing left able to call stop() on it.
            self._stop_event.set()
            if not self._supervisor.done():
                # Setting _stop_event only helps once the supervisor has
                # reached its own await on it -- if it's still stuck inside
                # connection setup (a hung TCP connect, a child that never
                # speaks MCP), nothing there is watching the event. Give it
                # a grace period for the "already past setup" case, then
                # cancel it directly -- it's blocked on an await, so unlike
                # this method's own caller, it *can* be cancelled cleanly.
                _, pending = await asyncio.wait(
                    {self._supervisor}, timeout=self._STOP_GRACE_SECONDS
                )
                if pending:
                    self._supervisor.cancel()
            with contextlib.suppress(BaseException):
                await self._supervisor
            raise

    def _build_transport(
        self, clog: logging.LoggerAdapter
    ) -> contextlib.AbstractAsyncContextManager[tuple[Any, Any]]:
        """Return the async-context-manager transport for this child's
        configured type, doing any type-specific prep first.

        Both `stdio_client()` and `streamable_http_client()` yield a plain
        `(read, write)` stream pair on entry -- `tuple[Any, Any]` rather than
        their own (differently-parametrized, partly-private) generic stream
        types, since `_supervise()` only ever unpacks the pair and hands it
        to `ClientSession()`, never touches the stream types directly.
        """
        if self.config.type == ServerType.PROXY:
            clog.info("Connecting to remote MCP server: %s", self.config.package)
            return streamable_http_client(self.config.package)

        cmd = build_command(self.config)
        clog.info("Starting: %s", " ".join(cmd))
        self._log_fh = log_capture.open_log_file(self.config.name)
        params = StdioServerParameters(
            command=cmd[0],
            args=cmd[1:],
            env=self.config.get_env() or None,
        )
        # Redirect child stderr to the per-child log file.
        return stdio_client(params, errlog=self._log_fh)

    async def _supervise(self, started: asyncio.Future) -> None:
        """Owns the child's whole lifecycle in a single detached task: opens
        the transport, signals start() once ready (or on failure), then
        holds it open until stop() signals _stop_event. Never raises out of
        the task itself (except KeyboardInterrupt/SystemExit) -- stop()
        awaits this task and, like the original AsyncExitStack-based
        teardown, a cleanup-time exception here must not propagate to
        stop()'s caller."""
        clog = _child_logger(self.config.name)
        started_ok = False
        stack = AsyncExitStack()
        try:
            transport = self._build_transport(clog)
            read, write = await stack.enter_async_context(transport)
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            result = await session.list_tools()
            self.session = session
            self.tools = result.tools
            self.error = None
            clog.info("Started – %d tool(s): %s", len(self.tools), [t.name for t in self.tools])
            started_ok = True
            if not started.done():
                started.set_result(None)

            await self._stop_event.wait()
            await stack.aclose()
            clog.info("Stopped")
        except BaseException as exc:
            if started_ok:
                # Started cleanly; this is a teardown-time problem (either
                # the stop_event-wait was itself interrupted, or the
                # stack.aclose() above raised). Best-effort cleanup + log,
                # matching stop()'s original contract that a cleanup
                # failure never propagates to its caller, and leaving
                # self.error alone rather than overwriting a clean start
                # with teardown noise.
                try:
                    await stack.aclose()
                except BaseException as exc_from_cleanup:
                    clog.warning("Error during stack cleanup: %s", exc_from_cleanup, exc_info=True)
                clog.warning("Error while stopping: %s", exc)
            else:
                # Setup failed. When the connection itself fails, the anyio
                # task group inside stdio_client()/streamable_http_client()
                # cancels this task, so what actually surfaces here is a
                # bare asyncio.CancelledError rather than the real transport
                # error -- self.error and the exception handed to started
                # need to carry the *real* failure, not that internal signal.
                #
                # A CancelledError arriving here is ambiguous: it is either
                # anyio's internal task-group cancellation (above), or a
                # genuine outer cancellation of this task. Both report
                # task.cancelling() == 1 at this point, so the count must
                # NOT be sampled here -- only after stack.aclose(), since
                # unwinding the stack exits anyio's cancel scopes and each
                # one calls task.uncancel() for every cancellation it
                # issued itself. A cancellation still pending after the
                # stack is closed is one that nothing in the stack owns --
                # i.e. a genuine outer cancel. Verified against anyio
                # 4.14.2: internal -> 0, genuine -> 1.
                cleanup_exc: BaseException | None = None
                try:
                    await stack.aclose()
                except BaseException as exc_from_cleanup:
                    clog.warning("Error during stack cleanup: %s", exc_from_cleanup, exc_info=True)
                    cleanup_exc = exc_from_cleanup

                task = asyncio.current_task()
                is_genuine_cancel = (
                    isinstance(exc, asyncio.CancelledError)
                    and task is not None
                    and task.cancelling() > 0
                )

                # Substitute only in the single case this exists to handle:
                # an internal (non-genuine) CancelledError, where the
                # cleanup exception carries the real connection failure and
                # the CancelledError is just anyio's cancel signal. Never
                # substitute for KeyboardInterrupt/SystemExit or a genuine
                # outer cancel.
                if (
                    cleanup_exc is not None
                    and isinstance(exc, asyncio.CancelledError)
                    and not is_genuine_cancel
                ):
                    exc = cleanup_exc

                # Signal-like exceptions must propagate untouched -- the
                # process itself is trying to exit.
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    if not started.done():
                        started.set_exception(exc)
                    # Must be "raise exc", not a bare "raise": if the
                    # original exc was an internal CancelledError and
                    # stack.aclose() itself raised the signal, exc was
                    # rebound to cleanup_exc above, and a bare raise would
                    # re-raise the original CancelledError from
                    # sys.exc_info() instead, losing the signal entirely.
                    raise exc  # noqa: TRY201 -- see comment above

                if is_genuine_cancel:
                    # Nothing here was cancelled by a failed connection --
                    # this task was cancelled from outside (e.g. start()'s
                    # own caller giving up, or app shutdown). "CancelledError"
                    # alone is a useless status() message, so say what
                    # actually happened instead of falling through to the
                    # empty-str(exc) case below.
                    error_msg = "Startup was cancelled"
                else:
                    # Extract a meaningful message: if it's an
                    # ExceptionGroup, find the first real sub-error;
                    # otherwise use str(exc).
                    error_msg = str(exc) or repr(exc)
                    if hasattr(exc, "exceptions"):
                        try:
                            for sub_exc in exc.exceptions:
                                sub_msg = str(sub_exc) or repr(sub_exc)
                                if sub_msg and sub_msg.strip():
                                    error_msg = sub_msg
                                    break
                        except AttributeError, TypeError:
                            pass  # fall back to the original message
                self.error = error_msg or repr(exc)  # never let this be falsy
                clog.error("Failed to start: %s", exc)

                # start()'s callers use `except Exception:` (ChildManager.add,
                # meta_tools._add_server, api/routers.py, main.lifespan) --
                # hand them something catchable regardless of what exc
                # actually was (including a genuine outer CancelledError,
                # which this detached supervisor task has no caller relying
                # on seeing as a real cancellation).
                if not started.done():
                    if isinstance(exc, Exception):
                        started.set_exception(exc)
                    else:
                        wrapped = RuntimeError(self.error)
                        wrapped.__cause__ = exc
                        started.set_exception(wrapped)
        finally:
            self.session = None
            self.tools = []
            self._close_log_fh()
            if not started.done():
                started.set_exception(RuntimeError("supervisor exited without signalling start"))

    async def stop(self) -> None:
        if self._supervisor and not self._supervisor.done():
            self._stop_event.set()
            await self._supervisor
        self._supervisor = None
        self.session = None
        self.tools = []

    def _close_log_fh(self) -> None:
        if self._log_fh:
            with contextlib.suppress(Exception):
                self._log_fh.close()
            self._log_fh = None


class ChildManager:
    def __init__(self) -> None:
        self._children: dict[str, ChildState] = {}
        # One lock per server name, serializing add/remove/restart for that
        # name. Without this, two concurrent calls for the same name (e.g.
        # two overlapping restart_server tool calls) can each create their
        # own supervisor task and race to win self._children[name] -- the
        # loser's supervisor is left running with no reference anyone can
        # call stop() on, leaking its child process indefinitely.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        return lock

    async def add(self, config: Server) -> ChildState:
        async with self._lock_for(config.name):
            if config.name in self._children:
                await self._remove_locked(config.name)
            state = ChildState(config=config)
            await state.start()
            self._children[config.name] = state
            return state

    async def remove(self, name: str) -> None:
        async with self._lock_for(name):
            await self._remove_locked(name)

    async def _remove_locked(self, name: str) -> None:
        """remove()'s body, for callers that already hold _lock_for(name)."""
        state = self._children.pop(name, None)
        if state:
            await state.stop()

    async def restart(self, name: str) -> ChildState:
        async with self._lock_for(name):
            state = self._children.get(name)
            if not state:
                raise KeyError(f"Unknown server: {name}")
            await state.stop()
            await state.start()
            return state

    def get(self, name: str) -> ChildState | None:
        return self._children.get(name)

    def all_tools(self) -> list[tuple[str, Tool]]:
        result = []
        for name, state in self._children.items():
            if state.running:
                result.extend((name, t) for t in state.tools)
        return result

    def resolve(self, namespaced: str) -> tuple[ChildState | None, str]:
        if "__" not in namespaced:
            return None, namespaced
        srv, tool = namespaced.split("__", 1)
        return self._children.get(srv), tool

    def status(self) -> list[dict]:
        return [
            {
                "name": s.config.name,
                "type": s.config.type,
                "package": s.config.package,
                "running": s.running,
                "tool_count": len(s.tools),
                "error": s.error,
            }
            for s in self._children.values()
        ]


# Singleton
child_manager = ChildManager()
