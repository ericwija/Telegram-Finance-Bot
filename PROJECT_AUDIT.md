# Telegram-Finance-Bot — Project Audit

| | |
|---|---|
| **Audit date** | 2026-10-23 |
| **Branch / commit** | `develop` @ `df41430` |
| **Scope** | `README.md`, `main.py` (3,337 lines), `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `.gitignore` |
| **Method** | Read-only review of every file in scope and the git history. The local parser functions were copied into a scratch directory and run against sample inputs. Nothing was installed, built, deployed or committed, and no application file was modified. |

### How to read this document

- **[V] Verified**: seen directly in the code, config or git history, with a `file:line` reference, or reproduced by running the copied local parser.
- **[E] External / needs confirmation**: based on knowledge of third-party libraries or services that this audit could not check offline (for example deprecation dates or default behavior in newer library versions). Confirm before acting on these.
- **[R] Recommendation**: a proposed change. Recommendations are kept in their own subsections or sections (§7–§9) so they are not confused with findings.

No secret values appear in this document. The git history was scanned for private keys, Google API keys and Telegram bot tokens, and **none were found** [V]. All 7 commits only ever contained the 8 files listed in the scope.

---

## 1. Executive Summary

Telegram-Finance-Bot is a single-file Python bot (python-telegram-bot v20+) for personal finance logging in Indonesian. It records transactions from free text and from receipt photos using Google Gemini, stores them in one Google Sheet, and produces reports on demand. It is containerized and runs in long-polling mode.

**Overall assessment:** the bot works as a personal or small-group tool, and its user experience is well thought out (confirmation steps, multi-line input, three ways to record a receipt, a local fallback parser). It is **not yet production-grade** for multi-user or sensitive financial use. The main problems:

| # | Area | Severity | Summary |
|---|---|---|---|
| 1 | Dependencies | **High** | Every dependency is unpinned (`>=`). `oauth2client` is deprecated, `google-generativeai` and `gemini-2.0-flash-lite` are probably deprecated or retired [E], and a fresh `docker build` may produce a bot that does not start or cannot reach Gemini. |
| 2 | Data integrity | **High** | Rows are found by a timestamp with one-second resolution. Batch inserts share timestamps, so delete operations can remove the wrong row, including another user's row (§6.2). |
| 3 | Privacy | **High** | All users share one sheet, and `/sheet` gives every user the link to the whole spreadsheet. Raw financial text and other users' amounts are written to logs. |
| 4 | Correctness | **Medium** | The local fallback parser picks up item quantities as the amount ("beli 2 kopi 30000" → Rp 2.000) [V, reproduced]. Times are in UTC inside the container, so "today" and "kemarin" are wrong for WIB users between 00:00 and 07:00. |
| 5 | Architecture | **Medium** | One 3,300-line module with import-time network side effects, synchronous Gemini and Sheets calls that block the asyncio loop, no tests, and a lot of duplicated code. |
| 6 | Authorization | **Low–Medium** | Several handlers skip `is_authorized()`. Impact is limited today, but it is a defense-in-depth gap. |

**Recommended direction:** pin dependencies and migrate the SDKs first, then split the code into layers (handlers → services → repository). Introduce PostgreSQL as the system of record, give rows real IDs, and keep Google Sheets as an optional export or sync target.

---

## 2. Existing Architecture

### 2.1 Shape [V]

```
                ┌──────────────────────── main.py (single module) ─────────────────────────┐
Telegram  ◄────►│ PTB Application (long polling)                                           │
(Bot API)       │   CommandHandlers / CallbackQueryHandlers / MessageHandlers              │
                │        │                                                                 │
                │        ├── parse_financial_data() ──► Gemini (text)  ─┐ fallback         │
                │        │                                              └► parse_transaction_locally()
                │        ├── analyze_receipt_image() ─► Gemini (vision)                    │
                │        └── sheet.append_row / get_all_records / delete_rows ──► Google Sheets (sheet1)
                │                                                                          │
                │ PicklePersistence ──► /app/data/bot_data.pickle (conversation state)     │
                └──────────────────────────────────────────────────────────────────────────┘
```

- **Single module.** Configuration, credential loading, AI prompts, parsing, persistence, formatting and handlers all live in `main.py`.
- **Import-time side effects.** Importing the module loads the env file, prints a startup banner, configures Gemini and **connects to Google Sheets** (`main.py:78-535`). It cannot be imported for tests without network access and credentials.
- **Global mutable state.** `sheet`, `USE_GOOGLE_SHEETS`, `SPREADSHEET_ID` and `model` are module globals.
- **State machine via `context.user_data` keys.** The flow state lives in ad-hoc keys: `conversation_state`, `delete_state`, `pending_transaction`, `pending_multiple_transactions`, `pending_receipt`, `recent_transactions`, `records_to_delete`, and others. The bot does not use PTB's `ConversationHandler`.
- **Persistence.** `PicklePersistence` at the hard-coded path `/app/data/bot_data.pickle` (`main.py:3230`) stores conversation state only. Transactions go to Google Sheets.
- **Transport.** `run_polling(drop_pending_updates=True)` (`main.py:3327`). The bot needs no inbound port, and only one instance can run at a time (a second one gets a 409 Conflict, which is handled at `main.py:3286`).

### 2.2 Data model (Google Sheet `sheet1`) [V]

The column order is defined only implicitly, by the `row_data` lists that are appended (for example `main.py:900-907`), and the header names only by the `record.get(...)` lookups:

| Col | Header (expected) | Content |
|---|---|---|
| A | `Date` | `YYYY-MM-DD` transaction date (from Gemini, the receipt or today) |
| B | `Amount` | signed float: income > 0, expense < 0 |
| C | `Category` | free text (from Gemini or chosen by the user) |
| D | `Description` | free text |
| E | `User ID` | Telegram user ID |
| F | `Timestamp` | `YYYY-MM-DD HH:MM:SS` insert time; also used as the row's identity |

The header row is **never created or validated** by the code. An operator has to create it by hand with these exact names, and the README does not document this.

### 2.3 Handler registration (`main.py:3236-3278`) [V]

| Type | Trigger | Handler | Auth check |
|---|---|---|---|
| Command | `/start` | `start` | ✅ |
| Command | `/catat` | `record_command` | ❌ (only prints instructions) |
| Command | `/laporan` | `report` | ✅ |
| Command | `/help` | `help_command` | ❌ (static text) |
| Command | `/menu` | `menu_command` | ✅ |
| Command | `/sheet` | `sheet_link` | ✅ |
| Command | `/hapus` | `delete_data` | ❌ (shows the delete menu; the next step checks auth) |
| Command | `/hapuspesan` | `toggle_delete_messages` | ✅ |
| Command | `/me` | `cmd_me` (nested in `main()`) | ❌ (by design: shows the caller's ID) |
| Callback | `^confirm_all_` | `multiple_transactions_callback` | ✅ |
| Callback | `^delete_` | `delete_callback` | ✅ |
| Callback | `^del_specific_` | `delete_specific_callback` | ❌ |
| Callback | `^confirm_delete_` | `confirm_delete_callback` | ❌ |
| Callback | `^receipt_` | `receipt_callback` | ✅ |
| Callback | `^(confirm_\|type_)` | `button_callback` | ✅ |
| Callback | `^cat_` | `category_callback` | ❌ |
| Message | photo, private chat | `photo_handler` | ✅ |
| Message | keyboard-button text, private chat | `keyboard_handler` | ✅ |
| Message | text, private chat | `message_handler` | ✅ |

---

## 3. Current Features

### 3.1 Verified in code [V]

| Feature | Where | Notes |
|---|---|---|
| Free-text transaction capture (ID/EN) | `parse_financial_data` `main.py:1446` | Gemini extracts the amount, category, type and date. A long prompt handles relative dates ("kemarin", "Senin lalu", "awal bulan"). |
| Indonesian amount formats | `parse_indonesian_amount` `main.py:257` | `50rb`, `70k`, `1jt`, `1.5jt`, `1.000.000`. |
| Local fallback parser | `parse_transaction_locally` `main.py:300` | Keyword rules for type and category. Used when Gemini fails. |
| Multi-line batch input | `parse_multiple_transactions` `main.py:1688` | One Gemini call per line, run sequentially. |
| Confirmation before save | `process_financial_message` `main.py:2635` | Buttons: confirm, re-enter, cancel. |
| Ask for the amount when it is missing | `main.py:2652-2721`, `2754-2770` | Asks for the type, then waits for the amount. |
| Receipt OCR (Gemini Vision) | `photo_handler` `main.py:1811`, `analyze_receipt_image` `main.py:1344` | Extracts store, date, items, tax and discount. |
| Three receipt recording modes | `receipt_callback` `main.py:1979` | Total only, per item, or per category. |
| Category summary with emoji after saving | `generate_category_summary` `main.py:1262` | |
| Full report | `report` `main.py:2343` | All-time, this month, last 7 days and today; counts, averages and maxima; category breakdown; savings-rate "health" indicator; last 5 transactions; one tip. |
| Delete flows | `delete_callback` etc. `main.py:657-1182` | Last transaction, one of the last 5, a date range, or all of the user's rows. Each has a confirmation step. |
| Sheet link | `sheet_link` `main.py:555` | |
| Persistent reply keyboard | `get_main_keyboard` `main.py:1751` | |
| Bot command menu registration | `post_init` `main.py:3306` | |
| Gemini retry with exponential backoff and jitter | `call_gemini_with_retry` `main.py:206` | |
| Allow-list authorization | `is_authorized` `main.py:541` | Comma-separated `AUTHORIZED_USER_ID`. |
| Three ways to load credentials | `setup_google_sheets_credentials` `main.py:400` | JSON or Base64 in an env var, a file-path env var, or a list of common file paths. |
| "Limited mode" when Sheets is unavailable | many handlers | The bot starts, but **nothing can be saved, reported or deleted**. |

### 3.2 Features that are advertised but missing or incomplete [V]

| Claim | Reality |
|---|---|
| `/hapuspesan` toggles auto-deletion of messages (`main.py:635`, help text) | `delete_transaction_messages` (`main.py:606`) is **never scheduled**, because the job queue is never used. The toggle has no effect, and `messages_to_delete` grows without limit in the pickle file. |
| README: `/hapus` = "Hapus transaksi terakhir" | It actually opens a menu with four delete options. |
| README: Google Sheets is "Optional" | Without Sheets the bot cannot store anything. In practice it is required. |
| Startup log: "using JSON storage only" (`main.py:185`) | There is no JSON storage. |
| `report()` limited-mode text: "Bot masih dapat menjawab pertanyaan keuangan umum dengan Gemini AI" (`main.py:2360`) | The bot has no Q&A feature. |
| README "Laporan Otomatis … harian, mingguan, bulanan" | The report runs only on demand. There are no scheduled reports. |
| README command table | Leaves out `/menu`, `/hapuspesan` and `/me`. |
| `.env.example` "Method 3: `GOOGLE_SHEETS_CREDENTIALS_FILE`" | The code reads `GOOGLE_SHEETS_CREDENTIALS` (`main.py:81`, `432`). The documented variable name is ignored. |

### 3.3 Dead code [V]

- `handle_amount_input` (`main.py:3045`) is never registered. As a result `category_callback` (`main.py:3082`) can only be reached through crafted callback data.
- `delete_transaction_messages` (`main.py:606`) is never scheduled.
- `STATE_WAITING_CONFIRMATION` (`main.py:394`) is unused.
- The multi-line branch inside `process_financial_message` (`main.py:2728-2744`) cannot run, because `message_handler` already sends multi-line input elsewhere.
- In `message_handler` (`main.py:2255-2261`) the "starts with `/`" branch cannot run, because the handler's filter excludes commands (`~filters.COMMAND`, `main.py:3268`). This means `delete_state` is **not** cleared when the user switches to another command (see §6.2).
- Unused imports: `time` (`main.py:32`) and `google_exceptions` (`main.py:40`). `aiohttp` in `requirements.txt` is never imported.

---

## 4. Dependencies and Integrations

### 4.1 `requirements.txt` [V]

| Package | Spec | Used? | Observation |
|---|---|---|---|
| `python-telegram-bot[job-queue]` | `>=20.0` | Yes | Unpinned. The job-queue extra is installed but never used. |
| `google-generativeai` | `>=0.3.0` | Yes | Unpinned. [E] Google has replaced this SDK with `google-genai`; the old package is deprecated and no longer gets features or fixes. |
| `gspread` | `>=5.0.0` | Yes | Unpinned. [E] gspread 6.x dropped support for `oauth2client` credentials. A fresh build that installs 6.x may fail at `gspread.authorize(creds)` (`main.py:485`). If so, the bot falls back to "limited mode" **silently** and can no longer save anything. **This is the most urgent item to verify** with a build in a test environment. |
| `oauth2client` | `>=4.1.3` | Yes | [E] Deprecated for years. Replace it with `google-auth` (`google.oauth2.service_account.Credentials`). |
| `python-dotenv` | `>=0.19.0` | Yes | |
| `Pillow` | `>=9.0.0` | Yes | Decodes user-uploaded images (`main.py:1834`). Keep it on a patched version. |
| `aiohttp` | `>=3.8.0` | **No** | Remove it. |

There is no lock file and no hash pinning, so every build can resolve to different versions [V].

### 4.2 Telegram [V]

- Uses long polling with `drop_pending_updates=True`, so messages sent while the bot is down are discarded.
- Only private chats are handled (`filters.ChatType.PRIVATE`), which is good.
- Uses legacy `parse_mode='Markdown'` everywhere and interpolates **user- and AI-supplied text** into it (descriptions, store names, categories). A stray `_`, `*` or `[` makes Telegram reject the message with a BadRequest. Only `button_callback` and `category_callback` fall back to plain text. The receipt flows, `process_multiple_transactions`, `report` and `sheet_link` do not, so those messages simply fail.
- Messages are not checked against Telegram's 4,096-character limit. A long report or a large batch can fail to send.
- Callback data is parsed with `split("_")` and positional indexes (`main.py:667`, `964`, `1115`, `3087`). This is brittle; for example a category containing `_` would be truncated.

### 4.3 Gemini [V unless marked]

- The model is hard-coded: `gemini-2.0-flash-lite` (`main.py:195`). [E] This model generation is scheduled for or already past retirement; check Google's current model list. When it is retired, every call fails and the bot silently degrades to the local parser, which has the accuracy bugs described in §6.3.
- **Blocking calls.** `call_gemini_with_retry` calls the synchronous `generate_content` directly inside a coroutine (`main.py:223`). Every Gemini call, including retries of up to about 25 seconds, freezes the event loop **for all users**.
- **Retries are too broad.** Any exception is retried, including an invalid API key or a blocked prompt. Rate-limit detection uses substring matches such as `'rate'` and `'exceeded'` (`main.py:230-236`).
- **No structured output.** JSON is pulled out of Markdown code fences by string splitting (`main.py:1398-1403`, `1539-1544`). The resulting fields are not checked for type: a `null` or string `quantity` raises an error at `item_qty > 1` (`main.py:1895`, `2105`), and a `null` amount raises an error in `float(...)`.
- **Prompt injection.** User text is inserted directly into the prompt (`main.py:1464`), and receipt images can carry instructions too. The impact is limited today, because the user sees and confirms every result, but it matters as soon as the model's output feeds automation.
- **Cost and latency.** A multi-line message makes N sequential Gemini calls. The receipt path makes one call, sends a full-resolution image and does not compress it.
- `GEMINI_API_KEY` is not required at startup. If it is missing, the bot runs with every AI call failing (`main.py:107-112`, `190`).

### 4.4 Google Sheets [V]

- Uses the `sheet1` of one spreadsheet. **All users' data is in the same sheet**, separated only by the `User ID` column.
- Every confirmation, receipt save, report and delete calls `get_all_records()` or `get_all_values()`, which reads **the whole sheet**. The cost grows linearly with history, and [E] the Sheets API read quota (about 60 reads per minute per user) is reached quickly.
- There is a fixed `asyncio.sleep(3)` after each single append (`main.py:2889`, `3118`) and 0.3–0.5 seconds between batch rows. This is a workaround for read-after-write behavior, and all of these calls also block the event loop.
- There are no transactions or locking. Delete operations compute row indexes from a snapshot and then call `delete_rows`. If another user edits the sheet in between, the indexes shift and the wrong rows can be deleted.
- `report()` calls `float(record['Amount'])` with no protection (`main.py:2380` onward). One malformed row, for example a value someone typed by hand, breaks the whole report for every user.
- [E] Since `value_input_option` is not set explicitly, it depends on the library default (normally `RAW`). Set it explicitly to guard against formula injection from descriptions that start with `=`.

---

## 5. Security Findings

Severity reflects the current deployment model: a small allow-list of trusted users on a self-hosted VM.

### 5.1 Verified findings

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| S-1 | **High** | **Over-privileged Google scope.** The `https://www.googleapis.com/auth/drive` scope is requested, which gives access to every Drive file shared with the service account, not just this sheet. | `main.py:397` |
| S-2 | **High** | **No data isolation between users.** Every user's rows are in one sheet, and `/sheet` gives each user the link to the whole spreadsheet. Anyone the sheet is shared with sees everyone's finances. | `main.py:535`, `555-582` |
| S-3 | **Medium** | **Financial data and personal data in logs.** The bot logs the full raw text of messages (`main.py:1634`, `2266`) and the parsed transactions (`1703`). It logs the amount and category of **other users'** rows for today on every save (`2924`, `2938`, `3151`, `3165`), and the full allow-list of user IDs (`139`). The Docker json-file logs keep this on disk. | as cited |
| S-4 | **Medium** | **Missing authorization checks** in `delete_data`, `delete_specific_callback`, `confirm_delete_callback` and `category_callback`. Current impact is limited, because queries are filtered by the caller's own `user_id`. However, `category_callback` **writes a row** for whoever triggers it (`main.py:3105-3115`). Every entry point should check authorization. | `main.py:584`, `946`, `1110`, `3082` |
| S-5 | **Medium** | **Internal error details shown to users.** Raw exception strings are sent back in messages. | `main.py:1442`, `2629-2630` |
| S-6 | **Low** | **Spreadsheet ID printed unmasked.** Masking is used elsewhere, but `validate_spreadsheet_id` prints the full ID. | `main.py:155`, `165`, `171` |
| S-7 | **Low** | **Part of the credential JSON is printed**, because `_mask(GOOGLE_SHEETS_CREDENTIALS_JSON[:50])` shows a few characters from inside the credential blob. A presence/length indicator would be enough. | `main.py:118` |
| S-8 | **Low** | **Unencrypted pickle state.** `bot_data.pickle` holds pending transactions and descriptions in plaintext. Python's pickle format also runs code when loaded, so anyone who can write to the volume can run code in the bot. | `main.py:3230` |
| S-9 | **Low** | **The credential search walks many file paths**, including `/root/` and `../`, and logs file sizes. This widens where secrets might be picked up from. | `main.py:446-471` |
| S-10 | **Low** | **Allow-list parsing.** `AUTHORIZED_USER_ID` is split without stripping whitespace, so `123, 456` silently denies user `456`. This fails closed (access is denied, not granted), but it is confusing. | `main.py:84` |
| S-11 | **Info** | Unauthorized users get a reply, which confirms the bot exists and is active. Photos from unauthorized users are not downloaded, because the auth check runs first (`main.py:1816`), which is good. | — |
| S-12 | **Info** | Some things are already done right: `.env`, `*.key`, `*.pem`, `credentials.json`, `service-account-key.json` and `*.pickle` are git-ignored. The container runs as a non-root user (`Dockerfile:23-26`). The git history contains no secrets. | `.gitignore`, `Dockerfile` |

### 5.2 Data-privacy considerations [E]

- Receipt photos and transaction text are sent to Google's Gemini API. On Google's **unpaid** tier, submitted content may be used to improve Google's products. Check the current Gemini API terms, and consider the paid tier for financial data. Tell users about this.
- Indonesia's personal data protection law (UU PDP, Law 27/2022) is likely to apply to multi-user deployments: expect obligations around consent, purpose limitation, retention and deletion. Get legal confirmation for any use beyond personal use.

### 5.3 Security recommendations [R]

1. Narrow the Google scope to `https://www.googleapis.com/auth/spreadsheets`. Better still, use one spreadsheet or one worksheet per user, or move to PostgreSQL (§7).
2. Use a single authorization guard (decorator or middleware) and apply it to **every** handler, including callbacks.
3. Never log message text, amounts or other users' rows. Log event IDs and outcomes only. Mask user IDs in logs.
4. Send users a generic error message plus a correlation ID, and keep the details in the server-side log.
5. Replace `PicklePersistence` with database-backed persistence, or at least make the volume read/write for the bot user only.
6. Strip and validate `AUTHORIZED_USER_ID` entries as integers, and fail fast at startup on malformed values.
7. Set `value_input_option='RAW'` explicitly on every Sheets write.
8. Load secrets from Docker secrets, or from a file mounted read-only with mode 0400, instead of a multi-kilobyte JSON env var. Rotate the service-account key regularly.

---

## 6. Technical Debt

### 6.1 Structural [V]

- **God module.** One 3,337-line file mixes configuration, I/O and presentation.
- **Duplication.** The "save → wait 3 s → read the whole sheet → today's summary → three-level Markdown fallback" sequence is copied almost line for line in `button_callback` (`main.py:2862-2991`) and `category_callback` (`main.py:3099-3219`), and in a shorter form in `receipt_callback` (`main.py:2043-2071`). The "Sheets not available" message is copied about 10 times. Row-finding code is copied 4 times (`main.py:700-706`, `978-984`, `1135-1138`, `1161-1168`). The confirmation keyboard is built 3 times. The date-display `try/except` appears 6 or more times.
- **Bare `except:`** blocks swallow errors (`main.py:827`, `1563`, `1882`, `1941`, `2595`, `2675`, `2783`).
- **Local re-import** of `datetime` inside a function (`main.py:2781`).
- **Mixed `print` and `logger` output**, plus a decorative startup banner. Some messages are printed and never logged, so they never reach a log handler.
- **Inconsistent version labels.** `BOT_VERSION = "v2025.12.09-rate-limit-fix"`, but the banner says "v3.0" (`main.py:59-66`).
- **No tests, linting, type hints, formatter or CI.**
- **Unused code**, listed in §3.3.

### 6.2 Correctness bugs [V]

| ID | Bug | Evidence / scenario |
|---|---|---|
| B-1 | **Rows are identified by a timestamp with one-second resolution.** Batch saves (receipt items 0.3 s apart, multi-line input 0.5 s apart) produce several rows with the same `Timestamp`. "Delete last" matches the **first** row with that timestamp, not the last (`main.py:701-706`). The date-range delete adds the *same* row index once for each duplicate (`main.py:1162-1168`) and then deletes that index repeatedly. Each repeat removes whichever row has moved into that position. If another user's row is next in the sheet, **that user's data is deleted**. | as cited |
| B-2 | **The local parser treats a quantity as the amount.** The `k` suffix matches the first letter of the next word. | Reproduced: `"beli 2 kopi 30000"` → **−2,000** (expected −30,000). `"Bayar 3 kg beras 45000"` → **−3,000**. `"bayar 10 kotak 20000"` → **−10,000**. |
| B-3 | **Keyword categorization uses substring matching.** | Reproduced: `"Beli busana 200k"` → category **Transportasi** (because "bus" is in "busana"). |
| B-4 | **Timezone.** `datetime.now()` uses the container's timezone, which is UTC because `TZ` is not set in the `Dockerfile` or compose file. Between 00:00 and 07:00 WIB, "hari ini", "kemarin", the month boundaries and "today's summary" are all off by one day. | `Dockerfile`, `docker-compose.yml`, e.g. `main.py:309`, `1455` |
| B-5 | **The delete-by-date state is never cleared** when the user runs another command, because of the dead branch described in §3.3. The next normal message is treated as a date. | `main.py:2252-2263`, `3268` |
| B-6 | **Money is stored as `float`.** Use integer rupiah or `Decimal`. | throughout |
| B-7 | **Gemini output is not validated.** A `null` or string `quantity` or `amount` raises an error in the receipt flow. A `null` `store_name` is shown as "None". | `main.py:1875`, `1895`, `2100-2105` |
| B-8 | **`category_callback` always saves today's date** and ignores any date that was detected. | `main.py:3104` |
| B-9 | **`category_callback` calls `context.user_data.clear()`**, which also wipes the user's settings, such as `delete_messages`. | `main.py:3219` |
| B-10 | **The persistence path is hard-coded to `/app/data`**, so the README's "Option B: Local Python" breaks unless `/app/data` exists on the host. | `main.py:3230` |
| B-11 | **The local parser's description cleanup** removes every number from the text, including numbers that belong to the description (for example "Indomaret 2"). | `main.py:370` |

### 6.3 Performance and scalability [V]

- The event loop is blocked by synchronous Gemini and gspread calls and the fixed 3-second sleeps. One slow receipt delays every other user.
- Each save and each report reads the whole sheet, which is O(n) in the size of the history.
- Multi-line input makes N sequential AI calls.
- Only one instance can run at a time (polling). This is acceptable at the current scale.

---

## 7. Recommended Architecture [R]

### 7.1 Target module layout

```
finance_bot/
├── __main__.py              # entrypoint: build app, wire dependencies, run
├── config.py                # pydantic-settings: typed, validated env (fail fast)
├── logging.py               # structured JSON logging, PII redaction filter
├── domain/
│   ├── models.py            # Transaction, Receipt, Category (Decimal/int amounts)
│   └── categories.py        # canonical category list + emoji map
├── parsing/
│   ├── amount.py            # Indonesian amount parser (fixed, unit-tested)
│   ├── dates.py             # relative-date resolution, Asia/Jakarta tz
│   └── local_parser.py      # keyword fallback (word-boundary matching)
├── ai/
│   ├── client.py            # google-genai async client, retry on 429/5xx only
│   ├── schemas.py           # response JSON schemas (structured output)
│   └── prompts.py           # versioned prompt templates
├── storage/
│   ├── repository.py        # TransactionRepository protocol
│   ├── postgres.py          # SQLAlchemy 2.x async / asyncpg implementation
│   └── sheets_export.py     # optional async export/sync to Google Sheets
├── services/
│   ├── transactions.py      # record / delete / list use-cases
│   └── reports.py           # aggregates computed in SQL
├── bot/
│   ├── auth.py              # @require_authorized decorator applied everywhere
│   ├── keyboards.py
│   ├── formatting.py        # HTML parse_mode + escaping, 4096-char chunking
│   └── handlers/            # start, record, receipt, report, delete, settings
└── tests/                   # pytest: parsers, services (repo fakes), handlers
```

Principles:

- **Dependency direction:** handlers → services → repository/AI interfaces. No Telegram types below the `bot/` layer.
- **No import-time I/O.** Wire everything in `__main__.py`, so every module can be imported in tests.
- **Async all the way down:** an async Gemini client, an async database driver, and Sheets calls run via `asyncio.to_thread` or on a background queue.
- Use PTB's `ConversationHandler` for the record, receipt and delete flows instead of ad-hoc `user_data` keys.
- Use `parse_mode=HTML` with `html.escape()` on all user and AI text.
- Use structured AI output (a JSON response schema), validated with Pydantic.

### 7.2 PostgreSQL migration opportunity

**Why:** stable row IDs (fixes B-1), per-user isolation (fixes S-2), atomic deletes, reports computed in SQL instead of by reading the whole sheet, an audit trail, and data that can be backed up.

Proposed schema (first version):

```sql
CREATE TABLE users (
  id            BIGSERIAL PRIMARY KEY,
  telegram_id   BIGINT UNIQUE NOT NULL,
  display_name  TEXT,
  timezone      TEXT NOT NULL DEFAULT 'Asia/Jakarta',
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  settings      JSONB NOT NULL DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE categories (
  id     SERIAL PRIMARY KEY,
  name   TEXT UNIQUE NOT NULL,
  kind   TEXT NOT NULL CHECK (kind IN ('income','expense','both')),
  emoji  TEXT
);

CREATE TABLE receipts (
  id            BIGSERIAL PRIMARY KEY,
  user_id       BIGINT NOT NULL REFERENCES users(id),
  store_name    TEXT,
  receipt_date  DATE,
  total_amount  BIGINT,              -- whole rupiah
  raw_ai_json   JSONB,               -- for audit/debug; consider retention limit
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE transactions (
  id            BIGSERIAL PRIMARY KEY,
  user_id       BIGINT NOT NULL REFERENCES users(id),
  txn_date      DATE NOT NULL,
  amount        BIGINT NOT NULL,     -- signed whole rupiah (or NUMERIC(14,2))
  category_id   INT REFERENCES categories(id),
  description   TEXT,
  source        TEXT NOT NULL CHECK (source IN ('text','receipt_total','receipt_item','receipt_category','manual')),
  receipt_id    BIGINT REFERENCES receipts(id),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at    TIMESTAMPTZ          -- soft delete enables "undo"
);
CREATE INDEX ON transactions (user_id, txn_date) WHERE deleted_at IS NULL;
```

Migration path:

1. Introduce the `TransactionRepository` interface with a Sheets implementation, which keeps today's behavior.
2. Add a Postgres implementation, Alembic migrations and a `postgres` service in docker-compose.
3. Write a one-off import script: Sheets → Postgres, mapping `User ID` → `users` and treating each row's `Timestamp` as `created_at`.
4. Run both for a while: write to Postgres and to Sheets (as an export).
5. Make Postgres the system of record. Sheets becomes an optional **per-user** export (`/export` → CSV/XLSX, or a per-user worksheet).
6. Replace `PicklePersistence` with a database-backed persistence class, or keep conversation state in a `bot_state` table.

### 7.3 Future feature modules (enabled by the above)

- Budgets per category with alerts (job queue).
- Scheduled daily, weekly and monthly summaries (the README already promises "Laporan Otomatis").
- Edit and undo for transactions (soft delete).
- `/export` to CSV or XLSX.
- Multiple wallets or accounts, and recurring transactions.
- Admin commands to manage the allow-list in the database instead of an env var.
- A Gemini Q&A mode over the user's own aggregated data, which would make the existing help-text claim true.

---

## 8. Development Roadmap [R]

Effort estimates are rough, for one developer.

### Phase 0: Stabilize (about 1–2 days, do first)
- [ ] Do a test build in a sandbox to confirm or rule out the gspread 6 / `oauth2client` break and the status of the Gemini model (§4.1, §4.3).
- [ ] Pin every dependency (`pip-compile` → `requirements.lock`). Remove `aiohttp`.
- [ ] Set `TZ=Asia/Jakarta` and use timezone-aware dates (B-4).
- [ ] Add the authorization guard to every handler (S-4).
- [ ] Remove PII and financial data from logs. Stop printing the spreadsheet ID and the credential prefix (S-3, S-6, S-7).
- [ ] Narrow the Google scope to `spreadsheets` (S-1).
- [ ] Fix B-1 for now: give each row a unique ID (for example a UUID in a new column) and delete by that ID.
- [ ] Fix the local amount parser (B-2) and categorize with word boundaries (B-3).
- [ ] Fix the `.env.example` variable name (`GOOGLE_SHEETS_CREDENTIALS`), the README command table and the "Optional" claim, and document the required sheet header row.

### Phase 1: Modernize integrations (about 3–5 days)
- [ ] Migrate `google-generativeai` → `google-genai` with an async client and a supported model set by a `GEMINI_MODEL` env var.
- [ ] Use structured JSON output and validate it with Pydantic (B-7).
- [ ] Retry only on 429 and 5xx errors.
- [ ] Migrate `oauth2client` → `google-auth`, and move Sheets calls off the event loop.
- [ ] Switch to HTML parse mode with escaping, and split long messages into chunks.
- [ ] Downscale and compress receipt images before upload.

### Phase 2: Refactor and test (about 1–2 weeks)
- [ ] Split the code into the modules in §7.1, with typed config and no import-time I/O.
- [ ] Convert the flows to `ConversationHandler`. Remove dead code (§3.3).
- [ ] pytest suite: amount and date parsing (table-driven, including the reproduced failures), services with fake repositories, and handler smoke tests.
- [ ] Add ruff, mypy and pre-commit, plus GitHub Actions CI (lint, test, build image).

### Phase 3: PostgreSQL (about 1–2 weeks)
- [ ] Repository abstraction, Postgres implementation, Alembic.
- [ ] Sheets → Postgres import script, a dual-write period, then cut over.
- [ ] Reports in SQL. Per-user `/export`. Database-backed bot persistence.

### Phase 4: Features (ongoing)
- [ ] Budgets and alerts, scheduled reports, edit/undo, recurring transactions, multiple wallets, admin allow-list management.

---

## 9. Deployment Recommendations

### 9.1 Current state [V]

| Item | Observation |
|---|---|
| Base image | `python:3.10-slim`. [E] Python 3.10 reaches end of life in October 2026, so it is at or past end of life now. |
| Build | Installs `gcc`, which is not needed because every dependency ships wheels. This makes the image bigger. It is a single-stage build. |
| Reproducibility | Unpinned requirements, and the base tag is not pinned by digest. |
| User | Non-root `botuser` (uid 1000). Good. |
| Copy scope | Only `requirements.txt` and `main.py` are copied, which limits what gets into the image even though there is no `.dockerignore`. Good. |
| Health | No `HEALTHCHECK`. A bot that has crashed or is stuck in a restart loop is only noticed by watching the logs. |
| Compose | The `version: '3.8'` key is obsolete. The `./logs:/app/logs` bind mount is never written by the app, and Docker creates it as root-owned on the host. There are no resource limits. Secrets are passed as env vars (visible in `docker inspect`). |
| Env handling | `GOOGLE_SHEETS_CREDENTIALS` (file-path method) is not passed into the container, so only the JSON-in-env method works under compose. |
| Logging | json-file with rotation (10 MB × 3). Good, but see S-3 about what gets logged. |
| Network | Polling only, so no ports need to be exposed. Good. |
| State | Named volume `bot-data` for the pickle file. There is no backup strategy. |

### 9.2 Recommended Dockerfile changes [R]

- Use `python:3.12-slim` (or 3.13), pinned by digest. Drop `gcc`. Use a multi-stage build that installs from a hashed lock file (`pip install --require-hashes`).
- Set `ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Jakarta` and install `tzdata`.
- Add a `HEALTHCHECK` that checks a heartbeat file the bot updates from a job-queue tick, or a small `/healthz` endpoint.
- Add a `.dockerignore` (`.git`, `.env*`, `*.json` keys, `venv/`, `__pycache__/`) so a future `COPY . .` cannot leak secrets into the image.

### 9.3 Recommended compose changes [R]

- Remove `version:` and the unused `./logs` mount.
- Pass credentials through Compose **secrets**, mounted as read-only files, and have the app read them from `/run/secrets/...`. Keep non-secret config in `env_file: .env`.
- Add `deploy.resources.limits` (for example 256 MB of memory for an e2-micro). Set `read_only: true` with a `tmpfs` for `/tmp`, plus `security_opt: [no-new-privileges:true]` and `cap_drop: [ALL]`.
- When Phase 3 lands, add a `postgres:16` service with a named volume, a healthcheck, `depends_on: condition: service_healthy`, and a nightly `pg_dump` backup job sent to off-host storage.

### 9.4 Operations [R]

- **CI/CD:** GitHub Actions to lint, run tests, build, scan (Trivy or pip-audit) and push the image to GHCR tagged by git SHA. Deploy with `docker compose pull && up -d`.
- **Single instance:** document that only one polling instance may run per token. When deploying to Railway and a VM, make sure they do not both run (the code already logs the 409 conflict).
- **Monitoring:** alert on repeated Gemini or Sheets failures and on "LIMITED MODE" at startup, which today is only a log line. Consider sending an admin a Telegram DM when the bot starts in a degraded state.
- **Secrets hygiene:** rotate the Telegram token, Gemini key and service-account key on a schedule and whenever anyone with access leaves. Restrict the Gemini key to the Generative Language API, and set Google Cloud budget alerts.
- **Webhooks (optional, later):** if the bot runs behind HTTPS, webhooks remove polling latency and make horizontal scaling possible, but they need a database for shared state first (Phase 3).

---

*End of audit. Only `PROJECT_AUDIT.md` was created. No application files, dependencies, builds, deployments or git state were changed.*
