"""Conversations created before Step 6B, carried across ``0004`` → head (Step 6C, review item 8).

Step 6 (``0411db8``) stored drafts and category prompts **without** ``membership_version`` (and
create drafts without ``account_id``). They are written here in exactly that format, on a
database at ``0004``, as the Step 6 code would have (the proposal shape is copied from
``CaptureState.to_json`` of ``0411db8``). After migrating to head:

* by design, such a conversation is **invalidated** on its next use: a missing version never
  matches the current one, so it is expired (persisted) and refused like any stale button. The
  migration does not backfill a version, which would re-authorize a conversation started
  under a membership nobody can verify any more;
* a confirmed old draft is never replayed with transaction details;
* nothing is posted, and new conversations work normally.

Runs only with FINBOT_TEST_PG_BACKEND.
"""

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

from finbot.domain.finance import DraftExpiredError, draft_ref
from finbot.domain.ledger import TxnType
from tests.migration.test_baseline_migration_db import _query
from tests.migration.test_ledger_migration_db import _engine, _existing_users
from tests.support import pg
from tests.support.finance import NOW, finance, record

pytestmark = pytest.mark.db


def _old_create_proposal(category_id: str) -> str:
    """``CaptureState.to_json()`` of Step 6: no membership_version, no account_id."""
    return json.dumps(
        {
            "purpose": "create",
            "step": "confirm",
            "txn_date": "2026-10-25",
            "txn_type": "expense",
            "amount": 12_000,
            "rounded": False,
            "category_id": category_id,
            "description": "lama",
            "description_done": True,
        }
    )


def test_pre_6b_conversations_are_invalidated_not_revived_after_migration(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    command.upgrade(alembic_config, "0004")
    ani, budi = _existing_users(pg_cluster, test_database)
    expires = (NOW + timedelta(hours=12)).isoformat()

    async def seed_and_confirm() -> Any:  # at 0004: finance defaults, one confirmed draft
        app = _engine(pg_cluster, test_database, pg.APP_ROLE)
        try:
            f = finance(app)
            await f.ledger.balance(budi.actor())
            return await record(f, ani.actor(), TxnType.EXPENSE, "5rb", "Makanan")
        finally:
            await app.dispose()

    confirmed = asyncio.run(seed_and_confirm())
    makanan = _query(
        pg_cluster,
        test_database,
        f"SELECT id FROM categories WHERE workspace_id = '{ani.personal_ws}' AND name = 'Makanan'",  # noqa: S608
    )[0][0]
    # The confirmed draft back in its Step 6 shape; a pending Step 6 draft and prompt.
    _query(
        pg_cluster,
        test_database,
        f"UPDATE drafts SET proposal = proposal - 'membership_version' - 'account_id' "  # noqa: S608
        f"WHERE user_id = '{ani.user_id}'",
    )
    old_draft = _query(
        pg_cluster,
        test_database,
        "INSERT INTO drafts (workspace_id, user_id, purpose, proposal, source, expires_at) "  # noqa: S608
        f"VALUES ('{ani.personal_ws}', '{ani.user_id}', 'create', "
        f"'{_old_create_proposal(str(makanan))}'::jsonb, 'manual', '{expires}') RETURNING id",
    )[0][0]
    old_prompt = _query(
        pg_cluster,
        test_database,
        "INSERT INTO pending_actions (workspace_id, user_id, kind, payload, expires_at) "  # noqa: S608
        f"VALUES ('{budi.personal_ws}', '{budi.user_id}', 'category_create', "
        f"'{{\"kind\": \"expense\"}}'::jsonb, '{expires}') RETURNING id",
    )[0][0]

    command.upgrade(alembic_config, "head")

    async def after() -> None:
        app = _engine(pg_cluster, test_database, pg.APP_ROLE)
        try:
            f = finance(app)
            assert await f.capture.current(ani.actor()) is None  # not offered
            with pytest.raises(DraftExpiredError):
                await f.capture.confirm(ani.actor(), draft_ref(old_draft))
            with pytest.raises(DraftExpiredError):  # no replay with details either
                await f.capture.confirm(ani.actor(), draft_ref(confirmed_draft_id))
            assert await f.capture.current(budi.actor()) is None
            with pytest.raises(DraftExpiredError):
                await f.capture.input_text(budi.actor(), "Kopi")
            fresh = await f.capture.start(ani.actor(), TxnType.EXPENSE, amount_text="7rb")
            fresh = await f.capture.choose_category(ani.actor(), fresh.ref, fresh.categories[0].id)
            fresh = await f.capture.skip_description(ani.actor(), fresh.ref)
            saved = await f.capture.confirm(ani.actor(), fresh.ref)
            assert saved.record.amount == 7_000
            prompt = await f.capture.start_category_create(budi.actor(), TxnType.EXPENSE)
            assert await f.capture.input_text(budi.actor(), "Kopi") is not None
            assert prompt.action.payload["membership_version"] == 1
        finally:
            await app.dispose()

    confirmed_draft_id = _query(
        pg_cluster,
        test_database,
        f"SELECT id FROM drafts WHERE confirmed_txn_id = '{confirmed.record.id}'",  # noqa: S608
    )[0][0]
    asyncio.run(after())
    statuses = dict(
        _query(
            pg_cluster,
            test_database,
            f"SELECT id::text, status FROM drafts WHERE id = '{old_draft}' "  # noqa: S608
            f"UNION ALL SELECT id::text, status FROM pending_actions WHERE id = '{old_prompt}'",
        )
    )
    assert statuses == {str(old_draft): "expired", str(old_prompt): "expired"}
    amounts = sorted(
        r[0] for r in _query(pg_cluster, test_database, "SELECT amount FROM transactions")
    )
    assert amounts == [5_000, 7_000]  # the old 12.000 draft was never posted
    assert (
        _query(pg_cluster, test_database, "SELECT count(*) FROM categories WHERE name = 'Kopi'")[0][
            0
        ]
        == 1
    )
