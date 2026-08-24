"""
Unit tests for the visibility/ownership/personal-token rules in
access_control.py -- the single module every other access-control
enforcement point (meta_tools, routers, /mcp) delegates to. These
tests exercise the DB-backed User/UserIdentity model via resolve_login,
not raw env-var-backed prefixed strings.
"""

import asyncio

from aggregator import access_control, database, identity_providers
from aggregator.database import add_server, delete_server
from aggregator.models import ServerType, ServerVisibility


async def _cleanup(server_id: int) -> None:
    await delete_server(server_id)


async def _make_user(raw_id: str, *, is_admin: bool = False, provider: str = "github") -> str:
    """Test helper: create a real User + UserIdentity via the same
    resolve_login() auto-provisioning path a real first login exercises,
    optionally promoting it to admin afterward. Returns the canonical
    "user:<id>" identity."""
    canonical = await access_control.resolve_login(provider, raw_id, raw_id)
    if is_admin:
        user_id = int(canonical.removeprefix("user:"))
        await database.update_user_flags(user_id, is_admin=True)
    return canonical


async def test_is_admin_true_only_for_admin_user():
    admin = await _make_user("is-admin-test-admin", is_admin=True)
    regular = await _make_user("is-admin-test-regular")
    assert await access_control.is_admin(admin)
    assert not await access_control.is_admin(regular)


async def test_is_admin_false_when_account_disabled():
    admin = await _make_user("is-admin-disabled-test", is_admin=True)
    user_id = int(admin.removeprefix("user:"))
    await database.update_user_flags(user_id, allowed=False)
    assert not await access_control.is_admin(admin)


async def test_can_manage_true_for_owner_and_admin_false_for_stranger():
    owner = await _make_user("can-manage-owner")
    admin = await _make_user("can-manage-admin", is_admin=True)
    stranger = await _make_user("can-manage-stranger")
    server = await add_server(
        "ac-can-manage",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=owner,
        visibility=ServerVisibility.PRIVATE.value,
    )
    try:
        assert await access_control.can_manage(server, owner)
        assert await access_control.can_manage(server, admin)
        assert not await access_control.can_manage(server, stranger)
    finally:
        await _cleanup(server.id)


async def test_visible_servers_private_only_to_owner_and_admin():
    owner = await _make_user("visible-private-owner")
    admin = await _make_user("visible-private-admin", is_admin=True)
    stranger = await _make_user("visible-private-stranger")
    server = await add_server(
        "ac-visible-private",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=owner,
        visibility=ServerVisibility.PRIVATE.value,
    )
    try:
        owner_names = {s.name for s in await access_control.visible_servers(owner)}
        admin_names = {s.name for s in await access_control.visible_servers(admin)}
        stranger_names = {s.name for s in await access_control.visible_servers(stranger)}
        assert server.name in owner_names
        assert server.name in admin_names
        assert server.name not in stranger_names
    finally:
        await _cleanup(server.id)


async def test_visible_servers_everyone_visible_to_all():
    owner = await _make_user("visible-everyone-owner")
    stranger = await _make_user("visible-everyone-stranger")
    server = await add_server(
        "ac-visible-everyone",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=owner,
        visibility=ServerVisibility.EVERYONE.value,
    )
    try:
        stranger_names = {s.name for s in await access_control.visible_servers(stranger)}
        assert server.name in stranger_names
    finally:
        await _cleanup(server.id)


async def test_visible_server_names_matches_visible_servers():
    owner = await _make_user("visible-names-owner")
    stranger = await _make_user("visible-names-stranger")
    server = await add_server(
        "ac-visible-names",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=owner,
        visibility=ServerVisibility.EVERYONE.value,
    )
    try:
        names = await access_control.visible_server_names(stranger)
        assert server.name in names
    finally:
        await _cleanup(server.id)


async def test_generate_and_validate_personal_token_round_trip():
    user = await _make_user("token-round-trip-user")
    token = await access_control.generate_personal_token(user)
    assert await access_control.validate_personal_token(token) == user


async def test_regenerating_token_invalidates_previous_one():
    user = await _make_user("token-regen-user")
    old_token = await access_control.generate_personal_token(user)
    new_token = await access_control.generate_personal_token(user)
    assert await access_control.validate_personal_token(old_token) is None
    assert await access_control.validate_personal_token(new_token) == user


async def test_validate_personal_token_unknown_token_returns_none():
    assert await access_control.validate_personal_token("not-a-real-token") is None


async def test_validate_personal_token_rejects_disabled_user():
    """A personal token must stop working once its owner's account is
    disabled -- the token store alone must not keep a revoked user's
    access alive (mirrors admin_auth.get_session_user's same check for the
    session-cookie path)."""
    user = await _make_user("token-disabled-user")
    token = await access_control.generate_personal_token(user)
    user_id = int(user.removeprefix("user:"))
    await database.update_user_flags(user_id, allowed=False)
    assert await access_control.validate_personal_token(token) is None


async def test_resolve_login_auto_provisions_when_no_allowlist_restriction():
    canonical = await access_control.resolve_login("github", "resolve-new-user", "New User")
    assert canonical is not None
    assert canonical.startswith("user:")
    identity = await database.get_user_identity("github", "resolve-new-user")
    assert identity is not None
    assert identity.display_name == "New User"


async def test_resolve_login_denies_when_allowlist_restricts_and_not_listed():
    row = await database.create_allowed_identity("github", "resolve-someone-else")
    try:
        result = await access_control.resolve_login("github", "resolve-not-listed", "X")
        assert result is None
    finally:
        await database.delete_allowed_identity(row.id)


async def test_resolve_login_provisions_and_consumes_matching_allowlist_row():
    row = await database.create_allowed_identity("github", "resolve-listed-user", grant_admin=True)
    try:
        canonical = await access_control.resolve_login("github", "resolve-listed-user", "Listed")
        assert canonical is not None
        user_id = int(canonical.removeprefix("user:"))
        user = await database.get_user(user_id)
        assert user.is_admin is True  # grant_admin carried through
        # The allowed_identities row must still exist after use -- deleting
        # it would make the provider look unrestricted the moment its own
        # entries get consumed (see the regression test below).
        assert await database.get_allowed_identity("github", "resolve-listed-user") is not None
    finally:
        await database.delete_allowed_identity(row.id)


async def test_resolve_login_still_restricts_provider_after_its_only_entry_is_consumed():
    """The exact bug this fix addresses: a single seeded allow-list entry
    must not open the provider to everyone once that entry's owner logs
    in -- the row must persist so has_any_allowed_identity() keeps
    reporting the provider as restricted."""
    row = await database.create_allowed_identity("github", "resolve-selfdestruct-owner")
    try:
        owner_canonical = await access_control.resolve_login(
            "github", "resolve-selfdestruct-owner", "Owner"
        )
        assert owner_canonical is not None

        stranger_result = await access_control.resolve_login(
            "github", "resolve-selfdestruct-stranger", "Stranger"
        )
        assert stranger_result is None
    finally:
        if await database.get_allowed_identity("github", "resolve-selfdestruct-owner") is not None:
            await database.delete_allowed_identity(row.id)


async def test_resolve_login_returns_existing_canonical_for_known_identity():
    first = await access_control.resolve_login("github", "resolve-repeat-login", "First")
    second = await access_control.resolve_login("github", "resolve-repeat-login", "First Again")
    assert first == second  # same account both times, not a new one


async def test_resolve_login_updates_display_name_on_repeat_login():
    await access_control.resolve_login("steam", "76500000000000010", "Old Name")
    await access_control.resolve_login("steam", "76500000000000010", "Refreshed Name")
    identity = await database.get_user_identity("steam", "76500000000000010")
    assert identity.display_name == "Refreshed Name"


async def test_resolve_login_denies_when_existing_user_not_allowed():
    canonical = await access_control.resolve_login("github", "resolve-to-disable", "X")
    user_id = int(canonical.removeprefix("user:"))
    await database.update_user_flags(user_id, allowed=False)
    result = await access_control.resolve_login("github", "resolve-to-disable", "X")
    assert result is None


async def test_resolve_login_denies_when_provider_unconfigured(monkeypatch):
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "")  # unconfigured
    result = await access_control.resolve_login("steam", "76500000000000099", "X")
    assert result is None


async def test_is_session_valid_true_for_allowed_user_false_for_disabled():
    canonical = await access_control.resolve_login("github", "session-valid-test", "X")
    assert await access_control.is_session_valid(canonical)
    user_id = int(canonical.removeprefix("user:"))
    await database.update_user_flags(user_id, allowed=False)
    assert not await access_control.is_session_valid(canonical)


async def test_is_session_valid_false_for_malformed_username():
    assert not await access_control.is_session_valid("not-a-canonical-id")
    assert not await access_control.is_session_valid("user:not-a-number")


async def test_link_identity_attaches_new_identity_to_current_user():
    user = await _make_user("link-base-user")
    outcome = await access_control.link_identity(user, "steam", "76500000000000020", "Gamer")
    assert outcome == "ok"
    identities = await database.list_user_identities(int(user.removeprefix("user:")))
    assert any(i.provider == "steam" and i.raw_id == "76500000000000020" for i in identities)


async def test_link_identity_idempotent_relink_to_same_user():
    user = await _make_user("link-idempotent-user")
    await access_control.link_identity(user, "steam", "76500000000000021", "Gamer")
    outcome = await access_control.link_identity(user, "steam", "76500000000000021", "Gamer2")
    assert outcome == "ok"
    identities = await database.list_user_identities(int(user.removeprefix("user:")))
    steam_identities = [i for i in identities if i.provider == "steam"]
    assert len(steam_identities) == 1
    assert steam_identities[0].display_name == "Gamer2"  # display name refreshed


async def test_link_identity_conflict_when_owned_by_different_user():
    victim = await _make_user("link-conflict-victim")
    await access_control.link_identity(victim, "steam", "76500000000000022", "Victim's Steam")
    attacker = await _make_user("link-conflict-attacker")
    outcome = await access_control.link_identity(attacker, "steam", "76500000000000022", "X")
    assert outcome == "conflict"
    # Victim's identity must be untouched.
    identity = await database.get_user_identity("steam", "76500000000000022")
    assert identity.user_id == int(victim.removeprefix("user:"))


async def test_link_identity_conflict_when_already_has_this_provider():
    user = await _make_user("link-same-provider-user")  # already has a github identity
    outcome = await access_control.link_identity(user, "github", "link-second-github", "X")
    assert outcome == "conflict"


async def test_link_identity_invalid_for_disabled_account():
    user = await _make_user("link-disabled-account")
    user_id = int(user.removeprefix("user:"))
    await database.update_user_flags(user_id, allowed=False)
    outcome = await access_control.link_identity(user, "steam", "76500000000000023", "X")
    assert outcome == "invalid"


async def test_unlink_identity_removes_non_last_identity():
    user = await _make_user("unlink-base-user")
    await access_control.link_identity(user, "steam", "76500000000000030", "X")
    identities = await database.list_user_identities(int(user.removeprefix("user:")))
    steam_identity = next(i for i in identities if i.provider == "steam")
    outcome = await access_control.unlink_identity(user, steam_identity.id)
    assert outcome == "ok"
    remaining = await database.list_user_identities(int(user.removeprefix("user:")))
    assert all(i.provider != "steam" for i in remaining)


async def test_unlink_identity_refuses_to_remove_last_identity():
    user = await _make_user("unlink-only-identity-user")
    identities = await database.list_user_identities(int(user.removeprefix("user:")))
    outcome = await access_control.unlink_identity(user, identities[0].id)
    assert outcome == "last_identity"
    # Must still be there.
    assert len(await database.list_user_identities(int(user.removeprefix("user:")))) == 1


async def test_unlink_identity_not_found_for_wrong_owner():
    owner = await _make_user("unlink-owner-user")
    other = await _make_user("unlink-other-user")
    await access_control.link_identity(owner, "steam", "76500000000000031", "X")
    identities = await database.list_user_identities(int(owner.removeprefix("user:")))
    steam_identity = next(i for i in identities if i.provider == "steam")
    outcome = await access_control.unlink_identity(other, steam_identity.id)
    assert outcome == "not_found"


async def test_resolve_login_concurrent_first_login_does_not_duplicate_or_crash():
    """Two concurrent resolve_login calls for the SAME brand-new identity
    must not create two accounts and must not raise -- one wins, the
    other resolves to the same winning account."""
    results = await asyncio.gather(
        access_control.resolve_login("github", "resolve-race-user", "Racer"),
        access_control.resolve_login("github", "resolve-race-user", "Racer"),
    )
    assert results[0] is not None
    assert results[1] is not None
    assert results[0] == results[1]
    identity = await database.get_user_identity("github", "resolve-race-user")
    assert identity is not None
