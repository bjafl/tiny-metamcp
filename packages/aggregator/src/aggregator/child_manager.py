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
        except BaseException as exc:
            # Catch BaseException, not Exception: when the connection fails, the anyio
            # task group inside streamable_http_client cancels this task, so what actually
            # surfaces here is a bare asyncio.CancelledError rather than the real transport
            # error. We still need self.error set and a *catchable* error for the
            # "except Exception:" callers (main.lifespan(), api.routers add/enable).
            #
            # A CancelledError arriving here is ambiguous: it is either anyio's internal
            # task-group cancellation (above), or a genuine outer cancellation of this task
            # (e.g. shutdown cancelling an in-flight start). Both enter this handler, and
            # at THIS point both report task.cancelling() == 1 - so the count must NOT be
            # sampled here.
            #
            # It has to be sampled *after* stack.aclose(): unwinding the stack exits
            # anyio's cancel scopes, and each scope calls task.uncancel() once for every
            # cancellation it issued itself. So a cancellation still pending after the
            # stack is closed is one that nothing in the stack owns - i.e. a genuine outer
            # cancel. Verified against anyio 4.14.2: internal -> 0, genuine -> 1.
            cleanup_exc: BaseException | None = None
            # Always clean up, whatever went wrong - including for genuine cancellation
            # and for signal exceptions.
            try:
                await stack.aclose()
            except BaseException as exc_from_cleanup:
                clog.warning("Error during stack cleanup: %s", exc_from_cleanup,
                             exc_info=True)
                cleanup_exc = exc_from_cleanup
            self._close_log_fh()

            task = asyncio.current_task()
            is_genuine_cancel = (
                isinstance(exc, asyncio.CancelledError)
                and task is not None
                and task.cancelling() > 0
            )

            # Substitute only in the single case this exists to handle: an internal
            # (non-genuine) CancelledError, where the cleanup exception carries the real
            # connection failure and the CancelledError is just anyio's cancel signal.
            # Never substitute for KeyboardInterrupt/SystemExit or a genuine outer cancel.
            if (
                cleanup_exc is not None
                and isinstance(exc, asyncio.CancelledError)
                and not is_genuine_cancel
            ):
                exc = cleanup_exc

            # Signal-like exceptions must never be masked or converted.
            # NOTE: must be "raise exc", not a bare "raise" - if the original exc was an
            # internal CancelledError and stack.aclose() itself raised the signal, exc was
            # rebound to cleanup_exc above, and a bare raise would re-raise the original
            # CancelledError from sys.exc_info(), losing the signal entirely.
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise exc
            # Genuine outer cancellation propagates untouched, as CancelledError, with the
            # task's pending cancellation left intact for the next await point.
            # A bare raise is correct here: substitution requires "not is_genuine_cancel",
            # so in this branch exc is always still the original exception.
            if is_genuine_cancel:
                raise

            # Extract meaningful error message: if it's an ExceptionGroup, try to find
            # the first real error; otherwise use the exception's string representation.
            error_msg = str(exc) or repr(exc)
            if hasattr(exc, 'exceptions'):
                # ExceptionGroup: extract first sub-exception's message
                try:
                    for sub_exc in exc.exceptions:
                        sub_msg = str(sub_exc) or repr(sub_exc)
                        if sub_msg and sub_msg.strip():
                            error_msg = sub_msg
                            break
                except (AttributeError, TypeError):
                    pass  # Fallback to the original message
            self.error = error_msg or repr(exc)  # Never let self.error be empty/falsy
            clog.error("Failed to start: %s", exc)

            # Regular Exceptions re-raise as-is (preserving type for downstream handlers).
            # NOTE: must be "raise exc", not a bare "raise" - exc may have been rebound to
            # cleanup_exc above, and a bare raise would re-raise the original CancelledError
            # from sys.exc_info() instead, which "except Exception:" callers cannot catch.
            if isinstance(exc, Exception):
                raise exc
            # Any remaining non-Exception BaseException is wrapped in RuntimeError so
            # downstream code using "except Exception:" can catch it.
            raise RuntimeError(self.error) from exc

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
