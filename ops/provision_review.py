"""Provision the two explicitly requested review accounts through the owner connection."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.auth.passwords import check_password, hasher
from app.core.config import Settings
from app.db.models import User
from app.db.session import create_sessions


async def provision(accounts: list[dict[str, str]]) -> None:
    sessions = create_sessions(Settings())
    try:
        async with sessions() as db, db.begin():
            if len(accounts) != 2:
                raise ValueError("Expected two review accounts")
            for account, role in zip(accounts, ("user", "admin"), strict=True):
                if account["login"] != f"review_{role}" or account["role"] != role:
                    raise ValueError("Invalid review account profile")
                existing = await db.scalar(select(User).where(User.login == account["login"]))
                if existing is not None:
                    if (
                        not existing.active
                        or existing.role != role
                        or not check_password(existing.password_hash, account["password"])
                    ):
                        raise ValueError("Existing review account differs; refusing to reset")
                    continue
                db.add(
                    User(
                        id=uuid4(),
                        login=account["login"],
                        role=role,
                        password_hash=hasher.hash(account["password"]),
                        active=True,
                        created_at=datetime.now(UTC),
                    )
                )
        print("Review user/admin accounts ready; no passwords printed.")
    finally:
        await sessions.kw["bind"].dispose()


if __name__ == "__main__":
    try:
        asyncio.run(provision(json.loads(Path("/run/secrets/review_accounts").read_text())))
    except Exception as error:
        raise SystemExit(f"REVIEW_SETUP_FAILED ({type(error).__name__})") from None
