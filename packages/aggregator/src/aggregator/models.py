import json
import time as _time
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class ServerType(str, Enum):
    PYPI = "pypi"
    NPM = "npm"
    GIT = "git"
    CMD = "cmd"
    PROXY = "proxy"


class Server(SQLModel, table=True):
    __tablename__ = "servers"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    type: str
    package: str
    args: str = Field(default="[]")   # JSON array
    env: str = Field(default="{}")    # JSON object
    enabled: bool = Field(default=True)

    def get_args(self) -> list[str]:
        return json.loads(self.args)

    def get_env(self) -> dict[str, str]:
        return json.loads(self.env)


class OAuthToken(SQLModel, table=True):
    __tablename__ = "oauth_tokens"

    token: str = Field(primary_key=True)
    token_type: str
    github_user: str
    client_id: str
    expires_at: float
    created_at: float = Field(default_factory=_time.time)
