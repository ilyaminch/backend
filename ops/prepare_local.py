"""Prepare local-only credentials and pinned MLChat context; no Git/Docker commands."""

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"


def private_file(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def prepare(source: Path) -> None:
    reference = json.loads((ROOT / "ops/mlchat-reference.json").read_text(encoding="utf-8"))
    # Verify everything before copying anything. Never send MLChat's entire worktree,
    # .git, ignored credentials or local experiments to the Docker daemon.
    contents = {}
    for name, expected in reference["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid source path in MLChat reference")
        content = (source / relative).read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f"MLChat source changed: {name}; review the reference before updating")
        contents[relative] = content
    LOCAL.mkdir(exist_ok=True)
    context = LOCAL / "mlchat-source"
    # Do not reuse a stale/expanded context from another package revision.
    existing = {p.relative_to(context) for p in context.rglob("*") if p.is_file()}
    if existing - contents.keys():
        raise ValueError("Unexpected files in .local/mlchat-source; inspect them before building")
    for relative, content in contents.items():
        destination = context / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)

    files = [
        "db_owner_password",
        "db_runtime_password",
        "owner.env",
        "runtime.env",
        "frontend_accounts.json",
    ]
    present = [(LOCAL / name).exists() for name in files]
    if any(present) and not all(present):
        raise ValueError("Incomplete .local credentials; recover the existing set, do not reset DB")
    if not any(present):
        owner_password = secrets.token_hex(32)
        runtime_password = secrets.token_hex(32)
        owner_url = f"postgresql+asyncpg://gis_migrator:{owner_password}@db:5432/gis"
        runtime_url = f"postgresql+asyncpg://gis_runtime:{runtime_password}@db:5432/gis"
        private_file(LOCAL / "db_owner_password", owner_password + "\n")
        private_file(LOCAL / "db_runtime_password", runtime_password + "\n")
        private_file(
            LOCAL / "owner.env", f"DATABASE_URL={owner_url}\nMIGRATION_DATABASE_URL={owner_url}\n"
        )
        private_file(LOCAL / "runtime.env", f"DATABASE_URL={runtime_url}\n")
        private_file(
            LOCAL / "frontend_accounts.json",
            json.dumps(
                [
                    {
                        "login": "frontend_user",
                        "role": "user",
                        "password": secrets.token_urlsafe(24),
                    },
                    {
                        "login": "frontend_admin",
                        "role": "admin",
                        "password": secrets.token_urlsafe(24),
                    },
                ],
                indent=2,
            )
            + "\n",
        )
    # Non-secret record of the exact MLChat delivery used by this local image.
    (LOCAL / "mlchat-build.json").write_text(
        json.dumps(reference, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Local files prepared. Existing credentials preserved; no passwords printed.")
    print(f"Frontend accounts: {LOCAL / 'frontend_accounts.json'} (local only)")
    print(f"MLChat revision: {reference['revision']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mlchat-dir", type=Path, default=ROOT.parent / "mlchat")
    args = parser.parse_args()
    try:
        prepare(args.mlchat_dir.resolve())
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
