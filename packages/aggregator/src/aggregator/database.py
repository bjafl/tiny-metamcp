import json

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from .config import DB_PATH
from .models import (  # noqa: F401 – re-exported for callers
    OAuthToken,
    Server,
    ServerType,
)

_DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"
_engine = create_async_engine(_DATABASE_URL)
_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _engine, expire_on_commit=False
)


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


# ── Servers ───────────────────────────────────────────────────────────────────


async def list_servers() -> list[Server]:
    async with _session_factory() as session:
        result = await session.execute(select(Server).order_by(Server.id))
        return list(result.scalars().all())


async def get_server(server_id: int) -> Server | None:
    async with _session_factory() as session:
        return await session.get(Server, server_id)


async def add_server(
    name: str,
    server_type: ServerType,
    package: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> Server:
    server = Server(
        name=name,
        type=server_type.value if isinstance(server_type, ServerType) else server_type,
        package=package,
        args=json.dumps(args or []),
        env=json.dumps(env or {}),
    )
    async with _session_factory() as session:
        session.add(server)
        await session.commit()
        await session.refresh(server)
    return server


async def update_server_enabled(server_id: int, enabled: bool) -> None:
    async with _session_factory() as session:
        server = await session.get(Server, server_id)
        if server:
            server.enabled = enabled
            await session.commit()


async def update_server(
    server_id: int,
    name: str | None = None,
    server_type: ServerType | None = None,
    package: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> Server | None:
    try:
        async with _session_factory() as session:
            server = await session.get(Server, server_id)
            if not server:
                return None
            if name is not None:
                server.name = name
            if server_type is not None:
                server.type = (
                    server_type.value if isinstance(server_type, ServerType) else server_type
                )
            if package is not None:
                server.package = package
            if args is not None:
                server.args = json.dumps(args)
            if env is not None:
                server.env = json.dumps(env)
            await session.commit()
            await session.refresh(server)
            return server
    except IntegrityError as exc:
        # The unique constraint on Server.name is the only one that can
        # fail here -- letting the async-with block's own __aexit__ close
        # the session before we convert the error keeps SQLAlchemy's
        # rollback/close bookkeeping on its normal path (an explicit
        # session.rollback() from inside the except, still nested in the
        # async-with, corrupted the session's greenlet bridging).
        raise ValueError(f"A server named {name!r} already exists") from exc


async def delete_server(server_id: int) -> None:
    async with _session_factory() as session:
        server = await session.get(Server, server_id)
        if server:
            await session.delete(server)
            await session.commit()
