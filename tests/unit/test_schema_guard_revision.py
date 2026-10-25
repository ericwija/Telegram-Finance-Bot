"""Static checks of revision 0005 (schema guard, membership epochs) and of G-ENV-7's revision
list, from the offline SQL and the migrations directory (no database)."""

import io
import re
from pathlib import Path

import pytest

from finbot.adapters.cli import migrate
from finbot.infra.db.guards import SUPPORTED_SCHEMA_REVISIONS

VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


@pytest.fixture
def sql_0005(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("FINBOT_ENV", "staging")
    out, err = io.StringIO(), io.StringIO()
    assert migrate.main(["sql", "upgrade", "0004:0005"], out=out, err=err) == 0, err.getvalue()
    return out.getvalue()


def test_the_release_supports_exactly_the_head_revision() -> None:
    revisions = sorted(p.name[:4] for p in VERSIONS.glob("[0-9][0-9][0-9][0-9]_*.py"))
    assert revisions[-1] == "0005"
    assert frozenset({revisions[-1]}) == SUPPORTED_SCHEMA_REVISIONS


def test_0005_grants_no_table_privilege_and_one_narrow_function(sql_0005: str) -> None:
    assert "alembic_version TO finbot_app" not in sql_0005  # AT-RLS-05 stays true
    grants = re.findall(r"GRANT [^;]*;", sql_0005)
    assert grants == ["GRANT EXECUTE ON FUNCTION public.app_schema_revision() TO finbot_app;"]
    for function in ("app_schema_revision", "membership_version_bump"):
        assert f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC;" in sql_0005
    assert "SET search_path = pg_catalog, public" in sql_0005
    assert re.search(
        r"app_schema_revision\(\) RETURNS text\s+LANGUAGE sql STABLE SECURITY DEFINER", sql_0005
    )
    assert "BEFORE UPDATE OF role, status ON public.workspace_memberships" in sql_0005
    for forbidden in ("DROP TABLE", "DELETE FROM", "TRUNCATE", "BYPASSRLS", "ALTER TABLE"):
        assert forbidden not in sql_0005  # additive only
