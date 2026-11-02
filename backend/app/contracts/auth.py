from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr, StrictStr


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    login: StrictStr
    password: SecretStr


class UserInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    login: str
    role: Literal["user", "admin"]


class AuthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    user: UserInfo
    expires_at: datetime
    csrf_token: str
