"""HTML text and inline keyboards for the finance features (CLAUDE.md rule 11, AT-RENDER-01).

Pure functions: they return the message text and keyboard rows of ``(label, callback_data)``.
Every user- or database-supplied string (workspace and category names, descriptions) goes
through :func:`html.escape`; amounts go through ``format_rupiah``. Every confirmation names
the workspace and its type (G-8). Messages stay well under Telegram's 4,096 characters: lists
are paged (history) or capped (category breakdowns).
"""

import html
from datetime import date, datetime
from typing import Final
from zoneinfo import ZoneInfo

from finbot.adapters.telegram import finance_callbacks as cb
from finbot.adapters.telegram import messages_id as msg
from finbot.domain.finance import (
    HISTORY_PAGE_SIZE,
    CaptureState,
    CaptureStep,
    Category,
    CategoryTotal,
    DraftPurpose,
    DraftView,
    EditField,
    EditState,
    HistoryFilter,
    TransactionRecord,
)
from finbot.domain.ledger import TxnType
from finbot.domain.money import format_rupiah
from finbot.services.capture import CategoryPrompt, ConfirmResult
from finbot.services.finance import WorkspaceInfo
from finbot.services.ledger import (
    BalanceResult,
    CategoryListing,
    HistoryResult,
    SummaryResult,
    TransactionDetail,
)

Button = tuple[str, str]
Keyboard = list[list[Button]]
Reply = tuple[str, Keyboard]

SUMMARY_CATEGORY_LIMIT: Final = 15
#: Telegram allows at most 100 inline buttons per message; keep room for navigation rows.
CATEGORY_BUTTON_LIMIT: Final = 90
DESCRIPTION_PREVIEW: Final = 40
MONTH_LABELS: Final = (
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)  # fmt: skip


def esc(value: str) -> str:
    return html.escape(value, quote=False)


def fmt_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def fmt_datetime(value: datetime, timezone: str) -> str:
    return value.astimezone(ZoneInfo(timezone)).strftime("%d/%m/%Y %H:%M")


def workspace_line(name: str, kind: str) -> str:
    return msg.WORKSPACE_LINE.format(icon=msg.WORKSPACE_ICON.get(kind, "🏢"), name=esc(name))


def _ws(info: WorkspaceInfo) -> str:
    return workspace_line(info.name, info.kind)


def type_label(txn_type: TxnType | None) -> str:
    if txn_type is None:
        return msg.NO_DESCRIPTION
    return f"{msg.TYPE_ICON[txn_type.value]} {msg.TYPE_LABEL[txn_type.value]}"


def _rows(buttons: list[Button], per_row: int = 2) -> Keyboard:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


# ------------------------------------------------------------------------------ drafts


def render_draft(view: DraftView) -> Reply:
    if isinstance(view.state, EditState):
        return _render_edit(view, view.state)
    return _render_capture(view, view.state)


def _capture_lines(view: DraftView, state: CaptureState) -> list[str]:
    lines = [msg.DRAFT_TITLE_NEW, workspace_line(view.workspace_name, view.workspace_kind)]
    if state.txn_type is not None:
        lines.append(f"{msg.FIELD_TYPE}: {type_label(state.txn_type)}")
    if state.amount is not None:
        lines.append(f"{msg.FIELD_AMOUNT}: <b>{format_rupiah(state.amount)}</b>")
    if view.category is not None:
        lines.append(f"{msg.FIELD_CATEGORY}: {esc(view.category.name)}")
    if state.description_done:
        lines.append(f"{msg.FIELD_DESCRIPTION}: {esc(state.description or msg.NO_DESCRIPTION)}")
    lines.append(f"{msg.FIELD_DATE}: {fmt_date(state.txn_date)}")
    if state.rounded:
        lines.append(msg.ROUNDED_NOTE)
    return lines


def _category_buttons(view: DraftView) -> Keyboard:
    categories = view.categories[:CATEGORY_BUTTON_LIMIT]
    return _rows([(c.name, cb.draft_category(view.ref, c.id)) for c in categories])


def _render_capture(view: DraftView, state: CaptureState) -> Reply:
    lines = _capture_lines(view, state)
    cancel = [(msg.BUTTON_CANCEL, cb.draft_cancel(view.ref))]
    match state.step:
        case CaptureStep.TYPE:
            lines.append("\n" + msg.ASK_TYPE)
            keyboard = [
                [
                    (msg.BUTTON_INCOME, cb.draft_type(view.ref, TxnType.INCOME)),
                    (msg.BUTTON_EXPENSE, cb.draft_type(view.ref, TxnType.EXPENSE)),
                ],
                cancel,
            ]
        case CaptureStep.AMOUNT:
            lines.append("\n" + msg.ASK_AMOUNT)
            keyboard = [cancel]
        case CaptureStep.CATEGORY:
            lines.append("\n" + (msg.ASK_CATEGORY if view.categories else msg.ASK_CATEGORY_EMPTY))
            keyboard = [*_category_buttons(view), cancel]
        case CaptureStep.DESCRIPTION:
            lines.append("\n" + msg.ASK_DESCRIPTION)
            keyboard = [[(msg.BUTTON_SKIP, cb.draft_skip(view.ref))], cancel]
        case CaptureStep.DATE:
            lines.append("\n" + msg.ASK_DATE)
            keyboard = [cancel]
        case CaptureStep.CONFIRM:
            lines.append("\n" + msg.ASK_CONFIRM)
            keyboard = [
                [
                    (msg.BUTTON_SAVE, cb.draft_confirm(view.ref)),
                    (msg.BUTTON_DATE, cb.draft_date(view.ref)),
                ],
                cancel,
            ]
    return "\n".join(lines), keyboard


def _render_edit(view: DraftView, state: EditState) -> Reply:
    lines = [msg.DRAFT_TITLE_EDIT, workspace_line(view.workspace_name, view.workspace_kind)]
    target = view.target
    if target is not None:
        lines.append(_record_summary(target))
    label = msg.EDIT_FIELD_LABEL[state.field.value]
    cancel = [(msg.BUTTON_CANCEL, cb.draft_cancel(view.ref))]
    if state.step is CaptureStep.CONFIRM:
        lines.append(f"\n{label} baru: <b>{_edit_value(view, state)}</b>")
        if state.rounded:
            lines.append(msg.ROUNDED_NOTE)
        lines.append(msg.ASK_CONFIRM)
        return "\n".join(lines), [[(msg.BUTTON_SAVE, cb.draft_confirm(view.ref))], cancel]
    match state.step:
        case CaptureStep.AMOUNT:
            lines.append("\n" + msg.ASK_AMOUNT)
            keyboard = [cancel]
        case CaptureStep.CATEGORY:
            lines.append("\n" + (msg.ASK_CATEGORY if view.categories else msg.ASK_CATEGORY_EMPTY))
            keyboard = [*_category_buttons(view), cancel]
        case CaptureStep.DESCRIPTION:
            lines.append("\n" + msg.ASK_DESCRIPTION_EDIT)
            keyboard = [[(msg.BUTTON_CLEAR_DESCRIPTION, cb.draft_skip(view.ref))], cancel]
        case _:
            lines.append("\n" + msg.ASK_DATE)
            keyboard = [cancel]
    return "\n".join(lines), keyboard


def _edit_value(view: DraftView, state: EditState) -> str:
    match state.field:
        case EditField.AMOUNT:
            return format_rupiah(state.amount or 0)
        case EditField.CATEGORY:
            return esc(view.category.name) if view.category else msg.NO_DESCRIPTION
        case EditField.DESCRIPTION:
            return esc(state.description) if state.description else msg.NO_DESCRIPTION
        case EditField.DATE:
            return fmt_date(state.txn_date) if state.txn_date else msg.NO_DESCRIPTION


def render_confirmed(result: ConfirmResult) -> Reply:
    record = result.record
    if result.purpose is DraftPurpose.EDIT:
        return f"{msg.UPDATED}\n\n{_record_summary(record)}", _detail_shortcut(record)
    template = msg.SAVED_AGAIN if result.already else msg.SAVED
    text = template.format(
        type=type_label(record.type),
        amount=format_rupiah(record.amount),
        category=esc(record.category_name or msg.NO_DESCRIPTION),
        date=fmt_date(record.txn_date),
    )
    return f"{text}\n{_ws(result.workspace)}", _detail_shortcut(record)


def _detail_shortcut(record: TransactionRecord) -> Keyboard:
    return [[(msg.BUTTON_DETAIL, cb.transaction(record.id))]]


# ----------------------------------------------------------------------- transactions


def _record_summary(record: TransactionRecord) -> str:
    lines = [
        f"{msg.FIELD_TYPE}: {type_label(record.type)}",
        f"{msg.FIELD_AMOUNT}: <b>{format_rupiah(record.amount)}</b>",
        f"{msg.FIELD_CATEGORY}: {esc(record.category_name or msg.NO_DESCRIPTION)}"
        + (msg.CATEGORY_FLAG_ARCHIVED if record.category_archived else ""),
        f"{msg.FIELD_DESCRIPTION}: {esc(record.description or msg.NO_DESCRIPTION)}",
        f"{msg.FIELD_DATE}: {fmt_date(record.txn_date)}",
    ]
    return "\n".join(lines)


def render_detail(detail: TransactionDetail) -> Reply:
    record = detail.record
    lines = [
        msg.DETAIL_TITLE,
        _ws(detail.workspace),
        _record_summary(record),
        f"{msg.FIELD_ACCOUNT}: {esc(record.account_name or msg.NO_DESCRIPTION)}",
        f"{msg.FIELD_STATUS}: {msg.DETAIL_STATUS[record.status.value]}",
        msg.DETAIL_META.format(
            version=record.version,
            created=fmt_datetime(record.created_at, detail.workspace.timezone),
        ),
    ]
    buttons: list[Button] = []
    if detail.can_update:
        buttons.append((msg.BUTTON_EDIT, cb.edit_menu(record.id, record.version)))
    if detail.can_delete:
        buttons.append((msg.BUTTON_DELETE, cb.delete_ask(record.id, record.version)))
    if detail.can_restore:
        buttons.append((msg.BUTTON_RESTORE, cb.restore(record.id, record.version)))
    return "\n".join(lines), [buttons] if buttons else []


def render_edit_menu(detail: TransactionDetail) -> Reply:
    record = detail.record
    fields = [EditField.AMOUNT, EditField.CATEGORY, EditField.DESCRIPTION, EditField.DATE]
    buttons = [
        (msg.EDIT_FIELD_LABEL[f.value], cb.edit_field(f, record.id, record.version)) for f in fields
    ]
    text = "\n".join([msg.DRAFT_TITLE_EDIT, _ws(detail.workspace), _record_summary(record), "",
                      msg.CHOOSE_EDIT_FIELD])  # fmt: skip
    return text, [*_rows(buttons), [(msg.BUTTON_BACK, cb.transaction(record.id))]]


def render_delete_confirm(detail: TransactionDetail) -> Reply:
    record = detail.record
    text = "\n".join([_ws(detail.workspace), _record_summary(record), "", msg.CONFIRM_DELETE])
    return text, [
        [
            (msg.BUTTON_CONFIRM_DELETE, cb.delete_yes(record.id, record.version)),
            (msg.BUTTON_BACK, cb.transaction(record.id)),
        ]
    ]


def _filter_text(result: HistoryResult) -> str | None:
    filt = result.page.filter
    parts = []
    if filt.txn_type is not None:
        parts.append(msg.TYPE_LABEL[filt.txn_type.value].lower())
    if result.category is not None:
        parts.append(f"kategori {esc(result.category.name)}")
    if filt.date_from and filt.date_to:
        parts.append(f"{fmt_date(filt.date_from)} s.d. {fmt_date(filt.date_to)}")
    return ", ".join(parts) or None


def render_history(result: HistoryResult) -> Reply:
    page = result.page
    title = msg.HISTORY_TITLE_VOIDED if page.filter.voided else msg.HISTORY_TITLE
    lines = [title, _ws(result.workspace)]
    if (filters := _filter_text(result)) is not None:
        lines.append(msg.HISTORY_FILTER.format(filters=filters))
    if not page.items:
        lines.append("\n" + msg.HISTORY_EMPTY)
        return "\n".join(lines), []
    lines.append("")
    number_buttons: list[Button] = []
    for n, record in enumerate(page.items, start=1 + page.page * HISTORY_PAGE_SIZE):
        description = record.description or ""
        if len(description) > DESCRIPTION_PREVIEW:
            description = description[: DESCRIPTION_PREVIEW - 1] + "…"
        lines.append(
            msg.HISTORY_ITEM.format(
                n=n,
                date=fmt_date(record.txn_date),
                icon=msg.TYPE_ICON[record.type.value],
                amount=format_rupiah(record.amount),
                category=esc(record.category_name or msg.NO_DESCRIPTION),
                description=f" · {esc(description)}" if description else "",
            )
        )
        number_buttons.append((str(n), cb.transaction(record.id)))
    lines.append("\n" + msg.HISTORY_PAGE.format(page=page.page + 1) + " · " + msg.HISTORY_HINT)
    nav: list[Button] = []
    if page.page > 0:
        nav.append((msg.BUTTON_PREV, cb.history(page.page - 1, page.filter)))
    if page.has_next:
        nav.append((msg.BUTTON_NEXT, cb.history(page.page + 1, page.filter)))
    keyboard = _rows(number_buttons, per_row=5)
    if nav:
        keyboard.append(nav)
    return "\n".join(lines), keyboard


# ------------------------------------------------------------------------- categories


def _kind_label(kind: TxnType) -> str:
    return msg.TYPE_LABEL[kind.value]


def render_categories(listing: CategoryListing, kind: TxnType | None) -> Reply:
    lines = [msg.CATEGORIES_TITLE, _ws(listing.workspace)]
    buttons: list[Button] = []
    for current in (TxnType.INCOME, TxnType.EXPENSE):
        if kind is not None and current is not kind:
            continue
        items = [c for c in listing.categories if c.kind is current]
        lines.append("\n" + msg.CATEGORIES_KIND.format(kind=_kind_label(current)))
        for category in items:
            flags = msg.CATEGORY_FLAG_DEFAULT if category.is_default else ""
            flags += msg.CATEGORY_FLAG_ARCHIVED if category.archived else ""
            lines.append(msg.CATEGORY_ITEM.format(name=esc(category.name), flags=flags))
            buttons.append((category.name, cb.category_detail(category.id)))
    keyboard = _rows(buttons[:CATEGORY_BUTTON_LIMIT], per_row=3)
    if listing.can_manage:
        keyboard.append(
            [
                (msg.BUTTON_NEW_INCOME_CATEGORY, cb.category_new(TxnType.INCOME)),
                (msg.BUTTON_NEW_EXPENSE_CATEGORY, cb.category_new(TxnType.EXPENSE)),
            ]
        )
    return "\n".join(lines), keyboard


def render_category(category: Category, can_manage: bool) -> Reply:
    text = msg.CATEGORY_DETAIL.format(
        name=esc(category.name),
        kind=_kind_label(category.kind),
        status=msg.CATEGORY_STATUS[category.archived],
    )
    buttons: list[Button] = [
        (msg.BUTTON_CATEGORY_HISTORY, cb.history(0, HistoryFilter(category_id=category.id)))
    ]
    if can_manage:
        buttons.append((msg.BUTTON_RENAME, cb.category_rename(category.id)))
        if category.archived:
            buttons.append((msg.BUTTON_ACTIVATE, cb.category_active(category.id, True)))
        elif not category.is_default:
            buttons.append((msg.BUTTON_DEACTIVATE, cb.category_active(category.id, False)))
    return text, [*_rows(buttons), [(msg.BUTTON_BACK, cb.category_list(category.kind))]]


def render_category_prompt(prompt: CategoryPrompt) -> Reply:
    if prompt.category is not None:
        text = msg.CATEGORY_ASK_RENAME.format(name=esc(prompt.category.name))
    else:
        kind = TxnType(prompt.action.payload["kind"])
        text = msg.CATEGORY_ASK_NEW.format(kind=_kind_label(kind).lower())
    return f"{_ws(prompt.workspace)}\n{text}", []


# ---------------------------------------------------------------------------- reports


def render_balance(result: BalanceResult) -> Reply:
    report = result.report
    lines = [msg.BALANCE_TITLE.format(date=fmt_date(report.as_of)), _ws(result.workspace), ""]
    for account in report.accounts:
        warning = msg.BALANCE_NEGATIVE if account.balance < 0 else ""
        lines.append(
            msg.BALANCE_ACCOUNT.format(
                name=esc(account.name), amount=format_rupiah(account.balance), warning=warning
            )
        )
    lines.append("")
    for currency, total in sorted(report.recorded_balance().items()):
        lines.append(msg.BALANCE_TOTAL.format(currency=esc(currency), amount=format_rupiah(total)))
    for totals in report.all_time:
        lines.append(
            msg.BALANCE_ALL_TIME.format(
                currency=esc(totals.currency),
                income=format_rupiah(totals.income),
                expense=format_rupiah(totals.expense),
                net=format_rupiah(totals.net),
            )
        )
    lines.append("\n" + msg.BALANCE_NOTE)
    return "\n".join(lines), []


def _category_lines(header: str, totals: tuple[CategoryTotal, ...]) -> list[str]:
    if not totals:
        return []
    lines = ["", header]
    for total in totals[:SUMMARY_CATEGORY_LIMIT]:
        lines.append(
            msg.SUMMARY_CATEGORY.format(
                name=esc(total.name), amount=format_rupiah(total.total), count=total.count
            )
        )
    if len(totals) > SUMMARY_CATEGORY_LIMIT:
        lines.append(msg.SUMMARY_MORE.format(count=len(totals) - SUMMARY_CATEGORY_LIMIT))
    return lines


def render_summary(result: SummaryResult) -> Reply:
    summary = result.summary
    month = f"{MONTH_LABELS[summary.month.month - 1]} {summary.month.year}"
    lines = [msg.SUMMARY_TITLE.format(month=month), _ws(result.workspace)]
    if not summary.totals:
        lines.append("\n" + msg.SUMMARY_EMPTY)
        return "\n".join(lines), []
    for totals in summary.totals:
        lines.append("")
        if len(summary.totals) > 1:
            lines.append(msg.SUMMARY_CURRENCY.format(currency=esc(totals.currency)))
        lines.append(
            msg.SUMMARY_TOTALS.format(
                income=format_rupiah(totals.income),
                expense=format_rupiah(totals.expense),
                net=format_rupiah(totals.net),
            )
        )
    lines += _category_lines(msg.SUMMARY_EXPENSE_HEADER, summary.expense_by_category)
    lines += _category_lines(msg.SUMMARY_INCOME_HEADER, summary.income_by_category)
    return "\n".join(lines), []
