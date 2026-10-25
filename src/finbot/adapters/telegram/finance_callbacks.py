"""Callback data of the finance buttons: encode and strictly decode (docs/09 L1, T1).

Telegram limits callback data to 64 bytes and returns it verbatim from the client, so it is
untrusted input. It never carries a workspace: only a short draft reference, a transaction or
category ID, a version or a page. The services look every ID up **inside the caller's active
workspace** and authorize at execution time, so a forged or stale button can at most be
refused (NotFound/Forbidden/expired), never act elsewhere.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Final
from uuid import UUID

from finbot.domain.finance import (
    CATEGORY_MAX_PAGES,
    DraftField,
    EditField,
    HistoryFilter,
    InvalidInputError,
)
from finbot.domain.ledger import TxnType

PREFIX: Final = "f"
MAX_BYTES: Final = 64

_TYPE_CODE: Final = {TxnType.INCOME: "i", TxnType.EXPENSE: "e"}
_CODE_TYPE: Final = {v: k for k, v in _TYPE_CODE.items()}
_FIELD_CODE: Final = {
    EditField.AMOUNT: "a",
    EditField.CATEGORY: "c",
    EditField.DESCRIPTION: "d",
    EditField.DATE: "t",
}
_CODE_FIELD: Final = {v: k for k, v in _FIELD_CODE.items()}
_DRAFT_FIELD_CODE: Final = {
    DraftField.TYPE: "y",
    DraftField.AMOUNT: "a",
    DraftField.CATEGORY: "c",
    DraftField.DESCRIPTION: "d",
    DraftField.DATE: "t",
}
_CODE_DRAFT_FIELD: Final = {v: k for k, v in _DRAFT_FIELD_CODE.items()}
_REF_RE = re.compile(r"[0-9a-f]{8}")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DATE_RE = re.compile(r"\d{8}")
_MAX_VERSION: Final = 1_000_000
_MAX_CATEGORY_PAGE: Final = CATEGORY_MAX_PAGES - 1


class InvalidCallbackError(ValueError):
    """Malformed or unknown callback data (treated as a stale button)."""


@dataclass(frozen=True, slots=True)
class Callback:
    action: str
    ref: str | None = None
    txn_type: TxnType | None = None
    entity_id: UUID | None = None
    version: int | None = None
    field: EditField | None = None
    draft_field: DraftField | None = None
    page: int = 0
    history: HistoryFilter | None = None
    flag: bool = False


def _join(*parts: str) -> str:
    data = ":".join((PREFIX, *parts))
    if len(data.encode()) > MAX_BYTES:  # pragma: no cover - every format below fits
        raise ValueError("callback data too long")
    return data


# ----------------------------------------------------------------------------- encode


def draft_type(ref: str, txn_type: TxnType) -> str:
    return _join("ty", ref, _TYPE_CODE[txn_type])


def draft_category(ref: str, category_id: UUID) -> str:
    return _join("ca", ref, str(category_id))


def draft_skip(ref: str) -> str:
    return _join("sk", ref)


def draft_category_page(ref: str, page: int) -> str:
    return _join("cp", ref, str(page))


def draft_review(ref: str) -> str:
    """✏️ Ubah on a confirmation: choose the field to correct."""
    return _join("ub", ref)


def draft_revise(ref: str, draft_field: DraftField) -> str:
    return _join("uf", ref, _DRAFT_FIELD_CODE[draft_field])


def draft_back(ref: str) -> str:
    return _join("rv", ref)


def draft_confirm(ref: str) -> str:
    return _join("ok", ref)


def draft_cancel(ref: str) -> str:
    """❌ of one conversation (a draft or a category prompt): cancels only that one."""
    return _join("no", ref)


def _date_code(value: date | None) -> str:
    return value.strftime("%Y%m%d") if value else "-"


def history(page: int, history_filter: HistoryFilter) -> str:
    type_code = _TYPE_CODE[history_filter.txn_type] if history_filter.txn_type else "a"
    if history_filter.category_id is not None:
        return _join("kh", str(history_filter.category_id), str(page))
    return _join(
        "h",
        str(page),
        type_code,
        _date_code(history_filter.date_from),
        _date_code(history_filter.date_to),
        "1" if history_filter.voided else "0",
    )


def transaction(transaction_id: UUID) -> str:
    return _join("tx", str(transaction_id))


def edit_menu(transaction_id: UUID, version: int) -> str:
    return _join("ed", str(transaction_id), str(version))


def edit_field(edit: EditField, transaction_id: UUID, version: int) -> str:
    return _join("ef", _FIELD_CODE[edit], str(transaction_id), str(version))


def delete_ask(transaction_id: UUID, version: int) -> str:
    return _join("dl", str(transaction_id), str(version))


def delete_yes(transaction_id: UUID, version: int) -> str:
    return _join("dy", str(transaction_id), str(version))


def restore(transaction_id: UUID, version: int) -> str:
    return _join("rs", str(transaction_id), str(version))


def category_list(txn_type: TxnType | None, page: int = 0, *, archived: bool = False) -> str:
    return _join(
        "kl", _TYPE_CODE[txn_type] if txn_type else "a", str(page), "1" if archived else "0"
    )


def category_detail(category_id: UUID) -> str:
    return _join("kd", str(category_id))


def category_new(txn_type: TxnType) -> str:
    return _join("kn", _TYPE_CODE[txn_type])


def category_rename(category_id: UUID) -> str:
    return _join("kr", str(category_id))


def category_active(category_id: UUID, active: bool) -> str:
    return _join("ka", str(category_id), "1" if active else "0")


# ----------------------------------------------------------------------------- decode


def _ref(value: str) -> str:
    if not _REF_RE.fullmatch(value):
        raise InvalidCallbackError("ref")
    return value


def _uuid(value: str) -> UUID:
    if not _UUID_RE.fullmatch(value):
        raise InvalidCallbackError("id")
    return UUID(value)


def _int(value: str, upper: int) -> int:
    if not value.isdigit() or len(value) > len(str(upper)) or int(value) > upper:
        raise InvalidCallbackError("number")
    return int(value)


def _txn_type(value: str) -> TxnType:
    if value not in _CODE_TYPE:
        raise InvalidCallbackError("type")
    return _CODE_TYPE[value]


def _flag(value: str) -> bool:
    if value not in ("0", "1"):
        raise InvalidCallbackError("flag")
    return value == "1"


def _date(value: str) -> date | None:
    if value == "-":
        return None
    if not _DATE_RE.fullmatch(value):
        raise InvalidCallbackError("date")
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        raise InvalidCallbackError("date") from None


_ARITY: Final = {
    "ty": 2, "ca": 2, "cp": 2, "sk": 1, "ub": 1, "uf": 2, "rv": 1, "ok": 1, "no": 1, "h": 5,
    "kh": 2, "tx": 1, "ed": 2, "ef": 3, "dl": 2, "dy": 2, "rs": 2, "kl": 3, "kd": 1, "kn": 1,
    "kr": 1, "ka": 2,
}  # fmt: skip


def parse(data: str | None) -> Callback:  # noqa: PLR0911, PLR0912 - one branch per button family
    if not data or len(data.encode()) > MAX_BYTES:
        raise InvalidCallbackError("size")
    prefix, _, rest = data.partition(":")
    action, _, tail = rest.partition(":")
    args = tail.split(":") if tail else []
    if prefix != PREFIX or _ARITY.get(action) != len(args):
        raise InvalidCallbackError("shape")
    try:
        match action:
            case "ty":
                return Callback(action, ref=_ref(args[0]), txn_type=_txn_type(args[1]))
            case "ca":
                return Callback(action, ref=_ref(args[0]), entity_id=_uuid(args[1]))
            case "cp":
                return Callback(action, ref=_ref(args[0]), page=_int(args[1], _MAX_CATEGORY_PAGE))
            case "sk" | "ub" | "rv" | "ok" | "no":
                return Callback(action, ref=_ref(args[0]))
            case "uf":
                if args[1] not in _CODE_DRAFT_FIELD:
                    raise InvalidCallbackError("field")
                return Callback(action, ref=_ref(args[0]), draft_field=_CODE_DRAFT_FIELD[args[1]])
            case "h":
                type_code = args[1]
                txn_type = None if type_code == "a" else _txn_type(type_code)
                filt = HistoryFilter(
                    txn_type=txn_type,
                    date_from=_date(args[2]),
                    date_to=_date(args[3]),
                    voided=_flag(args[4]),
                )
                return Callback(action, page=_int(args[0], 999), history=filt)
            case "kh":
                category_id = _uuid(args[0])
                return Callback(
                    action,
                    entity_id=category_id,
                    page=_int(args[1], 999),
                    history=HistoryFilter(category_id=category_id),
                )
            case "tx" | "kd" | "kr":
                return Callback(action, entity_id=_uuid(args[0]))
            case "ed" | "dl" | "dy" | "rs":
                return Callback(
                    action, entity_id=_uuid(args[0]), version=_int(args[1], _MAX_VERSION)
                )
            case "ef":
                if args[0] not in _CODE_FIELD:
                    raise InvalidCallbackError("field")
                return Callback(
                    action,
                    field=_CODE_FIELD[args[0]],
                    entity_id=_uuid(args[1]),
                    version=_int(args[2], _MAX_VERSION),
                )
            case "kl":
                return Callback(
                    action,
                    txn_type=None if args[0] == "a" else _txn_type(args[0]),
                    page=_int(args[1], _MAX_CATEGORY_PAGE),
                    flag=_flag(args[2]),
                )
            case "kn":
                return Callback(action, txn_type=_txn_type(args[0]))
            case "ka":
                return Callback(action, entity_id=_uuid(args[0]), flag=_flag(args[1]))
    except InvalidCallbackError:
        raise
    except (ValueError, InvalidInputError):  # e.g. a HistoryFilter with from > to
        raise InvalidCallbackError("value") from None
    raise InvalidCallbackError("action")  # pragma: no cover - _ARITY covers every action
