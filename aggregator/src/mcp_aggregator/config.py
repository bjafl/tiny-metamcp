import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "aggregator.db"
PACKAGES_DIR = DATA_DIR / "packages"

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PORT = int(os.getenv("PORT", "8000"))
