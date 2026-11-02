import argparse
import asyncio
from datetime import UTC, datetime
from getpass import getpass
from uuid import uuid4

from sqlalchemy import select

from app.auth.passwords import hasher
from app.core.config import Settings
from app.db.models import User
from app.db.session import create_sessions


async def create_user(login: str, role: str, password: str) -> None:
    sessions = create_sessions(Settings())
    try:
        async with sessions() as db, db.begin():
            if await db.scalar(select(User).where(User.login == login)):
                raise ValueError("Учётная запись уже существует")
            db.add(
                User(
                    id=uuid4(),
                    login=login,
                    role=role,
                    password_hash=hasher.hash(password),
                    active=True,
                    created_at=datetime.now(UTC),
                )
            )
    finally:
        await sessions.kw["bind"].dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Создание учётной записи")
    parser.add_argument("login")
    parser.add_argument("--role", choices=["user", "admin"], required=True)
    args = parser.parse_args()
    password = getpass("Пароль: ")
    if not password or password != getpass("Повторите пароль: "):
        raise SystemExit("Пароли пусты или не совпадают")
    asyncio.run(create_user(args.login, args.role, password))
    print("Учётная запись создана.")


if __name__ == "__main__":
    main()
