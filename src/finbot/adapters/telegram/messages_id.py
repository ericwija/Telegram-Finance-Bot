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

# --- global (NFR-SEC-07, G-4) -------------------------------------------------------------
RATE_LIMITED: Final = "Terlalu banyak permintaan. Silakan tunggu sebentar, lalu coba lagi."
NOT_PERMITTED: Final = "Anda tidak memiliki izin untuk tindakan ini."
GENERIC_ERROR: Final = "Maaf, terjadi kesalahan. Silakan coba lagi nanti."

# --- platform admin: onboarding invites (docs/08 §3.3) --------------------------------------
INVITE_CREATED: Final = (
    "Undangan dibuat: sekali pakai, berlaku {days} hari.\n"
    "ID: <code>{invite_id}</code>\n\n"
    "Kirim tautan ini secara pribadi kepada satu orang. Tautan tidak disimpan dan tidak dapat "
    "ditampilkan lagi:\n{link}"
)
INVITE_NOTE_TOO_LONG: Final = "Catatan terlalu panjang (maksimal 200 karakter)."
INVITE_LIST_EMPTY: Final = "Tidak ada undangan."
INVITE_LIST_HEADER: Final = "Undangan ({count}):"
INVITE_LIST_ITEM: Final = "• <code>{invite_id}</code> — {status}, berlaku sampai {expires}{note}"
INVITE_STATUS: Final = {
    "active": "aktif",
    "used": "terpakai",
    "revoked": "dicabut",
    "expired": "kedaluwarsa",
}
INVITE_REVOKE_USAGE: Final = "Gunakan: /undangan_cabut &lt;ID undangan&gt;"
INVITE_REVOKED: Final = "Undangan <code>{invite_id}</code> dicabut."
INVITE_NOT_REVOKED: Final = {
    "not_found": "Undangan tidak ditemukan.",
    "used": "Undangan sudah dipakai dan tidak dapat dicabut.",
    "already_revoked": "Undangan sudah dicabut sebelumnya.",
    "expired": "Undangan sudah kedaluwarsa.",
}
