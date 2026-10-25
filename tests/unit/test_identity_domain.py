"""Onboarding token parsing, hashing and display-name normalisation (FR-IDN-01/05, T17)."""

import hashlib

import pytest

from finbot.domain.identity import (
    FALLBACK_DISPLAY_NAME,
    OnboardingToken,
    normalize_display_name,
    parse_start_payload,
)

TOKEN = "A" * 11 + "b-_9" + "z" * 7  # 22 characters


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        TOKEN,  # no prefix
        "lnk_" + TOKEN,  # a future link token, not onboarding
        "inv_" + TOKEN,
        "onb_",
        "onb_" + TOKEN[:21],  # too short (< 128 bits)
        "onb_" + "a" * 61,  # longer than Telegram allows
        "onb_" + TOKEN[:-1] + "!",
        "onb_" + TOKEN + " ",
        "ONB_" + TOKEN,
    ],
)
def test_only_well_formed_onboarding_payloads_parse(payload: str | None) -> None:
    assert parse_start_payload(payload) is None


def test_token_is_hashed_with_sha256_and_never_shown() -> None:
    token = parse_start_payload("onb_" + TOKEN)
    assert token == OnboardingToken(TOKEN)
    assert token.digest() == hashlib.sha256(TOKEN.encode()).digest()
    assert len(token.digest()) == 32
    assert TOKEN not in repr(token)
    assert TOKEN not in str(token)


@pytest.mark.parametrize(
    ("first_name", "expected"),
    [
        ("Budi", "Budi"),
        ("  Siti   Rahma  ", "Siti Rahma"),
        ("Bu\x00di\u202e", "Budi"),  # control and bidi-override characters dropped
        ("Na\u0303o", "N\u00e3o"),  # NFC
        ("x" * 80, "x" * 64),
        ("", FALLBACK_DISPLAY_NAME),
        (None, FALLBACK_DISPLAY_NAME),
        ("\u200b\u200b", FALLBACK_DISPLAY_NAME),
        ("<b>&</b>", "<b>&</b>"),  # stored as text; escaped only when rendered
    ],
)
def test_display_name_fits_the_users_column(first_name: str | None, expected: str) -> None:
    name = normalize_display_name(first_name)
    assert name == expected
    assert 1 <= len(name) <= 64
