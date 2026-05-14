import json
from dataclasses import dataclass, field
from enum import Enum

import aiosqlite

from .config import DB_PATH


class ServerType(str, Enum):
    PYPI = "pypi"
    NPM = "npm"
    GIT = "git"
    CMD = "cmd"


@dataclass
class ServerConfig:
    id: int
    name: str
    type: ServerType
    package: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


def _row_to_config(row: tuple) -> ServerConfig:
    return ServerConfig(
        id=row[0],
        name=row[1],
        type=ServerType(row[2]),
        package=row[3],
        args=json.loads(row[4]),
        env=json.loads(row[5]),
        enabled=bool(row[6]),
    )


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT    UNIQUE NOT NULL,
                type    TEXT    NOT NULL,
                package TEXT    NOT NULL,
                args    TEXT    NOT NULL DEFAULT '[]',
                env     TEXT    NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.commit()


async def list_servers() -> list[ServerConfig]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, name, type, package, args, env, enabled FROM servers ORDER BY id"
        )
        return [_row_to_config(r) for r in await cur.fetchall()]


async def get_server(server_id: int) -> ServerConfig | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, name, type, package, args, env, enabled FROM servers WHERE id = ?",
            (server_id,),
        )
        row = await cur.fetchone()
        return _row_to_config(row) if row else None


async def add_server(
    name: str,
    type: ServerType,
    package: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> ServerConfig:
    args = args or []
    env = env or {}
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO servers (name, type, package, args, env) VALUES (?, ?, ?, ?, ?)",
            (name, type.value, package, json.dumps(args), json.dumps(env)),
        )
        await db.commit()
        return ServerConfig(
            id=cur.lastrowid,
            name=name,
            type=type,
            package=package,
            args=args,
            env=env,
            enabled=True,
        )


async def update_server_enabled(server_id: int, enabled: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE servers SET enabled = ? WHERE id = ?", (int(enabled), server_id)
        )
        await db.commit()


async def delete_server(server_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM servers WHERE id = ?", (server_id,))
        await db.commit()
