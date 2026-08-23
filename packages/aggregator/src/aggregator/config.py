import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "aggregator.db"
PACKAGES_DIR = DATA_DIR / "packages"
LOGS_DIR = DATA_DIR / "logs"
WEBUI_DIST_DIR = Path(os.getenv("WEBUI_DIST_DIR", "webui_dist"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PORT = int(os.getenv("PORT", "8000"))

MCP_DOMAIN = os.getenv("MCP_DOMAIN", "localhost")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-in-production")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
GITHUB_ALLOWED_USERS: set[str] = {
    u.strip() for u in os.getenv("GITHUB_ALLOWED_USERS", "").split(",") if u.strip()
}
ADMIN_USERS: set[str] = {u.strip() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()}
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
STEAM_ALLOWED_USERS: set[str] = {
    u.strip() for u in os.getenv("STEAM_ALLOWED_USERS", "").split(",") if u.strip()
}
