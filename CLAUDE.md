# tiny-metamcp

- `uv` workspace: run Python commands as `cd packages/aggregator && uv run ...` (plain `uv run` from repo root fails — `packages/webui` has no `pyproject.toml` and is excluded from the uv workspace).
- `just lint` / `just format` (ruff under the hood, invoked via `uvx` — it isn't a project dependency, same as `.pre-commit-config.yaml`).
- `just test` — runs the aggregator's pytest suite (`packages/aggregator/tests/`).
- Local non-Docker run: export `ADMIN_TOKEN` and `DATA_DIR` manually — the app doesn't load `.env` itself (only `docker-compose.yml` does), and `DATA_DIR` defaults to `/data` (unwritable outside Docker).
- Tests: prefer a real local MCP server over mocking the transport (see `tests/conftest.py`'s `proxy_target_url` fixture) — bugs here tend to live in actual anyio/mcp-SDK cancel-scope semantics a mock wouldn't exercise. Don't try to `pytest.raises(KeyboardInterrupt)` across an `asyncio.Task` boundary — it aborts the whole pytest session instead of being catchable; verify that class of behavior with a standalone `uv run python -c "..."` script.
- `mcp` is pinned `>=2,<3` on purpose — 2.0.0 broke the `Server` API (decorator registration → `on_list_tools`/`on_call_tool` callbacks), renamed `mcp.server.fastmcp.FastMCP` → `mcp.server.mcpserver.MCPServer`, and made `Tool.input_schema` (not `.inputSchema`) the real attribute. Don't bump the major without re-reading the SDK's current API first.
