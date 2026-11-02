"""One-shot local bootstrap: migrations, limited runtime grants, local UI accounts."""

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from app.auth.passwords import check_password, hasher
from app.core.config import Settings
from app.db.models import User
from app.db.session import create_sessions


async def initialize(password: str, accounts: list[dict[str, str]], grants: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", password):
        raise ValueError("Invalid runtime role credential")
    if not isinstance(accounts, list) or len(accounts) != 2:
        raise ValueError("Expected two local frontend accounts")
    for account, role in zip(accounts, ("user", "admin"), strict=True):
        if (
            account.get("role") != role
            or account.get("login") != f"frontend_{role}"
            or not isinstance(account.get("password"), str)
            or not account["password"]
        ):
            raise ValueError("Invalid local account profile")
    sessions = create_sessions(Settings())
    try:
        async with sessions() as db, db.begin():
            exists = await db.scalar(text("SELECT 1 FROM pg_roles WHERE rolname='gis_runtime'"))
            operation = "ALTER" if exists else "CREATE"
            # Only the validated hex value is interpolated, never arbitrary input.
            await db.execute(text(f"{operation} ROLE gis_runtime LOGIN PASSWORD '{password}'"))
            grants = "\n".join(
                line for line in grants.splitlines() if not line.strip().startswith("--")
            )
            for statement in grants.split(";"):
                if statement.strip():
                    await db.execute(text(statement))
            for account in accounts:
                existing = await db.scalar(select(User).where(User.login == account["login"]))
                if existing is not None:
                    if (
                        not existing.active
                        or existing.role != account["role"]
                        or not check_password(existing.password_hash, account["password"])
                    ):
                        raise ValueError("Existing account differs; refusing to reset it")
                    continue
                db.add(
                    User(
                        id=uuid4(),
                        login=account["login"],
                        role=account["role"],
                        password_hash=hasher.hash(account["password"]),
                        active=True,
                        created_at=datetime.now(UTC),
                    )
                )
        print("Local migrations, runtime grants and frontend accounts are ready.")
    finally:
        await sessions.kw["bind"].dispose()


def main() -> None:
    try:
        command.upgrade(Config("/app/alembic.ini"), "head")
        password = Path("/run/secrets/db_runtime_password").read_text().strip()
        accounts = json.loads(Path("/run/secrets/frontend_accounts").read_text())
        grants = Path("/ops/grant-runtime.sql").read_text()
        asyncio.run(initialize(password, accounts, grants))
    except Exception as error:
        # Do not render SQL statements, private connection URLs or user passwords.
        raise SystemExit(
            f"LOCAL_SETUP_FAILED ({type(error).__name__}); check local setup files."
        ) from None


if __name__ == "__main__":
    main()
