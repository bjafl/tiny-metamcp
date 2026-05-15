#!/usr/bin/env bash
set -euo pipefail

INTERACTIVE=false
[[ "${1:-}" == "-i" || "${1:-}" == "i" ]] && INTERACTIVE=true

if [ -f .env ]; then
    read -rp ".env eksisterer allerede. Overskriv? [y/N] " confirm
    [[ "${confirm,,}" == "y" ]] || { echo "Avbrutt."; exit 0; }
fi

ADMIN_TOKEN=$(openssl rand -hex 32)
COOKIE_SECRET=$(openssl rand -base64 24 | tr -d '\n')

if $INTERACTIVE; then
    echo ""
    read -rp "Domene (f.eks. mcp.eksempel.no): "         MCP_DOMAIN
    read -rp "GitHub Client ID: "                         GITHUB_CLIENT_ID
    read -rp "GitHub Client Secret: "                     GITHUB_CLIENT_SECRET
    read -rp "GitHub brukernavn(er) [kommaseparert]: "    GITHUB_ALLOWED_USERS
    read -rp "Log level [INFO]: "                         LOG_LEVEL_INPUT
    LOG_LEVEL="${LOG_LEVEL_INPUT:-INFO}"
    echo ""
else
    MCP_DOMAIN="CHANGE_ME"
    GITHUB_CLIENT_ID="CHANGE_ME"
    GITHUB_CLIENT_SECRET="CHANGE_ME"
    GITHUB_ALLOWED_USERS="CHANGE_ME"
    LOG_LEVEL="INFO"
fi

cat > .env << ENVEOF
# Generert av 'just init-env' – ikke commit denne filen.

# ── Domene ────────────────────────────────────────────────────────────────────
# Produksjon:  offentlig hostname eksponert av Traefik/Coolify
# Lokal test:  sett til "localhost" og legg til OAUTH2_PROXY_COOKIE_SECURE=false
MCP_DOMAIN=${MCP_DOMAIN}

# ── Hemmelige nøkler (autogenerert) ──────────────────────────────────────────
# Bearer-token for MCP-klienter (Claude Desktop o.l.) og intern proxy-autentisering.
ADMIN_TOKEN=${ADMIN_TOKEN}

# Signeringsnøkkel for oauth2-proxy sin session-cookie (24 bytes, base64-kodet).
COOKIE_SECRET=${COOKIE_SECRET}

# ── GitHub OAuth ──────────────────────────────────────────────────────────────
# Registrer OAuth-app på: https://github.com/settings/developers → OAuth Apps → New
# Callback URL (produksjon): https://${MCP_DOMAIN}/oauth2/callback
# Callback URL (lokal test): http://localhost:4180/oauth2/callback
GITHUB_CLIENT_ID=${GITHUB_CLIENT_ID}
GITHUB_CLIENT_SECRET=${GITHUB_CLIENT_SECRET}

# Kommaseparert liste med tillatte GitHub-brukernavn. IKKE la stå tom.
GITHUB_ALLOWED_USERS=${GITHUB_ALLOWED_USERS}

# ── Valgfritt ─────────────────────────────────────────────────────────────────
LOG_LEVEL=${LOG_LEVEL}

# Tillat alle e-postdomener (GitHub-brukernavn brukes for tilgangskontroll, ikke e-post)
OAUTH2_PROXY_EMAIL_DOMAINS=*

# Fjern kommentar ved lokal test uten HTTPS (oauth2-proxy krever ellers sikker cookie)
#OAUTH2_PROXY_COOKIE_SECURE=false
ENVEOF

echo ".env opprettet."
$INTERACTIVE || echo "Husk å fylle inn CHANGE_ME-verdiene før du starter."
