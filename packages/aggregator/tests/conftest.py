"""
Shared fixtures for the aggregator test suite.

DATA_DIR must be set before `aggregator.config`/`aggregator.database` are
first imported, since both compute their paths/engine at import time --
this module runs before pytest collects any test file, so it's the one
place that's guaranteed to be early enough.
"""

import os
import socket
import tempfile
import threading
import time

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="aggregator-test-data-"))
os.environ.setdefault("ADMIN_USERS", "github:test-admin")

import pytest

from aggregator.child_manager import child_manager
from aggregator.database import init_db


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"nothing listening on 127.0.0.1:{port} after {timeout}s")


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    """Create the schema once for the whole test session, in the temp
    DATA_DIR set above."""
    import asyncio

    asyncio.run(init_db())


@pytest.fixture(scope="session")
def proxy_target_url() -> str:
    """
    URL of a real, local Streamable HTTP MCP server with two trivial tools
    (echo, add), run in a background thread for the whole test session.

    A real server (not a mock) is deliberate: the behavior this test suite
    exists to guard -- anyio cancel-scope semantics inside
    mcp.client.streamable_http.streamable_http_client -- only manifests
    against the actual transport, not a stand-in.
    """
    from mcp.server.mcpserver import MCPServer

    port = _free_port()
    mcp = MCPServer("test-target")

    @mcp.tool()
    def echo(text: str) -> str:
        return text.upper()

    @mcp.tool()
    def add(a: int, b: int) -> int:
        return a + b

    thread = threading.Thread(
        target=mcp.run,
        kwargs={"transport": "streamable-http", "port": port},
        daemon=True,
    )
    thread.start()
    _wait_for_port(port)
    return f"http://127.0.0.1:{port}/mcp"


@pytest.fixture
async def token_for():
    """Factory fixture: `token = await token_for("alice")` mints a real
    personal token for that username via access_control, exercising the
    same hash-and-store path a real user's self-service token generation
    would."""
    from aggregator import access_control

    async def _make(username: str) -> str:
        return await access_control.generate_personal_token(username)

    return _make


@pytest.fixture(autouse=True)
async def _clean_child_manager():
    """Every test starts and ends with no children registered, so a leaked
    supervisor task or leftover DB row in one test can't affect another."""
    assert not child_manager._children, (
        f"child_manager not empty before test: {list(child_manager._children)}"
    )
    yield
    for name in list(child_manager._children):
        await child_manager.remove(name)
