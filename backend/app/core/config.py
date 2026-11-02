from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: Literal["local", "test", "demo"] = "local"
    database_url: SecretStr
    public_origin: str = "http://localhost:8000"
    cookie_secure: bool = True
    auth_session_max_age_seconds: int = 28800
    # Operational defaults delegated by the user on 18.10.2026; not ChTZ constants.
    login_ip_attempts: int = Field(default=30, gt=0)
    login_ip_window_seconds: int = Field(default=60, gt=0)
    login_failed_attempts: int = Field(default=5, gt=0)
    login_failure_window_seconds: int = Field(default=300, gt=0)
    # Chat limits approved by the user on 18.10.2026.
    chat_message_max_characters: int = Field(default=8000, gt=0)
    chat_state_max_bytes: int = Field(default=131072, gt=0)
    chat_sessions_per_auth: int = Field(default=10, gt=0)
    chat_sessions_total: int = Field(default=1000, gt=0)
    chat_messages_per_session: int = Field(default=200, gt=0)
    chat_request_timeout_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)
    llm_response_max_bytes: int = Field(default=1048576, gt=0)
    # Import limits approved on 21.10.2026.
    import_max_file_bytes: int = Field(default=20971520, gt=0)
    import_max_request_bytes: int = Field(default=22020096, gt=0)
    import_max_features: int = Field(default=10000, gt=0)
    import_max_positions: int = Field(default=500000, gt=0)
    import_max_positions_per_geometry: int = Field(default=100000, gt=0)
    import_timeout_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)
    import_lock_timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    # Trusted deployment path only; never accepted from an HTTP request.
    demo_package_dir: Path | None = None
    # Installed trusted Python package, e.g. mlchat.integration:create_dialogue.
    dialogue_factory: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    llm_api_key: SecretStr | None = None
    llm_response_mode: Literal["text_json", "json_object", "json_schema"] = "text_json"

    @field_validator("llm_base_url")
    @classmethod
    def validate_llm_url(cls, value: str | None) -> str | None:
        from urllib.parse import urlsplit, urlunsplit

        if value is None:
            return None
        parsed = urlsplit(value)
        # Accessing port also rejects malformed/out-of-range values at startup.
        _ = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("LLM_BASE_URL должен быть HTTP(S) URL без секретов и параметров")
        path = parsed.path.rstrip("/")
        if not path.endswith("/v1"):
            path += "/v1"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    @field_validator("database_url")
    @classmethod
    def postgres_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("Требуется подключение PostgreSQL через asyncpg")
        return value

    @model_validator(mode="after")
    def validate_security(self) -> "Settings":
        from urllib.parse import urlparse

        origin = urlparse(self.public_origin)
        if origin.scheme not in {"http", "https"} or not origin.hostname:
            raise ValueError("PUBLIC_ORIGIN должен быть HTTP(S) origin")
        if origin.username or origin.password or origin.query or origin.fragment:
            raise ValueError("PUBLIC_ORIGIN не должен содержать секреты или параметры")
        if origin.path not in {"", "/"}:
            raise ValueError("PUBLIC_ORIGIN не должен содержать путь")
        self.public_origin = self.public_origin.rstrip("/")
        if not 1 <= self.auth_session_max_age_seconds <= 28800:
            raise ValueError("Сессия должна действовать не более 8 часов")
        if not self.cookie_secure and (
            self.app_env == "demo" or origin.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Незащищённая cookie допустима только локально")
        if self.app_env == "demo" and origin.scheme != "https":
            raise ValueError("Для демонстрации по сети требуется HTTPS")
        return self
