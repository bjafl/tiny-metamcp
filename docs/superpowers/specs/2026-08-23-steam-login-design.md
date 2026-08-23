# Design: Steam login as an alternative identity provider

**Date:** 2026-08-23
**Status:** Approved, not yet implemented

## Context

Today there is exactly one identity provider: GitHub. Both auth surfaces —
the admin webui's browser session (`admin_auth.py`) and the MCP-client-facing
OAuth 2.1 + PKCE flow (`oauth.py`/`api/oauth_router.py`) — hardcode GitHub's
OAuth 2.0 authorization-code exchange, and every identity-shaped thing in the
app (`GITHUB_ALLOWED_USERS`, `ADMIN_USERS`, `Server.owner_username`,
`PersonalToken.username`, `OAuthToken.github_user`) assumes a "username" is a
stable GitHub login string.

This design adds Steam as a second, optional login method, available on both
auth surfaces. Steam uses **OpenID 2.0**, not OAuth — no client secret, no
authorization code exchange. The redirect carries an `openid.claimed_id` URL
containing a stable 64-bit SteamID; the response is verified by POSTing the
same parameters back to Steam with `openid.mode=check_authentication` (this
POST *is* the security boundary — skipping it lets an attacker forge a
callback claiming any SteamID). An optional `STEAM_API_KEY` enables a
follow-up call to `ISteamUser/GetPlayerSummaries` for a display name; without
it, Steam login still works, just with no persona name available.

Both providers become optional; startup requires that **at least one** is
configured. GitHub's existing callback URL and OAuth App registration are
unaffected — Steam gets its own new callback path, not a shared one.

## Identity model

One person logging in with both GitHub and Steam gets **two separate
identities** in this system — no account linking. Each provider's resolved
identity is prefixed to keep the two namespaces from ever colliding:
`"github:octocat"`, `"steam:76561198012345678"`. This prefixed string is what
flows into every existing identity-shaped column and env var going forward:
`Server.owner_username`, `PersonalToken.username`, `ADMIN_USERS`.

`GITHUB_ALLOWED_USERS` and the new `STEAM_ALLOWED_USERS` stay **unprefixed**
(raw GitHub logins / raw SteamID64s respectively) — copy-paste friendly, and
`GITHUB_ALLOWED_USERS` needs no changes to existing deployments' `.env`
files. `ADMIN_USERS` is the one place identity must be unambiguous across
providers in a single list, so its values are prefixed.

Display names are handled separately from identity (see "Display names"
below) — they're cosmetic, never used for access control.

## Data model

`OAuthToken.github_user: str` is renamed to `username: str` (still holds the
resolved identity, now prefixed). No migration: these are short-lived MCP
session/refresh tokens (1 hour / 30 days TTL, already pruned by the existing
`cleanup_expired()` job), not durable user data. `init_db()` drops and
recreates the `oauth_tokens` table on upgrade if the old `github_user` column
is present — the one-time cost is that any MCP client with an active session
at upgrade time has to redo OAuth once. Writing a real column-rename
migration for genuinely disposable token-cache data isn't worth it.

No other schema changes. `Server.owner_username` and `PersonalToken.username`
already just hold opaque strings — they start receiving prefixed values with
no column change needed.

## Config

New env vars, all optional:

- `STEAM_API_KEY` — a Steam Web API key (free, from
  `steamcommunity.com/dev/apikey`). Its presence is the enable flag for
  Steam login *and* is used for the `GetPlayerSummaries` display-name
  lookup. Steam login without this key still works (OpenID itself needs no
  API key) but shows the raw SteamID64 instead of a persona name.
- `STEAM_ALLOWED_USERS` — comma-separated raw SteamID64s, same
  empty-means-unrestricted semantics as `GITHUB_ALLOWED_USERS`.

Changed:

- `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET` become optional (previously
  required). Startup fails only if **neither** GitHub (`GITHUB_CLIENT_ID` +
  `GITHUB_CLIENT_SECRET` both set) **nor** Steam (`STEAM_API_KEY` set, or
  Steam login is otherwise force-enabled — see Open Question below) is
  configured.
- `ADMIN_USERS` — same env var, but values are now expected to be prefixed
  (`github:octocat,steam:76561198012345678`). Existing deployments with
  unprefixed `ADMIN_USERS` values need to add the `github:` prefix on
  upgrade — call this out prominently in the README and changelog-style
  note, since it's a silent-failure risk otherwise (an unprefixed admin
  username just quietly stops matching, demoting that admin to a regular
  user rather than erroring).

## Display names

Steam's persona name is not persisted anywhere. It's resolved once at login
time (via `GetPlayerSummaries`, only if `STEAM_API_KEY` is set) and signed
into the session cookie alongside the identity string — the same
`itsdangerous`-signed payload `admin_auth.py` already uses, just carrying an
extra field. It naturally refreshes every time the cookie is renewed (7-day
`SESSION_MAX_AGE`). For GitHub, display name is always identical to the
unprefixed login — no behavior change.

`admin_auth.get_session_user(request) -> str | None` keeps its existing
signature and behavior (returns the prefixed identity, used everywhere access
control cares about identity) — every existing call site is untouched. A new
`admin_auth.get_session_display_name(request) -> str | None` decodes the same
cookie's extra field; only `GET /api/me` calls it.

**Deliberate scope limit:** the webui's server table "Owner" column
continues to show the raw prefixed identity (e.g. `steam:76561198012345678`),
not a resolved persona name. Resolving a display name for every row of a
table would need its own caching layer (a live Steam API call per row shown
is not acceptable); that's real added infrastructure this feature doesn't
need. Only the *currently logged-in user's own* display name — already
carried for free in their session cookie — gets shown nicely, in the nav bar
and the Account page.

## Identity provider abstraction

New module `packages/aggregator/src/aggregator/identity_providers.py`:

```python
@dataclass
class ProviderResult:
    username: str        # prefixed identity, e.g. "github:octocat" / "steam:76561198012345678"
    display_name: str    # persona name (Steam) or login (GitHub, same as username's suffix)

class IdentityProvider(Protocol):
    slug: str  # "github" / "steam" -- used in the callback path and cookie naming
    def is_configured(self) -> bool: ...
    def login_redirect(self, state: str) -> RedirectResponse: ...
    async def resolve_callback(self, request: Request) -> ProviderResult | None: ...  # None = failed/denied
```

`GitHubProvider` wraps the existing OAuth 2.0 exchange verbatim (moved, not
rewritten) and prefixes its result with `"github:"`. `SteamProvider` is new:
`login_redirect` builds the OpenID 2.0 `checkid_setup` redirect to
`steamcommunity.com/openid/login` (realm = `https://{MCP_DOMAIN}`, no
secret). `resolve_callback` performs the `check_authentication` verification
POST described above, extracts the SteamID64 from `openid.claimed_id`, and —
if `STEAM_API_KEY` is set — calls `GetPlayerSummaries` for the persona name
(falls back to the raw SteamID64 as `display_name` if unset or the call
fails).

Neither provider knows about allowlists. `access_control.py` gains
`is_allowed(username: str) -> bool`, which splits the prefix and checks the
matching raw allowlist (`GITHUB_ALLOWED_USERS` or `STEAM_ALLOWED_USERS`,
empty = unrestricted for that provider) — the single place this dispatch
happens. `is_admin` needs no change: it already just checks prefixed-string
membership in `ADMIN_USERS`. Every existing "recheck against the allowlist on
every request" call site (`admin_auth.get_session_user`,
`access_control.validate_personal_token`'s `GITHUB_ALLOWED_USERS` check added
in the prior feature's final review) switches from a GitHub-specific check to
`access_control.is_allowed(username)`.

## Auth flow wiring

### Admin browser session (`admin_auth.py`)

`/admin/login/github` (existing) and a new `/admin/login/steam` both set the
same `admin_oauth_state` CSRF cookie (unchanged mechanism, just no longer
GitHub-specific in name or intent) before calling the matching provider's
`login_redirect()`. GitHub's callback stays at `/oauth/callback` — **no
change to registered GitHub OAuth App callback URLs**. A new
`/oauth/callback/steam` route handles Steam's OpenID response and checks the
same `admin_oauth_state` cookie to decide whether it's servicing the admin
flow or the MCP PKCE flow below, exactly mirroring today's GitHub callback's
discriminator logic.

### MCP client PKCE flow (`oauth.py` / `api/oauth_router.py`)

Today, `/authorize` redirects straight to GitHub with no user choice — the
PKCE client (Claude Web UI, etc.) doesn't know about identity providers, only
the person in the browser window does.

- **Exactly one provider configured:** `/authorize` behaves exactly as
  today — immediate redirect to that provider, zero added friction for
  single-provider deployments (which, immediately after this ships, is
  everyone who hasn't set up Steam).
- **Both configured:** `/authorize` shows a minimal "Continue with GitHub" /
  "Continue with Steam" interstitial before redirecting — the same simple
  page the webui's login screen uses, parameterized to carry the pending
  PKCE session through to whichever provider the user picks instead of into
  an admin session.

`_authorize_handler` (`oauth_router.py`) changes from hardcoding the GitHub
redirect to resolving the configured provider(s) and either redirecting
directly or rendering the chooser.

## Webui

- **Login page** (`LoginPage.tsx`): shows a button per configured provider.
  Needs to know which are configured before rendering — new unauthenticated
  `GET /api/auth/providers` → `{"github": true, "steam": false}`.
- **Account page** (`AccountPage.tsx`): `GET /api/me` gains a `display_name`
  field (alongside existing `username`/`is_admin`), shown in the nav bar and
  on the Account page in place of the raw (possibly prefixed) username.
  Personal-token generation is already identity-agnostic (just signs
  whatever `username` the session carries) — no change needed there.
- **ServerTable**: Owner column is unchanged — still shows the raw prefixed
  identity, per the deliberate scope limit above.

## Docs & config

- README: new "Steam login" setup section (creating a Steam Web API key,
  setting `STEAM_API_KEY`/`STEAM_ALLOWED_USERS`); update the auth-model
  diagram and prose to describe both providers; update `ADMIN_USERS`
  examples to the prefixed format; a clear upgrade note about existing
  unprefixed `ADMIN_USERS` values needing the `github:` prefix added.
- `.env.example`, `docker-compose.yml`, `scripts/init-env.sh`: add
  `STEAM_API_KEY`/`STEAM_ALLOWED_USERS`; mark `GITHUB_CLIENT_ID`/`SECRET` as
  optional in comments.

## Testing

Following the project's convention of exercising real local servers instead
of mocking transport (`tests/conftest.py`'s `proxy_target_url` fixture) —
with one necessary exception:

- `identity_providers.py`: unit tests for `SteamProvider.resolve_callback`.
  There is no local Steam to talk to, so this is the one place mocking the
  external HTTP call is the right tool, not a shortcut — specifically:
  a forged callback (valid-looking `openid.*` params, but a mocked
  `check_authentication` response of `is_valid:false`) must be rejected.
  Also cover the happy path and the `STEAM_API_KEY`-unset fallback
  (SteamID64 as `display_name`).
- `access_control.is_allowed`: the admin/regular/not-allowed ×
  github-prefix/steam-prefix matrix.
- `GET /api/auth/providers` reflects actual configured-provider state
  (test with only `GITHUB_CLIENT_ID`/`SECRET` set, only `STEAM_API_KEY` set,
  and both).
- `/authorize`'s single-provider-vs-chooser branching: with one provider
  configured, confirm the immediate redirect (existing test coverage,
  updated); with both, confirm the chooser page renders instead.
- `OAuthToken` rename: a test creating a legacy-shaped `oauth_tokens` table
  (mirroring `test_database.py`'s existing `_migrate_server_columns` legacy
  test pattern) and confirming `init_db()` drops and recreates it cleanly.

No new frontend test suite, consistent with prior features in this project.

## Steam enablement flag

`STEAM_API_KEY`'s presence is the sole toggle for "Steam login is enabled" —
no separate `STEAM_LOGIN_ENABLED`-style flag. One fewer env var to document
and get out of sync; a deployer who wants Steam login without persona names
can still obtain a free API key and simply not think about it further. The
startup check ("at least one provider configured") is therefore: GitHub
configured (`GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` both set) OR Steam
configured (`STEAM_API_KEY` set).
