"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Rules (docs/10 §3, CLAUDE.md rules 1, 9 and 12):
* Hand-written SQL via op.execute(); no autogenerate, no ORM metadata.
* Every tenant table: workspace_id, composite FKs, RLS ENABLE + FORCE + policies, explicit grants
  (column-level UPDATE where required, S7) in THIS revision.
* No role creation, no passwords, no data from any real environment.
* downgrade() drops exactly what upgrade() created. It runs only in dev/test/drill (S9);
  staging and production use forward-fix revisions.
"""

from collections.abc import Sequence

from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    ${upgrades if upgrades else 'raise NotImplementedError("write the upgrade SQL")'}


def downgrade() -> None:
    ${downgrades if downgrades else 'raise NotImplementedError("write the downgrade SQL")'}
