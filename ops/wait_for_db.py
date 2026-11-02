"""Wait for a disposable CI database, fail within 60 seconds."""

import asyncio
import os
import time

import asyncpg


async def main() -> None:
    deadline = time.monotonic() + 60
    url = os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    while True:
        try:
            connection = await asyncpg.connect(url, timeout=3)
            await connection.close()
            return
        except (OSError, asyncpg.PostgresError):
            if time.monotonic() >= deadline:
                raise SystemExit("Test database did not become ready") from None
            await asyncio.sleep(1)


asyncio.run(main())
