"""
In-memory log capture for the MCP aggregator.

Two sources are combined in the API:
- Python logging records (aggregator-side events: start/stop/errors/tool calls)
- Child stderr written to per-server log files under LOGS_DIR

LogBroker provides an asyncio pub-sub for SSE streaming.
"""

import asyncio
import collections
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from .config import LOGS_DIR

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class LogEntry:
    ts: float          # unix timestamp
    level: str         # DEBUG / INFO / WARNING / ERROR
    server: str        # child server name, or "" for aggregator-level
    msg: str

    def as_dict(self) -> dict:
        return {"ts": self.ts, "level": self.level, "server": self.server, "msg": self.msg}


# ── In-memory ring buffer (Python logging) ────────────────────────────────────

_MAXLEN = 500
_buffer: dict[str, collections.deque[LogEntry]] = {}  # keyed by server name


def _append(entry: LogEntry) -> None:
    _buffer.setdefault(entry.server, collections.deque(maxlen=_MAXLEN)).append(entry)
    _broker.publish(entry)


def get_entries(server: str | None = None, limit: int = 200) -> list[dict]:
    """Return recent log entries, optionally filtered to one server."""
    if server:
        dq = _buffer.get(server, collections.deque())
        entries = list(dq)[-limit:]
    else:
        all_entries = sorted(
            (e for dq in _buffer.values() for e in dq),
            key=lambda e: e.ts,
        )
        entries = all_entries[-limit:]
    return [e.as_dict() for e in entries]


# ── Child stderr log files ────────────────────────────────────────────────────

def log_file(server_name: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"{server_name}.log"


def open_log_file(server_name: str):
    """Open (or create) the per-child stderr log file in append mode."""
    p = log_file(server_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    return open(p, "a", buffering=1)   # line-buffered


def read_log_file(server_name: str, limit: int = 200) -> list[str]:
    p = log_file(server_name)
    if not p.exists():
        return []
    lines = p.read_text(errors="replace").splitlines()
    return lines[-limit:]


def clear_log_file(server_name: str) -> None:
    p = log_file(server_name)
    if p.exists():
        p.write_text("")


# ── SSE pub-sub broker ────────────────────────────────────────────────────────

class LogBroker:
    def __init__(self) -> None:
        self._subs: list[asyncio.Queue[LogEntry | None]] = []

    def publish(self, entry: LogEntry) -> None:
        for q in self._subs:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, server: str | None = None) -> AsyncIterator[LogEntry]:
        q: asyncio.Queue[LogEntry | None] = asyncio.Queue(maxsize=200)
        self._subs.append(q)
        try:
            while True:
                entry = await q.get()
                if entry is None:
                    break
                if server is None or entry.server == server:
                    yield entry
        finally:
            self._subs.remove(q)


_broker = LogBroker()


# ── Python logging handler ────────────────────────────────────────────────────

class MemoryLogHandler(logging.Handler):
    """Captures Python log records into the in-memory ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        server = getattr(record, "server", "")
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        _append(LogEntry(ts=record.created, level=record.levelname, server=server, msg=msg))


def setup(root_logger: logging.Logger | None = None) -> None:
    """Attach the memory handler to the root logger."""
    handler = MemoryLogHandler()
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    (root_logger or logging.getLogger()).addHandler(handler)
