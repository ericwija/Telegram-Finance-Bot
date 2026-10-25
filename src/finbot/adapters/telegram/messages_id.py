"""Indonesian message catalogue for the Telegram adapter (CLAUDE.md conventions).

Messages are HTML (``parse_mode=HTML``). Placeholders are filled only through
``finbot.adapters.telegram.start.render`` (or later helpers), which ``html.escape()`` every
interpolated value.
"""

from typing import Final

START_ONBOARDED: Final = (
    "Selamat datang! Akun Anda sudah aktif.\nRuang kerja aktif: <b>{workspace}</b>"
)
START_WELCOME_BACK: Final = "Selamat datang kembali.\nRuang kerja aktif: <b>{workspace}</b>"
START_NEEDS_INVITE: Final = (
    "Bot ini hanya untuk pengguna yang diundang. Minta tautan undangan kepada admin, "
    "lalu buka tautan tersebut untuk mendaftar."
)
START_INVITE_REFUSED: Final = (
    "Tautan undangan tidak valid atau sudah tidak berlaku. Minta tautan baru kepada admin."
)
START_ACCOUNT_UNAVAILABLE: Final = "Akun Anda sedang tidak aktif. Silakan hubungi admin."
