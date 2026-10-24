"""AT-LOG-01 (docs/07 §5) skeleton: structured JSON logs never carry secrets or user content."""

import io
import json
import logging
import uuid
from collections.abc import Iterator
from enum import StrEnum
from typing import Any

import pytest

from finbot.logging import (
    ALLOWED_EXTRA_FIELDS,
    REDACTED,
    JsonFormatter,
    configure_logging,
    safe,
    scrub,
)
from tests.conftest import fake_telegram_token

SENTINEL = "ZZSECRETZZ"
# Built at runtime so secret scanners never see a PEM header literal in the repository.
_PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
_PEM_END = "-----END " + "PRIVATE KEY-----"


class Outcome(StrEnum):
    OK = "ok"


@pytest.fixture
def capture() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = configure_logging("DEBUG", env="test", stream=stream)
    yield stream
    logging.getLogger().removeHandler(handler)


def _records(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _last(stream: io.StringIO) -> dict[str, Any]:
    return _records(stream)[-1]


def test_output_is_one_json_object_per_line(capture: io.StringIO) -> None:
    log = logging.getLogger("finbot.test")
    log.info("first")
    log.warning("second")
    records = _records(capture)
    assert [r["message"] for r in records] == ["first", "second"]
    first = records[0]
    assert first["level"] == "INFO"
    assert first["logger"] == "finbot.test"
    assert first["env"] == "test"
    assert first["ts"].endswith("+00:00")


def test_allowed_extras_kept_and_others_dropped(capture: io.StringIO) -> None:
    request_id = uuid.uuid4()
    logging.getLogger("finbot.test").info(
        "draft created",
        extra={
            "request_id": request_id,
            "workspace_id": str(uuid.uuid4()),
            "duration_ms": 12,
            "outcome": Outcome.OK,
            "description": f"makan siang {SENTINEL}",
            "text": SENTINEL,
            "amount": 35000,
        },
    )
    record = _last(capture)
    assert record["request_id"] == str(request_id)
    assert record["duration_ms"] == 12
    assert record["outcome"] == "ok"
    assert record["dropped_fields"] == ["amount", "description", "text"]
    raw = capture.getvalue()
    assert SENTINEL not in raw
    assert "35000" not in raw


def test_string_arguments_are_redacted_unless_marked_safe(capture: io.StringIO) -> None:
    log = logging.getLogger("finbot.test")
    log.info("message from user: %s", f"beli kopi {SENTINEL}")
    assert _last(capture)["message"] == f"message from user: {REDACTED}"
    log.info("command %s handled in %d ms", safe("/start"), 7)
    assert _last(capture)["message"] == "command /start handled in 7 ms"
    log.info("enum %s, uuid %s, none %s", Outcome.OK, uuid.UUID(int=1), None)
    assert _last(capture)["message"].startswith("enum ok, uuid 00000000-")
    assert SENTINEL not in capture.getvalue()


def test_mapping_arguments_are_redacted(capture: io.StringIO) -> None:
    logging.getLogger("finbot.test").info("%(who)s sent %(n)d", {"who": SENTINEL, "n": 2})
    assert _last(capture)["message"] == f"{REDACTED} sent 2"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"token={SENTINEL}", SENTINEL),
        (f"password: {SENTINEL}", SENTINEL),
        (f"api_key='{SENTINEL}'", SENTINEL),
        (f"Authorization: Bearer {SENTINEL}", SENTINEL),
        (f"postgresql://finbot_app:{SENTINEL}@postgres:5432/finbot_dev", SENTINEL),
        ("AIza" + "B" * 35, "AIza" + "B" * 35),
        (_PEM_BEGIN + "\nabc\n" + _PEM_END, "abc"),
    ],
)
def test_credentials_scrubbed_from_message(text: str, secret: str) -> None:
    assert secret not in scrub(text)


def test_telegram_token_scrubbed_even_in_literal_message(capture: io.StringIO) -> None:
    token = fake_telegram_token()
    logging.getLogger("finbot.test").error("polling failed for " + token)
    assert token not in capture.getvalue()
    assert "[REDACTED:telegram-token]" in _last(capture)["message"]


@pytest.mark.parametrize(
    "text", ["Rp 35.000", "rp1.250.000", "beli 50rb", "gaji 5 jt", "1,5juta", "harga 70k"]
)
def test_rupiah_amounts_scrubbed(text: str) -> None:
    assert "[REDACTED:amount]" in scrub(text)


def test_long_digit_runs_scrubbed_but_uuids_and_small_numbers_kept() -> None:
    assert scrub("telegram user 123456789") == "telegram user [REDACTED:number]"
    assert scrub("took 1500 ms, 3 retries") == "took 1500 ms, 3 retries"


def test_uuid_extras_not_mangled(capture: io.StringIO) -> None:
    value = "12345678-1234-5678-1234-567812345678"
    logging.getLogger("finbot.test").info("x", extra={"user_id": value})
    assert _last(capture)["user_id"] == value


def test_exception_text_and_traceback_scrubbed(capture: io.StringIO) -> None:
    log = logging.getLogger("finbot.test")
    try:
        raise RuntimeError(f"db failed: postgresql://u:{SENTINEL}@db/x token={SENTINEL}")
    except RuntimeError:
        log.exception("operation failed")
    record = _last(capture)
    assert record["exception"]["type"] == "RuntimeError"
    assert SENTINEL not in capture.getvalue()


def test_non_string_message_objects_are_never_rendered(capture: io.StringIO) -> None:
    logging.getLogger("finbot.test").info({"password": SENTINEL, "text": "beli kopi"})
    assert _last(capture)["message"] == "[dict object redacted]"
    assert SENTINEL not in capture.getvalue()


def test_exception_as_message_is_scrubbed(capture: io.StringIO) -> None:
    logging.getLogger("finbot.test").error(ValueError(f"bad token={SENTINEL}"))
    assert _last(capture)["message"] == "ValueError: bad token=[REDACTED]"


def test_quoted_key_value_pairs_scrubbed() -> None:
    assert SENTINEL not in scrub(str({"password": SENTINEL, "api_key": SENTINEL}))
    assert SENTINEL not in scrub(f'{{"token": "{SENTINEL}"}}')


def test_bad_format_arguments_do_not_raise() -> None:
    # Isolated logger: pytest's own capture handler re-raises formatting errors by design.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("finbot.test.isolated")
    log.propagate = False
    log.addHandler(handler)
    try:
        log.info("%d items", "not-a-number")
    finally:
        log.removeHandler(handler)
    assert "unformattable" in json.loads(stream.getvalue())["message"]


def test_configure_logging_is_idempotent() -> None:
    first = configure_logging("INFO", stream=io.StringIO())
    second = configure_logging("INFO", stream=io.StringIO())
    root = logging.getLogger()
    try:
        assert first not in root.handlers
        assert second in root.handlers
    finally:
        root.removeHandler(second)


def test_allow_list_matches_nfr_obs_01() -> None:
    assert {"request_id", "user_id", "workspace_id", "event", "duration_ms", "outcome"} <= (
        ALLOWED_EXTRA_FIELDS
    )
    assert not {"description", "text", "amount", "message_text"} & ALLOWED_EXTRA_FIELDS
