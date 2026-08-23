import json
import time as _time
from enum import StrEnum

from sqlmodel import Field, SQLModel


class ServerType(StrEnum):
    PYPI = "pypi"
    NPM = "npm"
    GIT = "git"
    CMD = "cmd"
    PROXY = "proxy"


class ServerVisibility(StrEnum):
    EVERYONE = "everyone"
    PRIVATE = "private"


class Server(SQLModel, table=True):
    __tablename__ = "servers"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    type: str
    package: str
    args: str = Field(default="[]")  # JSON array
    env: str = Field(default="{}")  # JSON object
    enabled: bool = Field(default=True)
    owner_username: str | None = Field(default=None)
    # Model-level default is "everyone" for migration parity only (see
    # database._migrate_server_columns) -- new-server creation paths must
    # pass visibility explicitly (database.add_server defaults to PRIVATE).
    visibility: str = Field(default=ServerVisibility.EVERYONE.value)

    def get_args(self) -> list[str]:
        return json.loads(self.args)

    def get_env(self) -> dict[str, str]:
        return json.loads(self.env)


class OAuthToken(SQLModel, table=True):
    __tablename__ = "oauth_tokens"

    token: str = Field(primary_key=True)
    token_type: str
    username: str
    client_id: str
    expires_at: float
    created_at: float = Field(default_factory=_time.time)


class PersonalToken(SQLModel, table=True):
    __tablename__ = "personal_tokens"

    username: str = Field(primary_key=True)
    token_hash: str = Field(unique=True, index=True)
    created_at: float = Field(default_factory=_time.time)
