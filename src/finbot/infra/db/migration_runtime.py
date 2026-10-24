"""Runtime for Alembic's ``migrations/env.py`` (docs/10 §3, decisions S4, S9, S10).

``env.py`` only calls :func:`run_env`. Everything that makes migrations fail closed lives here, so
it is typed, linted and unit-tested:

* **Revision policy.** Revisions are hand-written with manual ids ``0001``, ``0002``, ... in
  files ``NNNN_<slug>.py``, forming one linear chain (no branches, merges or dependencies).
  Autogenerate is refused. The policy is checked before every run.
* **Guard SQL.** Before any migration, inside the migration transaction, a ``DO`` block checks
  the database name, that the session and current user are ``finbot_owner`` (not a superuser)
  and own the database, and that a ``platform_meta`` environment marker, once it exists, matches
  ``FINBOT_ENV``. The same SQL opens offline (``--sql``) output, so SQL reviewed or applied by
  hand is guarded as well. It also takes a transaction-scoped advisory lock (one migrator at a
  time) and sets ``lock_timeout``.
* **Downgrades (S9).** Refused before any step runs unless
  :attr:`MigrationSettings.downgrade_permitted`. There are three layers: the ``alembic`` CLI
  command name, the planned steps (checked before execution, online and offline), and a
  per-step callback as a backstop (the run is one transaction, so a refusal rolls back).
* **Credentials (S10).** The owner password comes from :class:`MigrationSettings` only.
"""

import asyncio
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from finbot.migration_settings import (
    MIGRATION_OWNER_ROLE,
    MigrationSettings,
    load_migration_settings,
)

if TYPE_CHECKING:
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory
    from sqlalchemy.engine import Connection

#: Revision id format and file name format (``file_template = %%(rev)s_%%(slug)s``).
REVISION_ID_RE = re.compile(r"^\d{4}$")
REVISION_FILE_RE = re.compile(r"^(?P<rev>\d{4})_[a-z0-9][a-z0-9_]{0,59}\.py$")

#: Key of the transaction-scoped advisory lock held while migrating ("finbot" in ASCII).
MIGRATION_LOCK_KEY = 0x66696E626F74

#: Bounded wait for table locks so a migration never queues behind traffic indefinitely.
MIGRATION_LOCK_TIMEOUT = "10s"

#: Config attribute names used by the CLI and tests to pass objects to env.py.
SETTINGS_ATTRIBUTE = "finbot_settings"
CONNECTION_ATTRIBUTE = "connection"

#: Prefix of every guard error, so callers can tell a refusal from other database errors.
GUARD_MESSAGE_PREFIX = "finbot-migrate guard:"

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class MigrationRefusedError(Exception):
    """A migration was refused by policy (never contains secret values)."""


# ---------------------------------------------------------------------------- revision policy


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    """The facts about one revision file that the policy checks."""

    filename: str
    revision: str
    down_revision: str | Sequence[str] | None
    branch_labels: Sequence[str] = ()
    depends_on: Sequence[str] = ()


def revision_policy_problems(records: Iterable[RevisionRecord]) -> list[str]:
    """Problems with a revision set; empty when it is one contiguous, linear, manual chain."""
    problems: list[str] = []
    by_id: dict[str, RevisionRecord] = {}
    for record in records:
        match = REVISION_FILE_RE.fullmatch(record.filename)
        if not REVISION_ID_RE.fullmatch(record.revision):
            problems.append(f"{record.filename}: revision id {record.revision!r} is not NNNN")
        if match is None:
            problems.append(f"{record.filename}: file name must be NNNN_<lowercase_slug>.py")
        elif match["rev"] != record.revision:
            problems.append(f"{record.filename}: file name does not start with its revision id")
        if record.branch_labels:
            problems.append(f"{record.filename}: branch labels are not allowed")
        if record.depends_on:
            problems.append(f"{record.filename}: depends_on is not allowed")
        if record.revision in by_id:
            problems.append(f"{record.filename}: duplicate revision id {record.revision}")
        by_id[record.revision] = record
    if problems:
        return problems
    for index, revision in enumerate(sorted(by_id), start=1):
        expected = f"{index:04d}"
        if revision != expected:
            problems.append(f"revision ids must be contiguous from 0001; expected {expected}")
            break
        expected_down = None if index == 1 else f"{index - 1:04d}"
        down = by_id[revision].down_revision
        if not (down is None or isinstance(down, str)):
            problems.append(f"{by_id[revision].filename}: merge revisions are not allowed")
        elif down != expected_down:
            problems.append(
                f"{by_id[revision].filename}: down_revision must be {expected_down!r}, not {down!r}"
            )
    return problems


def load_revision_records(script: "ScriptDirectory") -> list[RevisionRecord]:
    """Read the revision facts from an Alembic script directory."""
    records: list[RevisionRecord] = []
    for rev in script.walk_revisions():
        path = rev.path or ""
        records.append(
            RevisionRecord(
                filename=path.rsplit("/", 1)[-1],
                revision=rev.revision,
                down_revision=rev.down_revision,
                branch_labels=tuple(rev.branch_labels or ()),
                depends_on=tuple(rev.dependencies or ()),
            )
        )
    return records


def check_revision_policy(script: "ScriptDirectory") -> None:
    """Raise :class:`MigrationRefusedError` if the script directory violates the policy."""
    problems = revision_policy_problems(load_revision_records(script))
    if problems:
        raise MigrationRefusedError("revision policy: " + "; ".join(problems))


# ----------------------------------------------------------------------------------- guard SQL


def _sql_literal(value: str) -> str:
    """Quote a validated identifier-like value as an SQL string literal."""
    if not _IDENTIFIER_RE.fullmatch(value):
        raise MigrationRefusedError(f"unsafe value for guard SQL: {value!r}")
    return f"'{value}'"


def guard_sql(settings: MigrationSettings) -> list[str]:
    """Statements that must succeed, in the migration transaction, before any migration runs.

    One statement per item (asyncpg prepares each statement, and offline output appends ``;``).
    No ``%`` and no ``$n`` placeholders appear, so the offline SQL runs unchanged under psql.
    """
    db = _sql_literal(settings.db_name)
    env = _sql_literal(settings.env)
    owner = _sql_literal(MIGRATION_OWNER_ROLE)
    p = GUARD_MESSAGE_PREFIX
    # settings.env is a validated Literal and passed _sql_literal above (no injection).
    marker_query = (
        "SELECT EXISTS (SELECT 1 FROM public.platform_meta "  # noqa: S608
        f"WHERE key = ''environment'' AND value = ''{settings.env}'')"
    )
    guard = f"""DO $finbot_guard$
DECLARE
  marker_ok boolean;
BEGIN
  IF pg_catalog.current_database() <> {db} THEN
    RAISE EXCEPTION USING MESSAGE = '{p} connected to database '
      || pg_catalog.current_database() || ', expected ' || {db};
  END IF;
  IF session_user <> {owner} OR current_user <> {owner} THEN
    RAISE EXCEPTION USING MESSAGE = '{p} must run as ' || {owner} || ', not '
      || current_user || ' (session ' || session_user || ')';
  END IF;
  IF (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user) THEN
    RAISE EXCEPTION USING MESSAGE = '{p} refusing to migrate as a superuser';
  END IF;
  IF (SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_catalog.pg_database
      WHERE datname = pg_catalog.current_database()) <> current_user THEN
    RAISE EXCEPTION USING MESSAGE = '{p} ' || current_user || ' does not own database '
      || pg_catalog.current_database();
  END IF;
  IF pg_catalog.to_regclass('public.platform_meta') IS NOT NULL THEN
    EXECUTE '{marker_query}' INTO marker_ok;
    IF NOT marker_ok THEN
      RAISE EXCEPTION USING MESSAGE = '{p} platform_meta environment marker is not ' || {env};
    END IF;
  END IF;
END
$finbot_guard$"""  # noqa: S608 - every interpolated value passed _sql_literal or is a constant
    return [
        f"SET LOCAL lock_timeout = '{MIGRATION_LOCK_TIMEOUT}'",
        f"SELECT pg_catalog.pg_advisory_xact_lock({MIGRATION_LOCK_KEY})",
        guard,
    ]


# ------------------------------------------------------------------------------ downgrades


def _refuse_downgrade(settings: MigrationSettings, what: str) -> None:
    if not settings.downgrade_permitted:
        raise MigrationRefusedError(
            f"{what} refused in {settings.env}: downgrades run only in dev, test or drill with "
            "FINBOT_MIGRATION_ALLOW_DOWNGRADE=true; staging and production use forward fixes (S9)"
        )


def _guard_planned_steps(context: "EnvironmentContext", settings: MigrationSettings) -> None:
    """Check the direction of every planned step before the first one runs."""
    migration_context = context.get_context()
    # Private in Alembic (<2, pinned via uv.lock); tests/unit/test_migration_env.py pins it.
    planned: Callable[..., Iterable[Any]] | None = getattr(
        migration_context, "_migrations_fn", None
    )
    if planned is None:
        raise MigrationRefusedError("cannot inspect planned migration steps; refusing to run")

    def checked(heads: Any, ctx: Any) -> list[Any]:
        steps = list(planned(heads, ctx))
        if any(not getattr(step, "is_upgrade", True) for step in steps):
            _refuse_downgrade(settings, "downgrade")
        return steps

    migration_context._migrations_fn = checked


def _on_version_apply(settings: MigrationSettings) -> Callable[..., None]:
    def callback(*, step: Any, **_: Any) -> None:
        if not step.is_upgrade:
            _refuse_downgrade(settings, "downgrade step")

    return callback


def _refuse_autogenerate(context: "EnvironmentContext") -> None:
    cmd_opts = context.config.cmd_opts
    if cmd_opts is not None and getattr(cmd_opts, "autogenerate", False):
        raise MigrationRefusedError(
            "autogenerate is disabled: write each revision by hand with --rev-id NNNN (docs/10 §3)"
        )


def _refuse_by_cli_command(context: "EnvironmentContext", settings: MigrationSettings) -> None:
    """Refuse ``downgrade`` and ``stamp`` from the ``alembic`` CLI before anything runs."""
    cmd_opts = context.config.cmd_opts
    if cmd_opts is None:
        return
    command = getattr(cmd_opts, "cmd", None)
    name = getattr(command[0], "__name__", "") if command else ""
    if name in {"downgrade", "stamp"}:
        _refuse_downgrade(settings, f"alembic {name}")


# -------------------------------------------------------------------------------- env runner


def _configure_kwargs(settings: MigrationSettings) -> dict[str, Any]:
    return {
        "target_metadata": None,
        "transactional_ddl": True,
        "transaction_per_migration": False,  # one transaction: any failure rolls back everything
        "version_table": "alembic_version",
        "version_table_schema": "public",
        "on_version_apply": _on_version_apply(settings),
    }


def _run_in_transaction(context: "EnvironmentContext", settings: MigrationSettings) -> None:
    _guard_planned_steps(context, settings)
    with context.begin_transaction():
        for statement in guard_sql(settings):
            context.execute(statement)
        context.run_migrations()


def _run_with_connection(
    context: "EnvironmentContext", settings: MigrationSettings, connection: "Connection"
) -> None:
    context.configure(connection=connection, **_configure_kwargs(settings))
    _run_in_transaction(context, settings)


async def _run_online(context: "EnvironmentContext", settings: MigrationSettings) -> None:
    from finbot.infra.db.connection import create_engine, owner_target  # noqa: PLC0415

    engine = create_engine(owner_target(settings))
    try:
        async with engine.connect() as connection:
            await connection.run_sync(
                lambda sync_conn: _run_with_connection(context, settings, sync_conn)
            )
    finally:
        await engine.dispose()


def run_env(context: "EnvironmentContext") -> None:
    """Entry point for ``migrations/env.py``. Fails closed on any policy violation."""
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    _refuse_autogenerate(context)
    config = context.config
    offline = context.is_offline_mode()
    settings = config.attributes.get(SETTINGS_ATTRIBUTE)
    if settings is None:
        settings = load_migration_settings(require_password=not offline)
    _refuse_by_cli_command(context, settings)
    check_revision_policy(ScriptDirectory.from_config(config))

    if offline:
        context.configure(
            dialect_name="postgresql", literal_binds=True, **_configure_kwargs(settings)
        )
        _run_in_transaction(context, settings)
        return
    connection = config.attributes.get(CONNECTION_ATTRIBUTE)
    if connection is not None:
        _run_with_connection(context, settings, connection)
        return
    asyncio.run(_run_online(context, settings))
