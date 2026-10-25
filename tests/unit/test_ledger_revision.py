"""Static checks of revision 0004 (ledger core), from the offline SQL: RLS, grants, triggers."""

import io
import re

import pytest

from finbot.adapters.cli import migrate

LEDGER_TABLES = {
    "accounts",
    "categories",
    "transactions",
    "transaction_entries",
    "transaction_revisions",
    "drafts",
    "pending_actions",
}
APPEND_ONLY = ("transaction_entries", "transaction_revisions")


@pytest.fixture
def sql_0004(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("FINBOT_ENV", "staging")
    out, err = io.StringIO(), io.StringIO()
    assert migrate.main(["sql", "upgrade", "0003:0004"], out=out, err=err) == 0, err.getvalue()
    return out.getvalue()


def test_every_new_table_gets_rls_enable_force_and_a_policy(sql_0004: str) -> None:
    created = set(re.findall(r"CREATE TABLE public\.(\w+)", sql_0004))
    assert created == LEDGER_TABLES
    for table in created:
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;" in sql_0004
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY;" in sql_0004
        assert f"CREATE POLICY tenant_isolation ON public.{table}" in sql_0004


def test_entries_and_revisions_are_insert_and_select_only(sql_0004: str) -> None:
    for table in APPEND_ONLY:
        grants = re.findall(rf"GRANT ([^;]*) ON public\.{table} TO finbot_app;", sql_0004)
        assert grants == ["SELECT, INSERT"], table
    assert not re.search(r"GRANT [^;]*DELETE", sql_0004)
    assert not re.search(r"GRANT [^;]*TRUNCATE", sql_0004)
    assert "BYPASSRLS" not in sql_0004


def test_type_and_identity_columns_are_never_updatable(sql_0004: str) -> None:
    update = re.search(r"GRANT UPDATE \(([^)]*)\) ON public\.transactions", sql_0004)
    assert update is not None
    columns = {c.strip() for c in update[1].split(",")}
    assert not columns & {"id", "workspace_id", "type", "created_by", "created_at", "currency"}


def test_definer_functions_pin_search_path_and_drop_public_execute(sql_0004: str) -> None:
    functions = re.findall(r"CREATE FUNCTION public\.(\w+)\(\)", sql_0004)
    assert functions
    for name in functions:
        block = sql_0004[sql_0004.index(f"CREATE FUNCTION public.{name}()") :]
        header = block[: block.index("$$")]
        assert "SET search_path = pg_catalog, public" in header, name
        assert f"REVOKE ALL ON FUNCTION public.{name}() FROM PUBLIC;" in sql_0004
        assert not re.search(rf"GRANT [^;]*{name}", sql_0004)


def test_balanced_entries_trigger_is_deferred(sql_0004: str) -> None:
    assert sql_0004.count("CREATE CONSTRAINT TRIGGER trg_entries_balanced") == 2
    assert "DEFERRABLE INITIALLY DEFERRED" in sql_0004


def test_policies_require_membership_and_per_user_drafts(sql_0004: str) -> None:
    for table in LEDGER_TABLES:
        policy = sql_0004[sql_0004.index(f"CREATE POLICY tenant_isolation ON public.{table}") :]
        policy = policy[: policy.index(";")]
        assert "app_workspace_id()" in policy
        assert "app_is_active_member(workspace_id)" in policy
        if table in ("drafts", "pending_actions"):
            assert "user_id = (SELECT public.app_user_id())" in policy


def test_the_guard_runs_first_and_the_sql_is_psql_safe(sql_0004: str) -> None:
    guard = sql_0004.index("DO $finbot_guard$")
    assert guard < sql_0004.index("CREATE TABLE public.accounts")
    body = sql_0004[guard:]
    assert "%" not in body
    assert not re.search(r"(?<![:\w]):[a-z_]+\b", body.replace("::", ""))
