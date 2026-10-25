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

# --- finance core (docs/02 §5, §6, §8; Step 6) ----------------------------------------------
HELP: Final = (
    "<b>Perintah</b>\n"
    "/catat (/tambah) — catat transaksi baru\n"
    "/pemasukan [jumlah] [keterangan] — catat pemasukan\n"
    "/pengeluaran [jumlah] [keterangan] — catat pengeluaran\n"
    "/riwayat (/transaksi) [pemasukan|pengeluaran|dihapus] [bulan|tanggal tanggal] — riwayat\n"
    "/saldo — saldo tercatat per akun\n"
    "/laporan (/ringkasan) [bulan] — ringkasan bulanan\n"
    "/kategori [pemasukan|pengeluaran] — kelola kategori\n"
    "/batal — batalkan percakapan yang sedang berjalan\n"
    "/bantuan — tampilkan bantuan ini\n\n"
    "Contoh: <code>/pengeluaran 25rb makan siang</code>, <code>/laporan bulan lalu</code>, "
    "<code>/riwayat 2026-10</code>.\n"
    "Jumlah dalam rupiah utuh: 25000, 25.000, 25rb, 1,5jt."
)
START_MENU_HINT: Final = "\n\nKetik /bantuan untuk melihat perintah."
WORKSPACE_LINE: Final = "{icon} <b>{name}</b>"
WORKSPACE_ICON: Final = {"personal": "🏠", "business": "🏢"}
TYPE_LABEL: Final = {"income": "Pemasukan", "expense": "Pengeluaran", "transfer": "Transfer"}
TYPE_ICON: Final = {"income": "⬆️", "expense": "⬇️", "transfer": "🔁"}

DRAFT_TITLE_NEW: Final = "<b>Transaksi baru</b>"
DRAFT_TITLE_EDIT: Final = "<b>Ubah transaksi</b>"
ASK_TYPE: Final = "Pilih jenis transaksi:"
ASK_AMOUNT: Final = (
    "Kirim jumlahnya, misalnya <code>25000</code>, <code>25rb</code> atau <code>1,5jt</code>."
)
ASK_CATEGORY: Final = "Pilih kategori:"
ASK_CATEGORY_EMPTY: Final = "Belum ada kategori aktif untuk jenis ini. Tambahkan lewat /kategori."
ASK_DESCRIPTION: Final = "Kirim keterangan (opsional), atau tekan <b>Lewati</b>."
ASK_DESCRIPTION_EDIT: Final = "Kirim keterangan baru, atau tekan <b>Hapus keterangan</b>."
ASK_DATE: Final = (
    "Kirim tanggal: <code>hari ini</code>, <code>kemarin</code> atau <code>25/10/2026</code>."
)
ASK_CONFIRM: Final = "Periksa lalu tekan <b>Simpan</b>. Belum ada yang disimpan."
ROUNDED_NOTE: Final = "Catatan: jumlah dibulatkan ke rupiah terdekat."
FIELD_TYPE: Final = "Jenis"
FIELD_AMOUNT: Final = "Jumlah"
FIELD_CATEGORY: Final = "Kategori"
FIELD_DESCRIPTION: Final = "Keterangan"
FIELD_DATE: Final = "Tanggal"
FIELD_ACCOUNT: Final = "Akun"
FIELD_STATUS: Final = "Status"
NO_DESCRIPTION: Final = "—"
EDIT_FIELD_LABEL: Final = {
    "amount": "Jumlah",
    "category": "Kategori",
    "description": "Keterangan",
    "date": "Tanggal",
}
BUTTON_INCOME: Final = "⬆️ Pemasukan"
BUTTON_EXPENSE: Final = "⬇️ Pengeluaran"
BUTTON_SAVE: Final = "✅ Simpan"
BUTTON_DATE: Final = "📅 Tanggal"
BUTTON_CANCEL: Final = "❌ Batal"
BUTTON_SKIP: Final = "Lewati"
BUTTON_CLEAR_DESCRIPTION: Final = "Hapus keterangan"
BUTTON_EDIT: Final = "✏️ Ubah"
BUTTON_DETAIL: Final = "🔎 Detail"
BUTTON_DELETE: Final = "🗑️ Hapus"
BUTTON_RESTORE: Final = "♻️ Pulihkan"
BUTTON_CONFIRM_DELETE: Final = "Ya, hapus"
BUTTON_BACK: Final = "Kembali"
BUTTON_PREV: Final = "◀️ Sebelumnya"
BUTTON_NEXT: Final = "Berikutnya ▶️"
BUTTON_RENAME: Final = "✏️ Ganti nama"
BUTTON_DEACTIVATE: Final = "🚫 Nonaktifkan"
BUTTON_ACTIVATE: Final = "✅ Aktifkan"
BUTTON_CATEGORY_HISTORY: Final = "📜 Riwayat"
BUTTON_NEW_INCOME_CATEGORY: Final = "+ Kategori pemasukan"
BUTTON_NEW_EXPENSE_CATEGORY: Final = "+ Kategori pengeluaran"

SAVED: Final = "✅ Tersimpan: {type} {amount} — {category}, {date}."
SAVED_AGAIN: Final = "Transaksi ini sudah disimpan sebelumnya: {type} {amount}, {date}."
UPDATED: Final = "✅ Transaksi diperbarui."
CANCELLED: Final = "Dibatalkan. Tidak ada yang disimpan."
NOTHING_TO_CANCEL: Final = "Tidak ada percakapan yang sedang berjalan."
NO_CONVERSATION: Final = (
    "Saya belum memahami pesan bebas. Gunakan /catat, /pemasukan atau /pengeluaran untuk "
    "mencatat, atau /bantuan."
)
USE_BUTTONS: Final = "Gunakan tombol pada pesan sebelumnya, atau /batal untuk membatalkan."
DRAFT_EXPIRED: Final = (
    "Percakapan ini sudah tidak berlaku (kedaluwarsa, dibatalkan atau diganti). "
    "Mulai lagi dengan /catat."
)
STALE_BUTTON: Final = "Tombol ini sudah tidak berlaku."
NOT_FOUND: Final = "Data tidak ditemukan."
CONFLICT: Final = {
    "stale": "Transaksi ini sudah berubah. Buka lagi dari /riwayat untuk melihat versi terbaru.",
    "voided": "Transaksi ini sudah dihapus. Pulihkan dulu sebelum mengubahnya.",
    "already_voided": "Transaksi ini sudah dihapus.",
    "not_voided": "Transaksi ini tidak dalam keadaan terhapus.",
}
DUPLICATE_CATEGORY: Final = "Kategori aktif dengan nama <b>{name}</b> sudah ada untuk jenis ini."
INVALID_INPUT: Final = "Masukan tidak valid."
INVALID: Final = {
    "amount_empty": "Kirim jumlahnya, misalnya 25000.",
    "amount_invalid": "Jumlah tidak dikenali. Contoh: 25000, 25.000, 25rb, 1,5jt.",
    "amount_negative": "Jumlah harus positif. Jenis transaksi menentukan arahnya.",
    "amount_zero": "Jumlah harus lebih dari nol.",
    "amount_too_large": "Jumlah terlalu besar (maksimal Rp 1.000.000.000.000).",
    "category_name_empty": "Nama kategori tidak boleh kosong.",
    "category_name_too_long": "Nama kategori maksimal 40 karakter.",
    "category_archived": "Kategori itu sudah nonaktif. Pilih kategori lain.",
    "category_kind_mismatch": "Kategori itu bukan untuk jenis transaksi ini.",
    "category_default": "Kategori <b>Lainnya</b> adalah cadangan dan tidak dapat dinonaktifkan.",
    "category_kind": "Jenis kategori harus pemasukan atau pengeluaran.",
    "description_too_long": "Keterangan maksimal 500 karakter.",
    "void_reason_too_long": "Alasan maksimal 200 karakter.",
    "date_invalid": "Tanggal tidak dikenali. Contoh: hari ini, kemarin, 25/10/2026.",
    "date_in_future": "Tanggal tidak boleh di masa depan.",
    "date_too_old": "Tanggal paling awal adalah 01/01/2000.",
    "date_range": "Tanggal awal harus sebelum tanggal akhir.",
    "month_invalid": "Bulan tidak dikenali. Contoh: bulan ini, bulan lalu, 2026-10, oktober 2026.",
    "month_in_future": "Bulan itu belum berjalan.",
    "page": "Halaman tidak tersedia. Persempit dengan filter tanggal.",
    "no_changes": "Tidak ada perubahan.",
    "account_unavailable": "Akun bawaan tidak tersedia. Hubungi pemilik ruang kerja.",
    "txn_type": "Jenis transaksi harus pemasukan atau pengeluaran.",
    "filter_type": "Filter jenis harus pemasukan atau pengeluaran.",
}

HISTORY_TITLE: Final = "<b>Riwayat transaksi</b>"
HISTORY_TITLE_VOIDED: Final = "<b>Transaksi dihapus</b>"
HISTORY_FILTER: Final = "Filter: {filters}"
HISTORY_EMPTY: Final = "Belum ada transaksi yang cocok."
HISTORY_ITEM: Final = "{n}. {date} {icon} {amount} — {category}{description}"
HISTORY_PAGE: Final = "Halaman {page}"
HISTORY_HINT: Final = "Tekan nomor untuk melihat detail."

DETAIL_TITLE: Final = "<b>Detail transaksi</b>"
DETAIL_STATUS: Final = {"posted": "tercatat", "voided": "dihapus"}
DETAIL_META: Final = "Versi {version} · dicatat {created}"
CONFIRM_DELETE: Final = (
    "Hapus transaksi ini? Transaksi tidak lagi dihitung dalam saldo dan laporan, "
    "tetapi tetap dapat dipulihkan."
)
DELETED: Final = "🗑️ Transaksi dihapus. Pulihkan lewat /riwayat dihapus."
RESTORED: Final = "♻️ Transaksi dipulihkan."
CHOOSE_EDIT_FIELD: Final = "Apa yang ingin diubah?"

CATEGORIES_TITLE: Final = "<b>Kategori</b>"
CATEGORIES_KIND: Final = "<b>{kind}</b>"
CATEGORY_ITEM: Final = "• {name}{flags}"
CATEGORY_FLAG_DEFAULT: Final = " (cadangan)"
CATEGORY_FLAG_ARCHIVED: Final = " (nonaktif)"
CATEGORY_DETAIL: Final = "<b>{name}</b>\nJenis: {kind}\nStatus: {status}"
CATEGORY_STATUS: Final = {False: "aktif", True: "nonaktif"}
CATEGORY_ASK_NEW: Final = "Kirim nama kategori {kind} baru (maksimal 40 karakter)."
CATEGORY_ASK_RENAME: Final = "Kirim nama baru untuk kategori <b>{name}</b>."
CATEGORY_CREATED: Final = "✅ Kategori <b>{name}</b> ({kind}) ditambahkan."
CATEGORY_RENAMED: Final = "✅ Kategori diganti nama menjadi <b>{name}</b>."
CATEGORY_DEACTIVATED: Final = (
    "Kategori <b>{name}</b> dinonaktifkan. Transaksi lama tetap memakai kategori ini."
)
CATEGORY_ACTIVATED: Final = "Kategori <b>{name}</b> diaktifkan kembali."
CATEGORY_USAGE: Final = (
    "Gunakan: /kategori [pemasukan|pengeluaran], atau "
    "/kategori tambah pemasukan|pengeluaran &lt;nama&gt;"
)

BALANCE_TITLE: Final = "<b>Saldo tercatat</b> per {date}"
BALANCE_ACCOUNT: Final = "• {name}: {amount}{warning}"
BALANCE_NEGATIVE: Final = " ⚠️"
BALANCE_TOTAL: Final = "Total tercatat ({currency}): <b>{amount}</b>"
BALANCE_ALL_TIME: Final = (
    "Sejak awal ({currency}): pemasukan {income}, pengeluaran {expense}, selisih {net}"
)
BALANCE_NOTE: Final = (
    "Catatan: saldo ini dihitung dari transaksi yang Anda catat di bot ini, bukan saldo "
    "rekening bank."
)
SUMMARY_TITLE: Final = "<b>Ringkasan {month}</b>"
SUMMARY_TOTALS: Final = (
    "Pemasukan: <b>{income}</b>\nPengeluaran: <b>{expense}</b>\nSelisih: <b>{net}</b>"
)
SUMMARY_CURRENCY: Final = "<i>{currency}</i>"
SUMMARY_EMPTY: Final = "Belum ada pemasukan atau pengeluaran pada bulan ini."
SUMMARY_EXPENSE_HEADER: Final = "<b>Pengeluaran per kategori</b>"
SUMMARY_INCOME_HEADER: Final = "<b>Pemasukan per kategori</b>"
SUMMARY_CATEGORY: Final = "• {name}: {amount} ({count} transaksi)"
SUMMARY_MORE: Final = "… dan {count} kategori lain"
