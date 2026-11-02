"""Explicit initial load; never runs automatically on API startup."""

import argparse
import asyncio

from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.errors import ApiError
from app.data_admin.demo_service import install_demo
from app.db.session import create_sessions


async def initialize() -> None:
    settings = Settings()
    sessions = create_sessions(settings)
    try:
        result = await install_demo(settings, sessions, initialize=True)
        print(result.model_dump_json())
    finally:
        await sessions.kw["bind"].dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Первоначальная загрузка доверенного демонабора")
    parser.add_argument("command", choices=["initialize"])
    parser.parse_args()
    try:
        asyncio.run(initialize())
    except ApiError as error:
        raise SystemExit(f"{error.code}: {error.message}") from None
    except (SQLAlchemyError, ConnectionError, OSError):
        raise SystemExit("DATABASE_UNAVAILABLE: Не удалось подключиться к базе данных.") from None


if __name__ == "__main__":
    main()
