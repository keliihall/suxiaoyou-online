"""Explicit cloud administration: python -m app.cloud.manage migrate/create-admin."""

import argparse
import asyncio
import getpass
import os

from sqlalchemy import select

from app.cloud.config import CloudSettings
from app.cloud.database import Database, migrate
from app.cloud.identity import create_user, lock_identity, normalize_email
from app.cloud.models import User


async def run(args) -> str:
    settings = CloudSettings()
    if args.command == "migrate":
        await migrate(settings.database_url)
        return "Cloud schema is at the latest revision."
    database = Database(settings)
    try:
        await database.initialize()
        email = normalize_email(args.email)
        async with database.sessions() as db:
            await lock_identity(db)
            user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if user:
                if user.role != "admin" or not user.is_active:
                    raise ValueError("Email already belongs to a non-active-admin user; no changes made")
                return "Active administrator already exists; no changes made."
            password = os.environ.get(args.password_env, "")
            if not password:
                password = getpass.getpass("New administrator password: ")
                if password != getpass.getpass("Repeat password: "):
                    raise ValueError("Passwords do not match")
            await create_user(db, email=email, password=password, display_name=args.display_name or email.split("@", 1)[0], role="admin", token_limit=settings.default_token_limit)
            await db.commit()
            return "Administrator created."
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent cloud database and account management")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="Apply versioned cloud migrations; safe to run repeatedly")
    admin = subparsers.add_parser("create-admin", help="Create a bootstrap administrator without resetting an existing account")
    admin.add_argument("--email", required=True)
    admin.add_argument("--display-name", default="")
    admin.add_argument("--password-env", default="SUXIAOYOU_CLOUD_ADMIN_PASSWORD", help="Read password from this environment variable, or prompt securely")
    args = parser.parse_args()
    try:
        print(asyncio.run(run(args)))
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
