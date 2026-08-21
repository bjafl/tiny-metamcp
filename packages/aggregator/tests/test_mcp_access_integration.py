"""
End-to-end regression test: two personal tokens hitting the real /mcp
endpoint over an actual HTTP connection see different tool lists, and a
stranger's direct tool-name call against a private server is rejected --
this is the behavior a mocked transport would likely miss, since it
exercises the aggregator.current_user ContextVar across a genuine
per-connection request handled by uvicorn, not a same-task direct call.
"""

import asyncio
import socket
import sys
import time

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from aggregator import access_control
from aggregator.child_manager import child_manager
from aggregator.database import add_server, delete_server
from aggregator.models import ServerType, ServerVisibility


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


@pytest.fixture
async def aggregator_url():
    """Run the real aggregator FastAPI app (with lifespan) on a free local
    port, so /mcp is exercised through a genuine per-connection HTTP
    request rather than a same-task direct function call."""
    # To work around streamable_manager only being runnable once per instance,
    # reload the main module to get a fresh app with fresh streamable_manager.
    for mod_name in ["aggregator.main", "aggregator.aggregator"]:
        if mod_name in sys.modules:
            del sys.modules[mod_name]

    # Re-import to get fresh instances
    from aggregator.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await asyncio.to_thread(_wait_for_port, port)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task


async def _list_tool_names(url: str, token: str) -> set[str]:
    async with (
        streamable_http_client(
            url, http_client=httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
        ) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.list_tools()
        return {t.name for t in result.tools}


async def test_mcp_tool_list_filters_private_servers_per_user(proxy_target_url, aggregator_url):
    owner_name = "mcp-integ-owner-private"
    shared_name = "mcp-integ-everyone"
    owner_token = await access_control.generate_personal_token("mcp-integ-owner")
    stranger_token = await access_control.generate_personal_token("mcp-integ-stranger")

    owner_config = await add_server(
        owner_name,
        ServerType.PROXY,
        proxy_target_url,
        owner_username="mcp-integ-owner",
        visibility=ServerVisibility.PRIVATE.value,
    )
    shared_config = await add_server(
        shared_name,
        ServerType.PROXY,
        proxy_target_url,
        owner_username="mcp-integ-owner",
        visibility=ServerVisibility.EVERYONE.value,
    )
    await child_manager.add(owner_config)
    await child_manager.add(shared_config)
    try:
        owner_tools = await _list_tool_names(aggregator_url, owner_token)
        stranger_tools = await _list_tool_names(aggregator_url, stranger_token)

        assert f"{owner_name}__echo" in owner_tools
        assert f"{shared_name}__echo" in owner_tools
        assert f"{owner_name}__echo" not in stranger_tools
        assert f"{shared_name}__echo" in stranger_tools
    finally:
        await child_manager.remove(owner_name)
        await child_manager.remove(shared_name)
        await delete_server(owner_config.id)
        await delete_server(shared_config.id)


async def test_mcp_call_tool_rejects_private_server_for_non_owner(proxy_target_url, aggregator_url):
    name = "mcp-integ-call-denied"
    stranger_token = await access_control.generate_personal_token("mcp-integ-call-stranger")

    config = await add_server(
        name,
        ServerType.PROXY,
        proxy_target_url,
        owner_username="mcp-integ-call-owner",
        visibility=ServerVisibility.PRIVATE.value,
    )
    await child_manager.add(config)
    try:
        async with (
            streamable_http_client(
                aggregator_url,
                http_client=httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {stranger_token}"}
                ),
            ) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            with pytest.raises(MCPError):
                await session.call_tool(f"{name}__echo", {"text": "hi"})
    finally:
        await child_manager.remove(name)
        await delete_server(config.id)
