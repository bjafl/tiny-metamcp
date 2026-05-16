import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "aggregator.db"
PACKAGES_DIR = DATA_DIR / "packages"
LOGS_DIR = DATA_DIR / "logs"

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PORT = int(os.getenv("PORT", "8000"))

MCP_DOMAIN = os.getenv("MCP_DOMAIN", "localhost")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
GITHUB_ALLOWED_USERS: set[str] = {
    u.strip() for u in os.getenv("GITHUB_ALLOWED_USERS", "").split(",") if u.strip()
}
