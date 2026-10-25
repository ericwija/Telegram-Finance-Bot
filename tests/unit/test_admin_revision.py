"""Static checks of revision 0003 (platform-admin bootstrap, invite management), offline SQL."""

import io
import re

import pytest

from finbot.adapters.cli import migrate

INVITE_FUNCTIONS = (
    "create_onboarding_invite(bytea, text, text)",
    "revoke_onboarding_invite(uuid, text)",
    "list_onboarding_invites(boolean)",
)
BOOTSTRAP = "bootstrap_platform_admin(bigint, text)"


@pytest.fixture
def sql_0003(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("FINBOT_ENV", "staging")
    out, err = io.StringIO(), io.StringIO()
    # Pinned to 0003: later revisions add their own grants (0004: ledger tables).
    assert migrate.main(["sql", "upgrade", "0003"], out=out, err=err) == 0, err.getvalue()
    sql = out.getvalue()
    return sql[sql.index(f"CREATE FUNCTION public.{BOOTSTRAP.split('(', maxsplit=1)[0]}(") :]


def _header(sql: str, name: str) -> str:
    block = sql[sql.index(f"CREATE FUNCTION public.{name}(") :]
    return block[: block.index("$$")]


def test_bootstrap_is_owner_only(sql_0003: str) -> None:
    header = _header(sql_0003, "bootstrap_platform_admin")
    assert "SECURITY DEFINER" not in header  # runs with the caller's rights
    assert "SET search_path = pg_catalog, public" in header
    assert f"REVOKE ALL ON FUNCTION public.{BOOTSTRAP} FROM PUBLIC;" in sql_0003
    assert not re.search(rf"GRANT [^;]*{re.escape(BOOTSTRAP)}", sql_0003)
    body = sql_0003[sql_0003.index("CREATE FUNCTION public.bootstrap_platform_admin(") :]
    body = body[: body.index("$$;")]
    assert "session_user <> 'finbot_owner'" in body
    # The permanent marker is claimed before anything else is written.
    assert body.index("'platform_admin_bootstrapped'") < body.index("INSERT INTO public.users")


def test_invite_functions_are_definer_app_only_and_check_the_admin_first(sql_0003: str) -> None:
    grants = [line for line in sql_0003.splitlines() if line.startswith("GRANT")]
    assert grants == [
        f"GRANT EXECUTE ON FUNCTION public.{f} TO finbot_app;" for f in INVITE_FUNCTIONS
    ]
    for function in INVITE_FUNCTIONS:
        name = function.split("(")[0]
        header = _header(sql_0003, name)
        assert "SECURITY DEFINER" in header
        assert "SET search_path = pg_catalog, public" in header
        assert f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC;" in sql_0003
        body = sql_0003[sql_0003.index(f"CREATE FUNCTION public.{name}(") :]
        body = body[body.index("BEGIN") :]
        assert (
            body.removeprefix("BEGIN")
            .lstrip()
            .startswith("IF NOT EXISTS (SELECT 1 FROM public.users u")
        )


def test_invites_expire_after_seven_days_and_listings_never_return_the_hash(sql_0003: str) -> None:
    create = sql_0003[sql_0003.index("CREATE FUNCTION public.create_onboarding_invite(") :]
    assert "pg_catalog.now() + interval '7 days'" in create[: create.index("$$;")]
    listing = sql_0003[sql_0003.index("CREATE FUNCTION public.list_onboarding_invites(") :]
    assert "token_hash" not in listing[: listing.index("$$;")]


def test_downgrade_drops_all_four_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINBOT_ENV", "test")
    monkeypatch.setenv("FINBOT_MIGRATION_ALLOW_DOWNGRADE", "true")
    out, err = io.StringIO(), io.StringIO()
    assert migrate.main(["sql", "downgrade", "0003:0002"], out=out, err=err) == 0, err.getvalue()
    for function in (*INVITE_FUNCTIONS, BOOTSTRAP):
        assert f"DROP FUNCTION public.{function};" in out.getvalue()
