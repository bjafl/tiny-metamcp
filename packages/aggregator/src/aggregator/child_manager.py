import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import Tool

from .models import Server
from .installer import build_command, install
from . import log_capture

logger = logging.getLogger(__name__)


def _child_logger(name: str) -> logging.LoggerAdapter:
    """Return a logger that tags records with the child server name."""
    return logging.LoggerAdapter(logger, extra={"server": name})


@dataclass
class ChildState:
    """
    A running (or starting/stopping) child MCP server.

    The child's stdio transport keeps an anyio task group open for as long
    as the process runs -- anyio requires that group to be entered and
    exited by the same task. start()/stop() can be called from whatever
    request happens to trigger them (a REST handler, a live MCP tool-call
    handler, the app's own startup/shutdown), so the group can't just be
    opened inline in start() and closed inline in stop(): those two calls
    are not guaranteed to run in the same task, and a live MCP tool call in
    particular runs nested inside the JSON-RPC dispatcher's own task group,
    where leaving the child's group open past the handler's own task
    aborts the whole session. A dedicated supervisor task owns the group
    for the child's entire lifetime instead -- the same task opens it (in
    _supervise) and closes it (when _stop_event fires) -- and start()/stop()
    just signal that task and await its outcome via ordinary asyncio
    primitives, which have no such same-task constraint.
    """

    config: Server
    session: Optional[ClientSession] = None
    tools: list[Tool] = field(default_factory=list)
    error: Optional[str] = None
    _supervisor: Optional[asyncio.Task] = field(default=None, repr=False)
    _stop_event: Optional[asyncio.Event] = field(default=None, repr=False)
    _log_fh: object = field(default=None, repr=False)   # file handle for child stderr

    @property
    def running(self) -> bool:
        return self.session is not None

    async def start(self) -> None:
        await install(self.config)
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
            try:
                await self._supervisor
            except BaseException:
                pass
            raise

    async def _supervise(self, started: asyncio.Future) -> None:
        """Owns the child's whole lifecycle in a single detached task: opens
        the stdio transport, signals start() once ready (or on failure),
        then holds the transport open until stop() signals _stop_event.
        Never raises out of the task itself -- stop() awaits this task and,
        like the original AsyncExitStack-based teardown, a cleanup-time
        exception here must not propagate to stop()'s caller."""
        clog = _child_logger(self.config.name)
        started_ok = False
        try:
            cmd = build_command(self.config)
            clog.info("Starting: %s", " ".join(cmd))
            self._log_fh = log_capture.open_log_file(self.config.name)
            params = StdioServerParameters(
                command=cmd[0],
                args=cmd[1:],
                env=self.config.get_env() or None,
            )
            async with AsyncExitStack() as stack:
                # Redirect child stderr to the per-child log file.
                read, write = await stack.enter_async_context(
                    stdio_client(params, errlog=self._log_fh)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                result = await session.list_tools()
                self.session = session
                self.tools = result.tools
                self.error = None
                clog.info("Started – %d tool(s): %s",
                          len(self.tools), [t.name for t in self.tools])
                started_ok = True
                if not started.done():
                    started.set_result(None)

                await self._stop_event.wait()
            clog.info("Stopped")
        except BaseException as exc:
            if started_ok:
                clog.warning("Error while stopping: %s", exc)
            else:
                self.error = str(exc)
                clog.error("Failed to start: %s", exc)
                if not started.done():
                    started.set_exception(exc)
        finally:
            self.session = None
            self.tools = []
            self._close_log_fh()
            if not started.done():
                started.set_exception(
                    RuntimeError("supervisor exited without signalling start")
                )

    async def stop(self) -> None:
        if self._supervisor and not self._supervisor.done():
            self._stop_event.set()
            await self._supervisor
        self._supervisor = None
        self.session = None
        self.tools = []

    def _close_log_fh(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None


class ChildManager:
    def __init__(self) -> None:
        self._children: dict[str, ChildState] = {}

    async def add(self, config: Server) -> ChildState:
        if config.name in self._children:
            await self.remove(config.name)
        state = ChildState(config=config)
        await state.start()
        self._children[config.name] = state
        return state

    async def remove(self, name: str) -> None:
        state = self._children.pop(name, None)
        if state:
            await state.stop()

    async def restart(self, name: str) -> ChildState:
        state = self._children.get(name)
        if not state:
            raise KeyError(f"Unknown server: {name}")
        await state.stop()
        await state.start()
        return state

    def get(self, name: str) -> Optional[ChildState]:
        return self._children.get(name)

    def all_tools(self) -> list[tuple[str, Tool]]:
        result = []
        for name, state in self._children.items():
            if state.running:
                result.extend((name, t) for t in state.tools)
        return result

    def resolve(self, namespaced: str) -> tuple[Optional[ChildState], str]:
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
