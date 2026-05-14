import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import Tool

from .database import ServerConfig
from .installer import build_command, install

logger = logging.getLogger(__name__)


@dataclass
class ChildState:
    config: ServerConfig
    session: Optional[ClientSession] = None
    tools: list[Tool] = field(default_factory=list)
    error: Optional[str] = None
    _stack: Optional[AsyncExitStack] = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.session is not None

    async def start(self) -> None:
        await install(self.config)
        cmd = build_command(self.config)
        logger.info("Starting %s: %s", self.config.name, cmd)

        params = StdioServerParameters(
            command=cmd[0],
            args=cmd[1:],
            env=self.config.env or None,
        )
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            result = await session.list_tools()
            self.session = session
            self.tools = result.tools
            self._stack = stack
            self.error = None
            logger.info(
                "%s started: %d tool(s)", self.config.name, len(self.tools)
            )
        except Exception as exc:
            await stack.aclose()
            self.error = str(exc)
            logger.error("Failed to start %s: %s", self.config.name, exc)
            raise

    async def stop(self) -> None:
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception as exc:
                logger.warning("Error stopping %s: %s", self.config.name, exc)
            finally:
                self._stack = None
                self.session = None
                self.tools = []


class ChildManager:
    def __init__(self) -> None:
        self._children: dict[str, ChildState] = {}

    async def add(self, config: ServerConfig) -> ChildState:
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
        """Return (server_name, tool) for every running child."""
        result = []
        for name, state in self._children.items():
            if state.running:
                result.extend((name, t) for t in state.tools)
        return result

    def resolve(self, namespaced: str) -> tuple[Optional[ChildState], str]:
        """Split 'server__tool' → (ChildState | None, tool_name)."""
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


# Singleton used across the app
child_manager = ChildManager()
