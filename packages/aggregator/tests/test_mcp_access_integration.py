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

from aggregator import oauth
from aggregator.child_manager import child_manager
from aggregator.database import add_server, delete_server, update_user_flags
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


async def test_mcp_tool_list_filters_private_servers_per_user(
    proxy_target_url, aggregator_url, make_user, token_for
):
    owner_name = "mcp-integ-owner-private"
    shared_name = "mcp-integ-everyone"
    owner = await make_user("mcp-integ-owner")
    stranger = await make_user("mcp-integ-stranger")
    owner_token = await token_for(owner)
    stranger_token = await token_for(stranger)

    owner_config = await add_server(
        owner_name,
        ServerType.PROXY,
        proxy_target_url,
        owner_username=owner,
        visibility=ServerVisibility.PRIVATE.value,
    )
    shared_config = await add_server(
        shared_name,
        ServerType.PROXY,
        proxy_target_url,
        owner_username=owner,
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


async def test_mcp_call_tool_rejects_private_server_for_non_owner(
    proxy_target_url, aggregator_url, make_user, token_for
):
    name = "mcp-integ-call-denied"
    stranger = await make_user("mcp-integ-call-stranger")
    stranger_token = await token_for(stranger)
    owner = await make_user("mcp-integ-call-owner")

    config = await add_server(
        name,
        ServerType.PROXY,
        proxy_target_url,
        owner_username=owner,
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


def _pkce_pair(verifier: str) -> tuple[str, str]:
    import base64
    import hashlib

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


async def test_mcp_bearer_oauth_token_revoked_when_account_disabled(aggregator_url, make_user):
    """Finding 2 regression: main.py's _check_bearer must re-validate an
    OAuth bearer token's owner against current DB state on every request,
    same as the personal-token branch already does -- otherwise disabling
    an account (allowed=False) doesn't actually revoke its /mcp access
    for up to the access token's 1hr lifetime (plus indefinitely via
    refresh). Mints a real OAuth access token via the actual
    start_session/finish_session/exchange_code flow, same as a live MCP
    client would, then disables the account and confirms /mcp now 401s."""
    user = await make_user("mcp-integ-oauth-disable-user")
    user_id = int(user.removeprefix("user:"))

    verifier, challenge = _pkce_pair("a-real-code-verifier-at-least-43-characters-long")
    state = oauth.start_session(
        "oauth-disable-client", "https://client.example/cb", challenge, "cs"
    )
    finish_result = await oauth.finish_session(state, user)
    assert finish_result is not None
    code, _, _ = finish_result
    exchange_result = await oauth.exchange_code(
        code, verifier, "oauth-disable-client", "https://client.example/cb"
    )
    assert exchange_result is not None
    access_token, _ = exchange_result

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    async with httpx2.AsyncClient() as h:
        # Sanity check: the token grants access while the account is
        # still enabled -- _check_bearer's auth dependency must not
        # itself reject this request (whatever streamable_manager makes
        # of the "ping" body is irrelevant here).
        still_enabled = await h.post(aggregator_url, headers=headers, json=body)
        assert still_enabled.status_code != 401

        await update_user_flags(user_id, allowed=False)

        after_disable = await h.post(aggregator_url, headers=headers, json=body)
        assert after_disable.status_code == 401
