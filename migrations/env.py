"""Alembic environment for Telegram-Finance-Bot V2.

All policy lives in :func:`finbot.infra.db.migration_runtime.run_env` (typed and unit-tested):
owner-role settings only, guard SQL before any migration, downgrade restrictions (S9), revision
policy, no autogenerate. Do not add logic here.
"""

from alembic import context

from finbot.infra.db.migration_runtime import run_env

run_env(context)  # type: ignore[arg-type]  # alembic.context proxies EnvironmentContext
