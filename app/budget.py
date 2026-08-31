"""Per-client spend control.

Money lives in Postgres rather than Redis: a lost counter here is a lost
invoice. Correctness under concurrency comes from a single conditional UPDATE,
which takes a row lock, so simultaneous requests serialise on it and each one
re-evaluates the budget against the committed total.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# A request is charged for what it asks the model to read. Rough, but the
# reservation only has to be a safe upper bound: settlement corrects it.
BASE_CENTS = 1
CENTS_PER_1K_CHARS = 2


class BudgetExceeded(Exception):
    def __init__(self, limit_cents: int, period: str):
        self.limit_cents = limit_cents
        self.period = period
        super().__init__("monthly budget exhausted")


@dataclass
class Reservation:
    event_id: str
    client_id: str
    period: str
    estimated_cents: int


def current_period(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def estimate_cents(operation: str, characters: int, units: int = 1) -> int:
    per_unit = BASE_CENTS + (characters * CENTS_PER_1K_CHARS + 999) // 1000
    return max(1, per_unit * max(units, 1))


_ENSURE_PERIOD = text(
    """
    INSERT INTO budget_periods (client_id, period, reserved_cents, spent_cents)
    VALUES (:client_id, :period, 0, 0)
    ON CONFLICT (client_id, period) DO NOTHING
    """
)

# The whole check-and-debit is this one statement. Doing it as SELECT then
# UPDATE would let two requests both read an affordable balance and both spend.
_RESERVE = text(
    """
    UPDATE budget_periods
    SET reserved_cents = reserved_cents + :cost
    WHERE client_id = :client_id
      AND period = :period
      AND spent_cents + reserved_cents + :cost <= :limit
    """
)

_SETTLE = text(
    """
    UPDATE budget_periods
    SET reserved_cents = reserved_cents - :estimated,
        spent_cents = spent_cents + :actual
    WHERE client_id = :client_id AND period = :period
    """
)

_RELEASE = text(
    """
    UPDATE budget_periods
    SET reserved_cents = reserved_cents - :estimated
    WHERE client_id = :client_id AND period = :period
    """
)


async def reserve(
    session: AsyncSession,
    client_id: str,
    limit_cents: int,
    operation: str,
    cost_cents: int,
) -> Reservation:
    period = current_period()
    await session.execute(_ENSURE_PERIOD, {"client_id": client_id, "period": period})

    updated = await session.execute(
        _RESERVE,
        {
            "client_id": client_id,
            "period": period,
            "cost": cost_cents,
            "limit": limit_cents,
        },
    )
    if updated.rowcount != 1:
        await session.rollback()
        raise BudgetExceeded(limit_cents, period)

    event_id = uuid.uuid4().hex
    await session.execute(
        text(
            "INSERT INTO usage_events "
            "(id, client_id, period, operation, estimated_cents, state) "
            "VALUES (:id, :client_id, :period, :operation, :estimated, 'reserved')"
        ),
        {
            "id": event_id,
            "client_id": client_id,
            "period": period,
            "operation": operation,
            "estimated": cost_cents,
        },
    )
    await session.commit()
    return Reservation(event_id, client_id, period, cost_cents)


def settle_sync(session, event_id: str, actual_cents: int) -> bool:
    """Convert a reservation into a real charge. Safe to call twice."""
    row = session.execute(
        text(
            "SELECT client_id, period, estimated_cents FROM usage_events "
            "WHERE id = :id AND state = 'reserved'"
        ),
        {"id": event_id},
    ).first()
    if row is None:
        return False

    session.execute(
        _SETTLE,
        {
            "client_id": row.client_id,
            "period": row.period,
            "estimated": row.estimated_cents,
            "actual": actual_cents,
        },
    )
    session.execute(
        text(
            "UPDATE usage_events SET state = 'settled', actual_cents = :actual "
            "WHERE id = :id AND state = 'reserved'"
        ),
        {"id": event_id, "actual": actual_cents},
    )
    session.commit()
    return True


def release_sync(session, event_id: str) -> bool:
    """Give the money back when the work never happened."""
    row = session.execute(
        text(
            "SELECT client_id, period, estimated_cents FROM usage_events "
            "WHERE id = :id AND state = 'reserved'"
        ),
        {"id": event_id},
    ).first()
    if row is None:
        return False

    session.execute(
        _RELEASE,
        {
            "client_id": row.client_id,
            "period": row.period,
            "estimated": row.estimated_cents,
        },
    )
    session.execute(
        text(
            "UPDATE usage_events SET state = 'released', actual_cents = 0 "
            "WHERE id = :id AND state = 'reserved'"
        ),
        {"id": event_id},
    )
    session.commit()
    return True


async def settle(session: AsyncSession, event_id: str, actual_cents: int) -> None:
    row = (
        await session.execute(
            text(
                "SELECT client_id, period, estimated_cents FROM usage_events "
                "WHERE id = :id AND state = 'reserved'"
            ),
            {"id": event_id},
        )
    ).first()
    if row is None:
        return

    await session.execute(
        _SETTLE,
        {
            "client_id": row.client_id,
            "period": row.period,
            "estimated": row.estimated_cents,
            "actual": actual_cents,
        },
    )
    await session.execute(
        text(
            "UPDATE usage_events SET state = 'settled', actual_cents = :actual "
            "WHERE id = :id AND state = 'reserved'"
        ),
        {"id": event_id, "actual": actual_cents},
    )
    await session.commit()
