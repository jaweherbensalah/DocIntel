"""Client administration: create clients and seed a development one.

    python -m app.admin create "Acme" --budget-cents 50000 --rate 120
    python -m app.admin seed
"""

import argparse
import hashlib
import secrets
import sys
import uuid

from sqlalchemy import select

from app.config import settings
from app.models import Client
from app.sync_db import SyncSessionLocal


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def create_client(name: str, budget_cents: int, rate_per_minute: int) -> str:
    api_key = secrets.token_urlsafe(32)
    with SyncSessionLocal() as session:
        session.add(
            Client(
                id=uuid.uuid4().hex,
                name=name,
                api_key_hash=hash_key(api_key),
                monthly_budget_cents=budget_cents,
                rate_limit_per_minute=rate_per_minute,
            )
        )
        session.commit()
    return api_key


def seed_default() -> bool:
    """Register the API_KEY from the environment as a development client."""
    if not settings.api_key:
        return False

    key_hash = hash_key(settings.api_key)
    with SyncSessionLocal() as session:
        exists = session.scalar(
            select(Client.id).where(Client.api_key_hash == key_hash)
        )
        if exists:
            return False
        session.add(
            Client(
                id=uuid.uuid4().hex,
                name="development",
                api_key_hash=key_hash,
                monthly_budget_cents=settings.default_monthly_budget_cents,
                rate_limit_per_minute=settings.default_rate_limit_per_minute,
            )
        )
        session.commit()
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="app.admin")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("create", help="create a client and print its API key")
    new.add_argument("name")
    new.add_argument("--budget-cents", type=int, default=50_000)
    new.add_argument("--rate", type=int, default=60)

    sub.add_parser("seed", help="register API_KEY as a development client")

    args = parser.parse_args(argv)

    if args.command == "create":
        key = create_client(args.name, args.budget_cents, args.rate)
        print(f"client created. api key (shown once): {key}")
    else:
        print("seeded development client" if seed_default() else "already seeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
