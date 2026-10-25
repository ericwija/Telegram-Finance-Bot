"""Static checks of revision 0002 (identity and onboarding) through its offline SQL.

Head SQL (all revisions) is checked for psql safety here too."""

import io
import re

import pytest

from finbot.adapters.cli import migrate

FUNCTIONS = ("resolve_principal(bigint)", "redeem_onboarding_invite(bytea, bigint, text)")


def _sql(monkeypatch: pytest.MonkeyPatch, *argv: str, downgrade: bool = False) -> str:
    monkeypatch.setenv("FINBOT_ENV", "test" if downgrade else "staging")
    if downgrade:
        monkeypatch.setenv("FINBOT_MIGRATION_ALLOW_DOWNGRADE", "true")
    out, err = io.StringIO(), io.StringIO()
    assert migrate.main(["sql", *argv], out=out, err=err) == migrate.EXIT_OK, err.getvalue()
    return out.getvalue()


@pytest.fixture
def head_sql(monkeypatch: pytest.MonkeyPatch) -> str:
    return _sql(monkeypatch, "upgrade", "head")


@pytest.fixture
def identity_sql(monkeypatch: pytest.MonkeyPatch) -> str:
    sql = _sql(monkeypatch, "upgrade", "0002")
    return sql[sql.index("CREATE TABLE public.telegram_identities") :]


def test_head_sql_is_psql_safe(head_sql: str) -> None:
    assert "%" not in head_sql
    assert not re.search(r"(?<!:):[a-z_]+\b", head_sql)


def test_identity_tables_are_closed_to_the_app(identity_sql: str) -> None:
    for table in ("telegram_identities", "onboarding_invites"):
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in identity_sql
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY" in identity_sql
        assert (
            f"CREATE POLICY no_direct_access ON public.{table} USING (false) WITH CHECK (false)"
            in identity_sql
        )
        assert not re.search(rf"GRANT [^;]* ON public\.{table}\b", identity_sql), table
    grants = [line for line in identity_sql.splitlines() if line.startswith("GRANT")]
    assert grants == [f"GRANT EXECUTE ON FUNCTION public.{f} TO finbot_app;" for f in FUNCTIONS]


def test_definer_functions_pin_search_path_and_revoke_public(identity_sql: str) -> None:
    for function in FUNCTIONS:
        name = function.split("(")[0]
        block = identity_sql[identity_sql.index(f"CREATE FUNCTION public.{name}(") :]
        header = block[: block.index("$$")]
        assert "SECURITY DEFINER" in header
        assert "SET search_path = pg_catalog, public" in header
        assert f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC" in identity_sql


def test_redeem_takes_no_workspace_role_or_user_argument(identity_sql: str) -> None:
    signature = re.search(
        r"CREATE FUNCTION public\.redeem_onboarding_invite\(([^)]*)\)", identity_sql
    )
    assert signature
    params = [p.split()[0] for p in signature.group(1).split(",")]
    assert params == ["token_hash", "tg_user_id", "display_name"]
    body = identity_sql[signature.end() : identity_sql.index("$$;", signature.end())]
    # Only a personal workspace, only an owner membership for the user it just created.
    assert "'business'" not in body
    assert re.findall(r"'(owner|admin|editor|viewer)'", body) == ["owner"]
    assert "pg_advisory_xact_lock" in body
    assert "FOR UPDATE" in body


def test_downgrade_removes_the_identity_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    sql = _sql(monkeypatch, "downgrade", "0002:0001", downgrade=True)
    for function in FUNCTIONS:
        assert f"DROP FUNCTION public.{function};" in sql
    for table in ("onboarding_invites", "telegram_identities"):
        assert f"DROP TABLE public.{table};" in sql
