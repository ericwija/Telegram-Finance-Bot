"""The workspace context of one request (docs/09 §3, L1/L2).

Built by the unit of work from **server-side state only**: the user's active membership read
inside the request's database transaction (never from callback data). One context per
database transaction; roles are never cached across requests (G-2).
"""

from dataclasses import dataclass
from uuid import UUID

from finbot.domain.identity import UserStatus


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    user_id: UUID
    user_status: UserStatus
    workspace_id: UUID
    workspace_kind: str  # personal | business
    workspace_name: str
    workspace_status: str  # active | pending_deletion
    timezone: str
    currency: str
    role: str  # owner | admin | editor | viewer
    default_account_id: UUID | None
