import json
import time

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel, select

from .config import ADMIN_USERS, DB_PATH, GITHUB_ALLOWED_USERS, STEAM_ALLOWED_USERS
from .models import (  # noqa: F401 – re-exported for callers
    AllowedIdentity,
    AuthSeedState,
    OAuthToken,
    PersonalToken,
    Server,
    ServerType,
    ServerVisibility,
    User,
    UserIdentity,
)

_DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"
_engine = create_async_engine(_DATABASE_URL)
_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _engine, expire_on_commit=False
)


async def _migrate_oauth_tokens_table(conn: AsyncConnection) -> None:
    """oauth_tokens.github_user was renamed to `username` when Steam login
    was added (it now holds a prefixed identity like "github:octocat" or
    "steam:76561198012345678", not just a GitHub login). These rows are
    short-lived session/refresh tokens (<=30 day TTL, already pruned by
    oauth.cleanup_expired()) -- not durable data worth writing a real
    column-rename migration for. On upgrade, a legacy-shaped table is
    dropped here, *before* create_all() runs in init_db(), so create_all()
    sees no table and recreates it fresh with the new schema. Any MCP
    client with an active session at upgrade time simply redoes OAuth once.
    """

    def _sync(sync_conn):
        inspector = inspect(sync_conn)
        if "oauth_tokens" not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns("oauth_tokens")}
        if "github_user" in existing and "username" not in existing:
            sync_conn.execute(text("DROP TABLE oauth_tokens"))

    await conn.run_sync(_sync)


async def _migrate_server_columns(conn: AsyncConnection) -> None:
    """Add columns introduced after a deployment's `servers` table was first
    created -- `SQLModel.metadata.create_all` only creates missing tables,
    it never alters an existing one."""

    def _sync(sync_conn):
        existing = {col["name"] for col in inspect(sync_conn).get_columns("servers")}
        if "owner_username" not in existing:
            sync_conn.execute(text("ALTER TABLE servers ADD COLUMN owner_username TEXT"))
        if "visibility" not in existing:
            sync_conn.execute(
                text("ALTER TABLE servers ADD COLUMN visibility TEXT DEFAULT 'everyone'")
            )

    await conn.run_sync(_sync)


async def _migrate_identity_prefixes(conn: AsyncConnection) -> None:
    """Existing deployments' servers.owner_username and
    personal_tokens.username hold bare GitHub logins (e.g. "octocat") from
    before Steam login was added -- GitHub was the only provider, so every
    existing identity was necessarily a GitHub identity. Backfill the
    "github:" prefix so resolve_login()/is_session_valid()/can_manage()/
    _is_visible()/validate_personal_token() -- which now default-deny any
    unprefixed identity -- keep recognizing them after upgrade. Idempotent: only rows
    whose value contains no ":" are touched, so a fresh install or an
    already-migrated deployment is a no-op. Must run after create_all()
    and _migrate_server_columns() -- both tables, and servers.owner_username
    specifically, are guaranteed to exist by the time this runs."""

    def _sync(sync_conn):
        sync_conn.execute(
            text(
                "UPDATE servers SET owner_username = 'github:' || owner_username "
                "WHERE owner_username IS NOT NULL AND owner_username != '' "
                "AND instr(owner_username, ':') = 0"
            )
        )
        sync_conn.execute(
            text(
                "UPDATE personal_tokens SET username = 'github:' || username "
                "WHERE instr(username, ':') = 0"
            )
        )

    await conn.run_sync(_sync)


async def _migrate_to_user_accounts(conn: AsyncConnection) -> None:
    """Pre-account-linking deployments store a raw "provider:raw_id" string
    directly in servers.owner_username / personal_tokens.username. Convert
    each distinct value into a real User + UserIdentity, then rewrite the
    column to the new canonical "user:<id>" form. Pure format conversion --
    no two distinct old values are ever merged into the same account here;
    merging only ever happens through the self-service linking flow from
    now on (access_control.link_identity). Idempotent: a value already
    shaped "user:<digits>" is left untouched, so re-running this on an
    already-migrated deployment or a fresh install is a no-op. Must run
    after create_all() and _migrate_server_columns()/_migrate_identity_prefixes()
    -- both tables must already have their final column set, and any bare
    (non-prefixed) legacy value must already have been prefixed."""

    def _sync(sync_conn):
        now = time.time()
        seen: dict[tuple[str, str], int] = {}

        def _canonical_for(value: str | None) -> str | None:
            if not value or not value.strip():
                return value
            if value.startswith("user:") and value.removeprefix("user:").isdigit():
                return value  # already migrated
            provider, sep, raw_id = value.partition(":")
            if not sep:
                return value  # not a "provider:raw" shape -- nothing sane to do, leave it
            key = (provider, raw_id)
            if key not in seen:
                result = sync_conn.execute(
                    text("INSERT INTO users (is_admin, allowed, created_at) VALUES (0, 1, :now)"),
                    {"now": now},
                )
                user_id = result.lastrowid
                sync_conn.execute(
                    text(
                        "INSERT INTO user_identities (user_id, provider, raw_id, display_name) "
                        "VALUES (:user_id, :provider, :raw_id, NULL)"
                    ),
                    {"user_id": user_id, "provider": provider, "raw_id": raw_id},
                )
                seen[key] = user_id
            return f"user:{seen[key]}"

        for server_id, owner in sync_conn.execute(
            text("SELECT id, owner_username FROM servers WHERE owner_username IS NOT NULL")
        ).fetchall():
            new_value = _canonical_for(owner)
            if new_value != owner:
                sync_conn.execute(
                    text("UPDATE servers SET owner_username = :v WHERE id = :id"),
                    {"v": new_value, "id": server_id},
                )

        for (old_username,) in sync_conn.execute(
            text("SELECT username FROM personal_tokens")
        ).fetchall():
            new_value = _canonical_for(old_username)
            if new_value != old_username:
                sync_conn.execute(
                    text("UPDATE personal_tokens SET username = :v WHERE username = :old"),
                    {"v": new_value, "old": old_username},
                )

    await conn.run_sync(_sync)


async def _seed_auth_env_vars(conn: AsyncConnection) -> None:
    """GITHUB_ALLOWED_USERS/STEAM_ALLOWED_USERS/ADMIN_USERS are now only
    ever consulted here, exactly once -- after this runs, allow-lists and
    admin rights live entirely in the allowed_identities/users tables,
    managed from the webui (api/users_router.py). Gated by a dedicated
    auth_seed_state row, not table emptiness: an admin who deliberately
    empties allowed_identities later must not have it silently
    repopulated by a stale .env on the next restart. Must run after
    _migrate_to_user_accounts() -- an ADMIN_USERS entry that already has a
    User (because they already owned a server or held a personal token)
    gets is_admin=1 set directly rather than a duplicate account created."""

    def _sync(sync_conn):
        row = sync_conn.execute(text("SELECT seeded FROM auth_seed_state WHERE id = 1")).fetchone()
        if row is None:
            sync_conn.execute(text("INSERT INTO auth_seed_state (id, seeded) VALUES (1, 0)"))
        elif row[0]:
            return  # already seeded -- these three env vars are now inert

        now = time.time()

        for raw_id in GITHUB_ALLOWED_USERS:
            sync_conn.execute(
                text(
                    "INSERT OR IGNORE INTO allowed_identities (provider, raw_id, grant_admin) "
                    "VALUES ('github', :raw_id, 0)"
                ),
                {"raw_id": raw_id},
            )
        for raw_id in STEAM_ALLOWED_USERS:
            sync_conn.execute(
                text(
                    "INSERT OR IGNORE INTO allowed_identities (provider, raw_id, grant_admin) "
                    "VALUES ('steam', :raw_id, 0)"
                ),
                {"raw_id": raw_id},
            )

        for entry in ADMIN_USERS:
            provider, sep, raw_id = entry.partition(":")
            if not sep:
                continue  # malformed (missing prefix) -- skip, matches the old default-deny
            existing = sync_conn.execute(
                text("SELECT user_id FROM user_identities WHERE provider = :p AND raw_id = :r"),
                {"p": provider, "r": raw_id},
            ).fetchone()
            if existing is not None:
                sync_conn.execute(
                    text("UPDATE users SET is_admin = 1 WHERE id = :id"), {"id": existing[0]}
                )
            else:
                result = sync_conn.execute(
                    text("INSERT INTO users (is_admin, allowed, created_at) VALUES (1, 1, :now)"),
                    {"now": now},
                )
                new_user_id = result.lastrowid
                sync_conn.execute(
                    text(
                        "INSERT INTO user_identities (user_id, provider, raw_id, display_name) "
                        "VALUES (:user_id, :provider, :raw_id, NULL)"
                    ),
                    {"user_id": new_user_id, "provider": provider, "raw_id": raw_id},
                )

        sync_conn.execute(text("UPDATE auth_seed_state SET seeded = 1 WHERE id = 1"))

    await conn.run_sync(_sync)


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with _engine.begin() as conn:
        await _migrate_oauth_tokens_table(conn)
        await conn.run_sync(SQLModel.metadata.create_all)
        await _migrate_server_columns(conn)
        await _migrate_identity_prefixes(conn)
        await _migrate_to_user_accounts(conn)
        await _seed_auth_env_vars(conn)


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
    owner_username: str | None = None,
    visibility: str = ServerVisibility.PRIVATE.value,
) -> Server:
    server = Server(
        name=name,
        type=server_type.value if isinstance(server_type, ServerType) else server_type,
        package=package,
        args=json.dumps(args or []),
        env=json.dumps(env or {}),
        owner_username=owner_username,
        visibility=visibility,
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
    visibility: str | None = None,
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
            if visibility is not None:
                server.visibility = visibility
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


# ── Personal tokens ──────────────────────────────────────────────────────────


async def set_personal_token(username: str, token_hash: str) -> None:
    async with _session_factory() as session:
        existing = await session.get(PersonalToken, username)
        if existing:
            existing.token_hash = token_hash
        else:
            session.add(PersonalToken(username=username, token_hash=token_hash))
        await session.commit()


async def get_username_by_token_hash(token_hash: str) -> str | None:
    async with _session_factory() as session:
        result = await session.execute(
            select(PersonalToken).where(PersonalToken.token_hash == token_hash)
        )
        token = result.scalar_one_or_none()
        return token.username if token else None


# ── Users ─────────────────────────────────────────────────────────────────────


async def create_user(is_admin: bool = False) -> User:
    async with _session_factory() as session:
        user = User(is_admin=is_admin)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def get_user(user_id: int) -> User | None:
    async with _session_factory() as session:
        return await session.get(User, user_id)


async def list_users() -> list[User]:
    async with _session_factory() as session:
        result = await session.execute(select(User).order_by(User.id))
        return list(result.scalars().all())


async def update_user_flags(
    user_id: int, *, is_admin: bool | None = None, allowed: bool | None = None
) -> User | None:
    async with _session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            return None
        if is_admin is not None:
            user.is_admin = is_admin
        if allowed is not None:
            user.allowed = allowed
        await session.commit()
        await session.refresh(user)
        return user


async def delete_user(user_id: int) -> None:
    async with _session_factory() as session:
        user = await session.get(User, user_id)
        if user:
            await session.delete(user)
            await session.commit()


# ── User identities ──────────────────────────────────────────────────────────


async def get_user_identity(provider: str, raw_id: str) -> UserIdentity | None:
    async with _session_factory() as session:
        result = await session.execute(
            select(UserIdentity).where(
                UserIdentity.provider == provider, UserIdentity.raw_id == raw_id
            )
        )
        return result.scalar_one_or_none()


async def create_user_identity(
    user_id: int, provider: str, raw_id: str, display_name: str | None
) -> UserIdentity:
    async with _session_factory() as session:
        identity = UserIdentity(
            user_id=user_id, provider=provider, raw_id=raw_id, display_name=display_name
        )
        session.add(identity)
        await session.commit()
        await session.refresh(identity)
    return identity


async def update_user_identity_display_name(identity_id: int, display_name: str | None) -> None:
    async with _session_factory() as session:
        identity = await session.get(UserIdentity, identity_id)
        if identity:
            identity.display_name = display_name
            await session.commit()


async def list_user_identities(user_id: int) -> list[UserIdentity]:
    async with _session_factory() as session:
        result = await session.execute(
            select(UserIdentity).where(UserIdentity.user_id == user_id).order_by(UserIdentity.id)
        )
        return list(result.scalars().all())


async def delete_user_identity(identity_id: int) -> None:
    async with _session_factory() as session:
        identity = await session.get(UserIdentity, identity_id)
        if identity:
            await session.delete(identity)
            await session.commit()


# ── Allowed identities (pre-approval list) ──────────────────────────────────


async def get_allowed_identity(provider: str, raw_id: str) -> AllowedIdentity | None:
    async with _session_factory() as session:
        result = await session.execute(
            select(AllowedIdentity).where(
                AllowedIdentity.provider == provider, AllowedIdentity.raw_id == raw_id
            )
        )
        return result.scalar_one_or_none()


async def has_any_allowed_identity(provider: str) -> bool:
    async with _session_factory() as session:
        result = await session.execute(
            select(AllowedIdentity.id).where(AllowedIdentity.provider == provider).limit(1)
        )
        return result.first() is not None


async def create_allowed_identity(
    provider: str, raw_id: str, grant_admin: bool = False
) -> AllowedIdentity:
    async with _session_factory() as session:
        row = AllowedIdentity(provider=provider, raw_id=raw_id, grant_admin=grant_admin)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def delete_allowed_identity(allowed_identity_id: int) -> None:
    async with _session_factory() as session:
        row = await session.get(AllowedIdentity, allowed_identity_id)
        if row:
            await session.delete(row)
            await session.commit()


async def list_allowed_identities() -> list[AllowedIdentity]:
    async with _session_factory() as session:
        result = await session.execute(select(AllowedIdentity).order_by(AllowedIdentity.id))
        return list(result.scalars().all())


async def list_pending_allowed_identities() -> list[AllowedIdentity]:
    """Like list_allowed_identities, but excludes rows already consumed by
    a real UserIdentity. resolve_login no longer deletes a consumed row
    (deleting it would make the provider's allow-list look empty --
    unrestricted -- the moment its own entries get used); this filters the
    admin-facing "pending identities" view down to genuinely-not-yet-used
    rows instead, matching what an admin actually wants to see there."""
    async with _session_factory() as session:
        subq = select(UserIdentity.id).where(
            UserIdentity.provider == AllowedIdentity.provider,
            UserIdentity.raw_id == AllowedIdentity.raw_id,
        )
        result = await session.execute(
            select(AllowedIdentity).where(~subq.exists()).order_by(AllowedIdentity.id)
        )
        return list(result.scalars().all())
