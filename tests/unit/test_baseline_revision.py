"""Static checks of the Phase 1 baseline revision through its offline SQL (no database).

The rendered SQL is what a reviewer (or the approved offline fallback, docs/10 §3) would see, so
these checks run on it: the guard comes first, the marker matches FINBOT_ENV, every table gets
RLS ENABLE + FORCE and a policy in the same revision (D5), the SQL is psql-safe, and the
downgrade drops everything the upgrade creates.
"""

import io
import re

import pytest

from finbot.adapters.cli import migrate

TABLE_RE = re.compile(r"CREATE TABLE public\.(\w+)")
FUNCTION_RE = re.compile(r"CREATE FUNCTION public\.(\w+)\(")
NON_RLS_TABLES = {"alembic_version", "platform_meta"}


def _sql(monkeypatch: pytest.MonkeyPatch, env: str, *argv: str, downgrade: bool = False) -> str:
    monkeypatch.setenv("FINBOT_ENV", env)
    if downgrade:
        monkeypatch.setenv("FINBOT_MIGRATION_ALLOW_DOWNGRADE", "true")
    out, err = io.StringIO(), io.StringIO()
    code = migrate.main(["sql", *argv], out=out, err=err)
    assert code == migrate.EXIT_OK, err.getvalue()
    return out.getvalue()


@pytest.fixture
def upgrade_sql(monkeypatch: pytest.MonkeyPatch) -> str:
    return _sql(monkeypatch, "staging", "upgrade", "head")


def test_guard_runs_before_any_schema_change(upgrade_sql: str) -> None:
    guard = upgrade_sql.index("DO $finbot_guard$")
    first_change = min(
        upgrade_sql.index(marker)
        for marker in ("CREATE TABLE", "REVOKE", "GRANT", "CREATE EXTENSION", "ALTER DEFAULT")
    )
    assert guard < first_change
    assert upgrade_sql.count("$finbot_guard$") == 2  # one guard block, opened and closed


def test_environment_marker_matches_the_target_environment(
    upgrade_sql: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "VALUES ('environment', 'staging')" in upgrade_sql
    assert "VALUES ('environment', 'drill')" in _sql(monkeypatch, "drill", "upgrade", "head")


def test_sql_is_psql_safe(upgrade_sql: str) -> None:
    assert "%" not in upgrade_sql  # Alembic would double it offline
    assert not re.search(r"(?<!:):[a-z_]+\b", upgrade_sql)  # no unbound :name parameters


def test_every_table_gets_rls_enable_force_and_a_policy(upgrade_sql: str) -> None:
    tables = set(TABLE_RE.findall(upgrade_sql)) - NON_RLS_TABLES
    assert tables == {"users", "workspaces", "workspace_memberships", "audit_events"}
    for table in tables:
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in upgrade_sql
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY" in upgrade_sql
        assert re.search(rf"CREATE POLICY \w+ ON public\.{table}\b", upgrade_sql), table


def test_definer_functions_pin_search_path_and_drop_public_execute(upgrade_sql: str) -> None:
    for name in FUNCTION_RE.findall(upgrade_sql):
        block = upgrade_sql[upgrade_sql.index(f"CREATE FUNCTION public.{name}(") :]
        header = block[: block.index(" AS")]
        if "SECURITY DEFINER" in header:
            assert "SET search_path = pg_catalog, public" in header, name
        assert f"REVOKE ALL ON FUNCTION public.{name}(" in upgrade_sql, name


def test_app_never_gets_update_or_delete_on_audit_events(upgrade_sql: str) -> None:
    grants = [line for line in upgrade_sql.splitlines() if line.startswith("GRANT")]
    audit = [g for g in grants if "public.audit_events" in g]
    assert audit == ["GRANT SELECT, INSERT ON public.audit_events TO finbot_app;"]
    assert not [g for g in grants if "DELETE" in g or "TRUNCATE" in g]


def test_downgrade_drops_everything_the_upgrade_creates(
    upgrade_sql: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    downgrade_sql = _sql(monkeypatch, "test", "downgrade", "0001:base", downgrade=True)
    for table in set(TABLE_RE.findall(upgrade_sql)) - {"alembic_version"}:
        assert f"DROP TABLE public.{table};" in downgrade_sql, table
    for name in FUNCTION_RE.findall(upgrade_sql):
        assert f"DROP FUNCTION public.{name}(" in downgrade_sql, name
    assert "DROP EXTENSION citext;" in downgrade_sql
    assert "TO PUBLIC" in downgrade_sql  # database and schema defaults restored
