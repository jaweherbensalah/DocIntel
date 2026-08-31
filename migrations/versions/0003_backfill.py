"""backfill: copy existing JSON payloads into the normalised tables

Batched, idempotent and non-destructive; see DECISIONS.md for why.

Revision ID: 0003_backfill
Revises: 0002_expand
Create Date: 2026-08-31
"""

import json
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_backfill"
down_revision: Union[str, None] = "0002_expand"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BATCH_SIZE = 500

logger = logging.getLogger("alembic.backfill")

profiles = sa.table(
    "profiles",
    sa.column("id", sa.Integer),
    sa.column("result_id", sa.String),
    sa.column("name", sa.String),
    sa.column("years_experience", sa.Integer),
)
profile_skills = sa.table(
    "profile_skills",
    sa.column("profile_id", sa.Integer),
    sa.column("skill", sa.String),
)
matches = sa.table(
    "matches",
    sa.column("result_id", sa.String),
    sa.column("rank", sa.Integer),
    sa.column("candidate_name", sa.String),
    sa.column("score", sa.Integer),
    sa.column("job_description", sa.Text),
    sa.column("rationale", sa.Text),
    sa.column("matched_skills", sa.JSON),
    sa.column("missing_skills", sa.JSON),
)

SELECT_BATCH = sa.text(
    """
    SELECT id, kind, payload
    FROM results
    WHERE id > :after
      AND payload IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM profiles p WHERE p.result_id = results.id)
      AND NOT EXISTS (SELECT 1 FROM matches m WHERE m.result_id = results.id)
    ORDER BY id
    LIMIT :limit
    """
)


def _insert_profile(bind, result_id: str, data: dict) -> None:
    bind.execute(
        sa.insert(profiles).values(
            result_id=result_id,
            name=(data.get("name") or "")[:200] or None,
            years_experience=int(data.get("years_experience") or 0),
        )
    )
    profile_id = bind.execute(
        sa.text("SELECT id FROM profiles WHERE result_id = :rid"), {"rid": result_id}
    ).scalar_one()

    skills = list(dict.fromkeys(s[:64] for s in (data.get("skills") or []) if s))
    if skills:
        bind.execute(
            sa.insert(profile_skills),
            [{"profile_id": profile_id, "skill": s} for s in skills],
        )


def _insert_matches(bind, result_id: str, kind: str, data: dict) -> None:
    if kind == "match":
        rows = [
            {
                "result_id": result_id,
                "rank": None,
                "candidate_name": None,
                "score": int(data.get("score") or 0),
                # legacy match payloads never stored the job description
                "job_description": "",
                "rationale": data.get("rationale") or "",
                "matched_skills": data.get("matched_skills") or [],
                "missing_skills": data.get("missing_skills") or [],
            }
        ]
    else:
        job = data.get("job_description") or ""
        rows = [
            {
                "result_id": result_id,
                "rank": entry.get("rank"),
                "candidate_name": (entry.get("name") or "")[:200] or None,
                "score": int(entry.get("score") or 0),
                "job_description": job,
                "rationale": entry.get("rationale") or "",
                "matched_skills": entry.get("matched_skills") or [],
                "missing_skills": entry.get("missing_skills") or [],
            }
            for entry in (data.get("shortlist") or [])
        ]

    if rows:
        bind.execute(sa.insert(matches), rows)


def upgrade() -> None:
    bind = op.get_bind()
    after = ""
    migrated = skipped = 0

    with op.get_context().autocommit_block():
        while True:
            batch = bind.execute(
                SELECT_BATCH, {"after": after, "limit": BATCH_SIZE}
            ).fetchall()
            if not batch:
                break

            for result_id, kind, payload in batch:
                try:
                    data = json.loads(payload)
                except (TypeError, ValueError):
                    logger.warning("unparseable payload on result %s", result_id)
                    skipped += 1
                    continue

                if not isinstance(data, dict):
                    skipped += 1
                    continue

                if kind == "extract":
                    _insert_profile(bind, result_id, data)
                elif kind in ("match", "batch_match"):
                    _insert_matches(bind, result_id, kind, data)
                else:
                    skipped += 1
                    continue
                migrated += 1

            # advance even if every row was skipped, so the loop terminates
            after = batch[-1][0]

    logger.info("backfill complete: %s migrated, %s skipped", migrated, skipped)


def downgrade() -> None:
    # No-op: rows written since the backfill are indistinguishable from
    # backfilled ones, so deleting them would lose data.
    pass
