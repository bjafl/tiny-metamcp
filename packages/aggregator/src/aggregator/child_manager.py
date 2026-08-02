import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

from .models import Server, ServerType
from .installer import build_command, install
from . import log_capture

logger = logging.getLogger(__name__)


def _child_logger(name: str) -> logging.LoggerAdapter:
    """Return a logger that tags records with the child server name."""
    return logging.LoggerAdapter(logger, extra={"server": name})


@dataclass
class ChildState:
    config: Server
    session: Optional[ClientSession] = None
    tools: list[Tool] = field(default_factory=list)
    error: Optional[str] = None
    _stack: Optional[AsyncExitStack] = field(default=None, repr=False)
    _log_fh: object = field(default=None, repr=False)   # file handle for child stderr

    @property
    def running(self) -> bool:
        return self.session is not None

    async def start(self) -> None:
        clog = _child_logger(self.config.name)

        if self.config.type == ServerType.PROXY:
            await self._connect(clog, streamable_http_client(self.config.package))
            return

        await install(self.config)
        cmd = build_command(self.config)
        clog.info("Starting: %s", " ".join(cmd))

        # Open per-child stderr log file
        self._log_fh = log_capture.open_log_file(self.config.name)

        params = StdioServerParameters(
            command=cmd[0],
            args=cmd[1:],
            env=self.config.get_env() or None,
        )
        # Redirect child stderr to the per-child log file.
        await self._connect(clog, stdio_client(params, errlog=self._log_fh))

    async def _connect(self, clog: logging.LoggerAdapter, transport_cm) -> None:
        """Shared session bring-up for both the stdio and proxy transports."""
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(transport_cm)
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            result = await session.list_tools()
            self.session = session
            self.tools = result.tools
            self._stack = stack
            self.error = None
            clog.info("Started – %d tool(s): %s",
                      len(self.tools), [t.name for t in self.tools])
        except Exception as exc:
            await stack.aclose()
            self._close_log_fh()
            self.error = str(exc)
            clog.error("Failed to start: %s", exc)
            raise

    async def stop(self) -> None:
        clog = _child_logger(self.config.name)
        if self._stack:
            try:
                await self._stack.aclose()
                clog.info("Stopped")
            except Exception as exc:
                clog.warning("Error during stop: %s", exc)
            finally:
                self._stack = None
                self.session = None
                self.tools = []
        self._close_log_fh()

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
