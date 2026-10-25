"""Ledger core: accounts, categories, transactions + entries, revisions, drafts, pending actions.

Revision ID: 0004
Revises: 0003

Specification: docs/05 §5 and the P3 row of §11 (D1 header + signed entries), docs/09 §4.2 and
§6, docs/08 R-8..R-10 and R-13; docs/10 §12 lists where this revision interprets them.

Additive only, safe on a database with existing users and workspaces: new tables, one new
nullable column (``workspace_memberships.default_account_id``) and grants. No existing row is
rewritten. Finance defaults ("Kas" and the default categories) are seeded by the application,
idempotently, the first time a workspace uses a finance feature, so workspaces created before
this revision need no data migration.

Rules for this file (docs/10 §3): one SQL statement per ``op.execute``; no ``%`` and no
``:name`` in the SQL; every table gets RLS ENABLE + FORCE and its policies here (D5); the
downgrade removes everything the upgrade created (dev/test/drill only, S9).
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_WS = "(SELECT public.app_workspace_id())"
_USER = "(SELECT public.app_user_id())"


def _member(column: str) -> str:
    return f"(SELECT public.app_is_active_member({column}))"


_MAX = "1000000000000"  # G-6 upper bound for amounts (10^12 rupiah)

# ------------------------------------------------------------------------------- tables

_TABLES = (
    f"""CREATE TABLE public.accounts (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id     uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
  name             text NOT NULL CHECK (pg_catalog.char_length(name) BETWEEN 1 AND 60),
  type             text NOT NULL CHECK (type IN ('cash', 'bank', 'ewallet')),
  opening_balance  bigint NOT NULL DEFAULT 0 CHECK (opening_balance BETWEEN -{_MAX} AND {_MAX}),
  opening_date     date NOT NULL,
  aliases          text[] NOT NULL DEFAULT '{{}}',
  archived_at      timestamptz,
  created_by       uuid NOT NULL REFERENCES public.users (id),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  version          int NOT NULL DEFAULT 1,
  CONSTRAINT accounts_workspace_id_id_key UNIQUE (workspace_id, id)
)""",
    "CREATE UNIQUE INDEX uq_account_name_active ON public.accounts "
    "(workspace_id, pg_catalog.lower(name)) WHERE archived_at IS NULL",
    """CREATE TABLE public.categories (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id  uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
  kind          text NOT NULL CHECK (kind IN ('income', 'expense')),
  name          text NOT NULL CHECK (pg_catalog.char_length(name) BETWEEN 1 AND 40
                                     AND name = pg_catalog.btrim(name)),
  emoji         text CHECK (pg_catalog.char_length(emoji) <= 8),
  is_default    boolean NOT NULL DEFAULT false,
  archived_at   timestamptz,
  created_by    uuid REFERENCES public.users (id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  version       int NOT NULL DEFAULT 1,
  CONSTRAINT categories_workspace_id_id_key UNIQUE (workspace_id, id),
  CONSTRAINT categories_workspace_id_id_kind_key UNIQUE (workspace_id, id, kind),
  CONSTRAINT categories_default_not_archived CHECK (NOT (is_default AND archived_at IS NOT NULL))
)""",
    "CREATE UNIQUE INDEX uq_category_name ON public.categories "
    "(workspace_id, kind, pg_catalog.lower(name)) WHERE archived_at IS NULL",
    "CREATE UNIQUE INDEX uq_category_default ON public.categories (workspace_id, kind) "
    "WHERE is_default",
    f"""CREATE TABLE public.transactions (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id    uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
  type            text NOT NULL CHECK (type IN ('income', 'expense', 'transfer')),
  amount          bigint NOT NULL CHECK (amount > 0 AND amount <= {_MAX}),
  currency        text NOT NULL DEFAULT 'IDR' CHECK (currency = 'IDR'),
  txn_date        date NOT NULL CHECK (txn_date >= DATE '2000-01-01'),
  category_id     uuid,
  description     text CHECK (pg_catalog.char_length(description) BETWEEN 1 AND 500),
  source          text NOT NULL
                  CHECK (source IN ('text', 'ai_text', 'receipt', 'manual', 'import')),
  status          text NOT NULL DEFAULT 'posted' CHECK (status IN ('posted', 'voided')),
  entry_revision  int NOT NULL DEFAULT 1 CHECK (entry_revision >= 1),
  version         int NOT NULL DEFAULT 1 CHECK (version >= 1),
  created_by      uuid NOT NULL REFERENCES public.users (id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_by      uuid REFERENCES public.users (id),
  updated_at      timestamptz,
  voided_at       timestamptz,
  voided_by       uuid REFERENCES public.users (id),
  void_reason     text CHECK (pg_catalog.char_length(void_reason) BETWEEN 1 AND 200),
  CONSTRAINT transactions_workspace_id_id_key UNIQUE (workspace_id, id),
  CONSTRAINT transactions_workspace_id_id_type_key UNIQUE (workspace_id, id, type),
  CONSTRAINT transactions_category_fkey FOREIGN KEY (workspace_id, category_id, type)
    REFERENCES public.categories (workspace_id, id, kind),
  CONSTRAINT transactions_category_iff_not_transfer
    CHECK ((type = 'transfer') = (category_id IS NULL)),
  CONSTRAINT transactions_void_consistent CHECK ((status = 'voided') = (voided_at IS NOT NULL)),
  CONSTRAINT transactions_voided_by_consistent CHECK ((voided_at IS NULL) = (voided_by IS NULL))
)""",
    "CREATE INDEX ix_txn_ws_date ON public.transactions (workspace_id, txn_date) "
    "WHERE status = 'posted'",
    "CREATE INDEX ix_txn_ws_category ON public.transactions (workspace_id, category_id, txn_date) "
    "WHERE status = 'posted'",
    "CREATE INDEX ix_txn_ws_history ON public.transactions "
    "(workspace_id, status, txn_date DESC, id DESC)",
    f"""CREATE TABLE public.transaction_entries (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id      uuid NOT NULL,
  transaction_id    uuid NOT NULL,
  transaction_type  text NOT NULL,
  revision          int NOT NULL CHECK (revision >= 1),
  leg               text NOT NULL CHECK (leg IN ('single', 'source', 'destination')),
  account_id        uuid NOT NULL,
  amount            bigint NOT NULL CHECK (amount <> 0 AND amount BETWEEN -{_MAX} AND {_MAX}),
  created_by        uuid NOT NULL REFERENCES public.users (id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT transaction_entries_transaction_fkey
    FOREIGN KEY (workspace_id, transaction_id, transaction_type)
    REFERENCES public.transactions (workspace_id, id, type) ON DELETE CASCADE,
  CONSTRAINT transaction_entries_account_fkey FOREIGN KEY (workspace_id, account_id)
    REFERENCES public.accounts (workspace_id, id),
  CONSTRAINT transaction_entries_sign_matches_leg CHECK (
       (transaction_type = 'income'   AND leg = 'single'      AND amount > 0)
    OR (transaction_type = 'expense'  AND leg = 'single'      AND amount < 0)
    OR (transaction_type = 'transfer' AND leg = 'source'      AND amount < 0)
    OR (transaction_type = 'transfer' AND leg = 'destination' AND amount > 0)),
  CONSTRAINT transaction_entries_one_leg_per_revision UNIQUE (transaction_id, revision, leg),
  CONSTRAINT transaction_entries_distinct_accounts UNIQUE (transaction_id, revision, account_id)
)""",
    "CREATE INDEX ix_entries_ws_account ON public.transaction_entries (workspace_id, account_id) "
    "INCLUDE (amount, transaction_id, revision)",
    "CREATE INDEX ix_entries_ws_txn ON public.transaction_entries "
    "(workspace_id, transaction_id, revision)",
    """CREATE TABLE public.transaction_revisions (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id    uuid NOT NULL,
  transaction_id  uuid NOT NULL,
  version         int NOT NULL CHECK (version >= 1),
  change_kind     text NOT NULL CHECK (change_kind IN ('create', 'update', 'void', 'restore')),
  snapshot        jsonb NOT NULL,
  changed_fields  text[] NOT NULL DEFAULT '{}',
  changed_by      uuid NOT NULL REFERENCES public.users (id),
  changed_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT transaction_revisions_version_key UNIQUE (transaction_id, version),
  CONSTRAINT transaction_revisions_transaction_fkey FOREIGN KEY (workspace_id, transaction_id)
    REFERENCES public.transactions (workspace_id, id) ON DELETE CASCADE
)""",
    "CREATE INDEX ix_revisions_ws_txn ON public.transaction_revisions "
    "(workspace_id, transaction_id, version)",
    """CREATE TABLE public.drafts (
  id                     uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id           uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
  user_id                uuid NOT NULL REFERENCES public.users (id),
  purpose                text NOT NULL DEFAULT 'create' CHECK (purpose IN ('create', 'edit')),
  target_transaction_id  uuid,
  status                 text NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'confirmed', 'cancelled', 'expired')),
  proposal               jsonb NOT NULL,
  source                 text NOT NULL CHECK (source IN ('text', 'ai_text', 'receipt', 'manual')),
  telegram_chat_id       bigint,
  telegram_message_id    bigint,
  confirmed_txn_id       uuid,
  expires_at             timestamptz NOT NULL,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT drafts_workspace_id_id_key UNIQUE (workspace_id, id),
  CONSTRAINT drafts_confirmed_txn_fkey FOREIGN KEY (workspace_id, confirmed_txn_id)
    REFERENCES public.transactions (workspace_id, id),
  CONSTRAINT drafts_target_txn_fkey FOREIGN KEY (workspace_id, target_transaction_id)
    REFERENCES public.transactions (workspace_id, id),
  CONSTRAINT drafts_confirmed_consistent
    CHECK ((status = 'confirmed') = (confirmed_txn_id IS NOT NULL)),
  CONSTRAINT drafts_target_iff_edit CHECK ((purpose = 'edit') = (target_transaction_id IS NOT NULL))
)""",
    "CREATE INDEX ix_drafts_pending ON public.drafts (expires_at) WHERE status = 'pending'",
    # One conversation at a time per user and workspace: starting a new one cancels the old.
    "CREATE UNIQUE INDEX uq_drafts_one_pending ON public.drafts (workspace_id, user_id) "
    "WHERE status = 'pending'",
    """CREATE TABLE public.pending_actions (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id  uuid REFERENCES public.workspaces (id) ON DELETE CASCADE,
  user_id       uuid NOT NULL REFERENCES public.users (id),
  kind          text NOT NULL CHECK (kind IN ('category_create', 'category_rename')),
  payload       jsonb NOT NULL DEFAULT '{}',
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'completed', 'cancelled', 'expired')),
  expires_at    timestamptz NOT NULL,
  completed_at  timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pending_actions_completed_consistent
    CHECK ((status = 'completed') = (completed_at IS NOT NULL))
)""",
    "CREATE UNIQUE INDEX uq_pending_actions_one_pending ON public.pending_actions "
    "(workspace_id, user_id) WHERE status = 'pending'",
    # FR-ACC-05: each member's default account in the workspace (docs/05 §4, P3 row of §11).
    "ALTER TABLE public.workspace_memberships ADD COLUMN default_account_id uuid",
    "ALTER TABLE public.workspace_memberships ADD CONSTRAINT "
    "workspace_memberships_default_account_fkey FOREIGN KEY (workspace_id, default_account_id) "
    "REFERENCES public.accounts (workspace_id, id)",
    # The effective entry set of each transaction; security_invoker so the caller's RLS applies.
    """CREATE VIEW public.current_entries WITH (security_invoker = true) AS
SELECT e.id, e.workspace_id, e.transaction_id, e.transaction_type, e.revision, e.leg,
       e.account_id, e.amount, e.created_by, e.created_at,
       t.txn_date, t.status, t.category_id, t.currency
FROM public.transaction_entries e
JOIN public.transactions t
  ON t.workspace_id = e.workspace_id AND t.id = e.transaction_id
 AND e.revision = t.entry_revision""",
)

# ------------------------------------------------------------------- trigger functions

_FUNCTIONS = (
    # INV-2..INV-4 at COMMIT (docs/05 §5.2). Definer: it must see every entry of the
    # transaction whatever the caller's RLS context is; it reads by transaction ID only.
    """CREATE FUNCTION public.ledger_entries_balanced() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS
$$
DECLARE
  v_txn   uuid;
  v_type  text;
  v_amt   bigint;
  v_rev   int;
  n_single int; n_source int; n_dest int;
  s_single bigint; s_source bigint; s_dest bigint;
BEGIN
  IF TG_TABLE_NAME = 'transaction_entries' THEN
    v_txn := NEW.transaction_id;
  ELSE
    v_txn := NEW.id;
  END IF;
  SELECT t.type, t.amount, t.entry_revision INTO v_type, v_amt, v_rev
  FROM public.transactions t WHERE t.id = v_txn;
  IF NOT FOUND THEN
    RETURN NULL;  -- removed in the same transaction (workspace purge cascade)
  END IF;
  IF EXISTS (SELECT 1 FROM public.transaction_entries e
             WHERE e.transaction_id = v_txn AND e.revision > v_rev) THEN
    RAISE EXCEPTION USING ERRCODE = 'check_violation',
      MESSAGE = 'transaction ' || v_txn::text || ' has entries beyond its entry_revision';
  END IF;
  SELECT count(*) FILTER (WHERE e.leg = 'single'),
         count(*) FILTER (WHERE e.leg = 'source'),
         count(*) FILTER (WHERE e.leg = 'destination'),
         coalesce(sum(e.amount) FILTER (WHERE e.leg = 'single'), 0),
         coalesce(sum(e.amount) FILTER (WHERE e.leg = 'source'), 0),
         coalesce(sum(e.amount) FILTER (WHERE e.leg = 'destination'), 0)
    INTO n_single, n_source, n_dest, s_single, s_source, s_dest
  FROM public.transaction_entries e
  WHERE e.transaction_id = v_txn AND e.revision = v_rev;
  IF (v_type = 'income' AND NOT (n_single = 1 AND n_source = 0 AND n_dest = 0
                                 AND s_single = v_amt))
     OR (v_type = 'expense' AND NOT (n_single = 1 AND n_source = 0 AND n_dest = 0
                                     AND s_single = -v_amt))
     OR (v_type = 'transfer' AND NOT (n_single = 0 AND n_source = 1 AND n_dest = 1
                                      AND s_dest = v_amt AND s_source + s_dest = 0)) THEN
    RAISE EXCEPTION USING ERRCODE = 'check_violation',
      MESSAGE = 'unbalanced entries for transaction ' || v_txn::text
        || ' (type ' || v_type || ', revision ' || v_rev::text || ')';
  END IF;
  RETURN NULL;
END
$$""",
    "REVOKE ALL ON FUNCTION public.ledger_entries_balanced() FROM PUBLIC",
    # INV-6 and append-only revisions (S6: session_user, see 0001).
    """CREATE FUNCTION public.ledger_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$$
BEGIN
  IF session_user <> 'finbot_owner' THEN
    RAISE EXCEPTION USING ERRCODE = 'insufficient_privilege',
      MESSAGE = TG_TABLE_NAME || ' is append-only: ' || TG_OP || ' refused for ' || session_user;
  END IF;
  IF TG_LEVEL = 'STATEMENT' THEN
    RETURN NULL;
  ELSIF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END
$$""",
    "REVOKE ALL ON FUNCTION public.ledger_append_only() FROM PUBLIC",
    # D21(a): the type is part of the sign rules and FKs; a type change is void + replace.
    """CREATE FUNCTION public.ledger_txn_type_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$$
BEGIN
  IF NEW.type IS DISTINCT FROM OLD.type THEN
    RAISE EXCEPTION USING ERRCODE = 'check_violation',
      MESSAGE = 'transactions.type is immutable (void and replace instead)';
  END IF;
  RETURN NEW;
END
$$""",
    "REVOKE ALL ON FUNCTION public.ledger_txn_type_immutable() FROM PUBLIC",
    # R-10: new movements never use an archived account. Definer: reads one account by key.
    """CREATE FUNCTION public.ledger_entry_active_account() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS
$$
BEGIN
  IF EXISTS (SELECT 1 FROM public.accounts a
             WHERE a.workspace_id = NEW.workspace_id AND a.id = NEW.account_id
               AND a.archived_at IS NOT NULL) THEN
    RAISE EXCEPTION USING ERRCODE = 'check_violation',
      MESSAGE = 'account ' || NEW.account_id::text || ' is archived';
  END IF;
  RETURN NEW;
END
$$""",
    "REVOKE ALL ON FUNCTION public.ledger_entry_active_account() FROM PUBLIC",
    # R-10 / FR-CAT-04: archived categories stay on old transactions but are never newly set.
    """CREATE FUNCTION public.ledger_txn_active_category() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS
$$
BEGIN
  IF NEW.category_id IS NOT NULL
     AND (TG_OP = 'INSERT' OR NEW.category_id IS DISTINCT FROM OLD.category_id)
     AND EXISTS (SELECT 1 FROM public.categories c
                 WHERE c.workspace_id = NEW.workspace_id AND c.id = NEW.category_id
                   AND c.archived_at IS NOT NULL) THEN
    RAISE EXCEPTION USING ERRCODE = 'check_violation',
      MESSAGE = 'category ' || NEW.category_id::text || ' is archived';
  END IF;
  RETURN NEW;
END
$$""",
    "REVOKE ALL ON FUNCTION public.ledger_txn_active_category() FROM PUBLIC",
)

_TRIGGERS = (
    "CREATE CONSTRAINT TRIGGER trg_entries_balanced AFTER INSERT ON public.transaction_entries "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.ledger_entries_balanced()",
    "CREATE CONSTRAINT TRIGGER trg_entries_balanced "
    "AFTER INSERT OR UPDATE OF amount, entry_revision, type ON public.transactions "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.ledger_entries_balanced()",
    "CREATE TRIGGER trg_entries_immutable BEFORE UPDATE OR DELETE ON public.transaction_entries "
    "FOR EACH ROW EXECUTE FUNCTION public.ledger_append_only()",
    "CREATE TRIGGER trg_entries_no_truncate BEFORE TRUNCATE ON public.transaction_entries "
    "FOR EACH STATEMENT EXECUTE FUNCTION public.ledger_append_only()",
    "CREATE TRIGGER trg_revisions_immutable BEFORE UPDATE OR DELETE "
    "ON public.transaction_revisions FOR EACH ROW EXECUTE FUNCTION public.ledger_append_only()",
    "CREATE TRIGGER trg_revisions_no_truncate BEFORE TRUNCATE ON public.transaction_revisions "
    "FOR EACH STATEMENT EXECUTE FUNCTION public.ledger_append_only()",
    "CREATE TRIGGER trg_txn_type_immutable BEFORE UPDATE OF type ON public.transactions "
    "FOR EACH ROW EXECUTE FUNCTION public.ledger_txn_type_immutable()",
    "CREATE TRIGGER trg_entries_active_account BEFORE INSERT ON public.transaction_entries "
    "FOR EACH ROW EXECUTE FUNCTION public.ledger_entry_active_account()",
    "CREATE TRIGGER trg_txn_active_category BEFORE INSERT OR UPDATE OF category_id "
    "ON public.transactions FOR EACH ROW EXECUTE FUNCTION public.ledger_txn_active_category()",
)

# ---------------------------------------------------------------------------------- RLS

TENANT_TABLES = (
    "accounts",
    "categories",
    "transactions",
    "transaction_entries",
    "transaction_revisions",
)
PER_USER_TABLES = ("drafts", "pending_actions")

_TENANT = f"workspace_id = {_WS} AND {_member('workspace_id')}"
_PER_USER = f"{_TENANT} AND user_id = {_USER}"

_POLICIES = (
    *(
        f"CREATE POLICY tenant_isolation ON public.{table} USING ({_TENANT}) WITH CHECK ({_TENANT})"
        for table in TENANT_TABLES
    ),
    # R-9: a draft or pending action is visible only to its own user, in its own workspace.
    *(
        f"CREATE POLICY tenant_isolation ON public.{table} "
        f"USING ({_PER_USER}) WITH CHECK ({_PER_USER})"
        for table in PER_USER_TABLES
    ),
)

# ------------------------------------------------------------------------- app grants (S7)

_APP_GRANTS = (
    "GRANT SELECT, INSERT ON public.accounts TO finbot_app",
    "GRANT UPDATE (name, opening_balance, opening_date, aliases, archived_at, updated_at, "
    "version) ON public.accounts TO finbot_app",
    "GRANT SELECT, INSERT ON public.categories TO finbot_app",
    "GRANT UPDATE (name, emoji, archived_at, updated_at, version) ON public.categories "
    "TO finbot_app",
    "GRANT SELECT, INSERT ON public.transactions TO finbot_app",
    "GRANT UPDATE (amount, txn_date, category_id, description, status, entry_revision, version, "
    "updated_by, updated_at, voided_at, voided_by, void_reason) ON public.transactions "
    "TO finbot_app",
    # INV-6: entries and revisions are INSERT + SELECT only (docs/05 §8).
    "GRANT SELECT, INSERT ON public.transaction_entries TO finbot_app",
    "GRANT SELECT, INSERT ON public.transaction_revisions TO finbot_app",
    "GRANT SELECT, INSERT ON public.drafts TO finbot_app",
    "GRANT UPDATE (status, proposal, telegram_message_id, confirmed_txn_id, expires_at, "
    "updated_at) ON public.drafts TO finbot_app",
    "GRANT SELECT, INSERT ON public.pending_actions TO finbot_app",
    "GRANT UPDATE (payload, status, expires_at, completed_at) ON public.pending_actions "
    "TO finbot_app",
    "GRANT UPDATE (default_account_id) ON public.workspace_memberships TO finbot_app",
    "GRANT SELECT ON public.current_entries TO finbot_app",
)


def upgrade() -> None:
    statements = [*_TABLES, *_FUNCTIONS, *_TRIGGERS]
    for table in (*TENANT_TABLES, *PER_USER_TABLES):
        statements.append(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        statements.append(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    statements += [*_POLICIES, *_APP_GRANTS]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    statements = (
        "REVOKE UPDATE (default_account_id) ON public.workspace_memberships FROM finbot_app",
        "DROP VIEW public.current_entries",
        "ALTER TABLE public.workspace_memberships "
        "DROP CONSTRAINT workspace_memberships_default_account_fkey",
        "ALTER TABLE public.workspace_memberships DROP COLUMN default_account_id",
        # Tables in reverse dependency order: their policies, triggers, indexes and grants go too.
        "DROP TABLE public.pending_actions",
        "DROP TABLE public.drafts",
        "DROP TABLE public.transaction_revisions",
        "DROP TABLE public.transaction_entries",
        "DROP TABLE public.transactions",
        "DROP TABLE public.categories",
        "DROP TABLE public.accounts",
        "DROP FUNCTION public.ledger_txn_active_category()",
        "DROP FUNCTION public.ledger_entry_active_account()",
        "DROP FUNCTION public.ledger_txn_type_immutable()",
        "DROP FUNCTION public.ledger_append_only()",
        "DROP FUNCTION public.ledger_entries_balanced()",
    )
    for statement in statements:
        op.execute(statement)
