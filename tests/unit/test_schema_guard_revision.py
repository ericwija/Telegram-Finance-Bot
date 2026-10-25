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
    assert revisions[-1] == "0006"
    assert frozenset({revisions[-1]}) == SUPPORTED_SCHEMA_REVISIONS


@pytest.fixture
def sql_0006(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("FINBOT_ENV", "staging")
    out, err = io.StringIO(), io.StringIO()
    assert migrate.main(["sql", "upgrade", "0005:0006"], out=out, err=err) == 0, err.getvalue()
    return out.getvalue()


def test_0006_quota_matches_the_domain_and_is_additive(sql_0006: str) -> None:
    from finbot.domain.finance import (  # noqa: PLC0415
        CATEGORY_MAX_PAGES,
        CATEGORY_PAGE_SIZE,
        CATEGORY_QUOTA_PER_KIND,
    )

    assert f">= {CATEGORY_QUOTA_PER_KIND} THEN" in sql_0006
    assert CATEGORY_MAX_PAGES * CATEGORY_PAGE_SIZE == 2 * CATEGORY_QUOTA_PER_KIND
    assert "SECURITY DEFINER SET search_path = pg_catalog, public" in sql_0006
    assert "REVOKE ALL ON FUNCTION public.ledger_category_quota() FROM PUBLIC;" in sql_0006
    assert "GRANT" not in sql_0006
    assert "BEFORE INSERT ON public.categories" in sql_0006
    body = sql_0006.split("BEGIN;", 1)[-1]
    assert "%" not in body
    for forbidden in ("DROP TABLE", "DELETE FROM", "ALTER TABLE", "UPDATE public.categories"):
        assert forbidden not in sql_0006


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
