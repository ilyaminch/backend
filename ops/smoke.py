"""Application smoke; no live LLM or database is needed for liveness."""

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app

settings = Settings(database_url=SecretStr("postgresql+asyncpg://unused:unused@127.0.0.1/gis"))
with TestClient(create_app(settings)) as client:
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert client.get("/openapi.json").status_code == 200
