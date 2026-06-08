# GrantBek — EduGrants Telegram Bot 🎓

A bilingual (English / Uzbek) Telegram bot that helps students discover
educational grants and scholarships, answers FAQs about applications, and
replies to comments in a channel's linked discussion group.

- **Retrieval:** SQLite **FTS5** full-text search over a curated grant database — fast, deterministic, **zero token cost**.
- **Answers:** optional **Claude (Haiku)** layer phrases natural bilingual replies, *strictly grounded* in your DB rows. If no API key is set, it falls back to free keyword-based FAQ matching.
- **Backend:** FastAPI webhook (production) with a `--polling` fallback for local dev.

---

## 1. Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit .env and set BOT_TOKEN
python scripts/seed_db.py     # populate the database
python main.py --polling      # run locally, no HTTPS needed
```

Talk to your bot in Telegram: `/start`, `/search engineering`, `/find`, `/faq`.

---

## 2. Environment variables

| Variable | Required | Notes |
|----------|----------|-------|
| `BOT_TOKEN` | ✅ | From [@BotFather](https://t.me/BotFather) |
| `BOT_USERNAME` | ✅ | Bot username without `@` (e.g. `GrantBekBot`) — used to detect mentions |
| `WEBHOOK_URL` | webhook mode | Public HTTPS base URL. On Railway this is auto-derived from `RAILWAY_PUBLIC_DOMAIN` |
| `WEBHOOK_SECRET` | recommended | Random string; Telegram echoes it back for verification |
| `ANTHROPIC_API_KEY` | optional | Enables the Claude answer layer. Omit to stay 100% free |
| `LLM_MODEL` | optional | Defaults to `claude-haiku-4-5-20251001` (cheapest tier) |
| `ALLOWED_GROUP_IDS` | optional | Comma-separated chat IDs the bot may reply in. Blank = all |
| `DATABASE_PATH` | optional | Defaults to `grantbek.db` |
| `DEFAULT_LANGUAGE` | optional | `en` or `uz` |

---

## 3. BotFather setup (required for group comments)

For GrantBek to reply under channel posts:

1. Add the bot as **admin** to your **channel** *and* its **linked discussion group**.
2. In @BotFather: `/setprivacy` → select your bot → **Disable**.
   (Otherwise the bot only sees messages that @mention it or reply to it.)
3. Optional — set the command menu in @BotFather via `/setcommands`:
   ```
   start - Welcome message
   search - Find grants by keyword
   categories - Browse grants by field
   deadlines - Grants closing soon
   find - Guided grant finder
   faq - Frequently asked questions
   lang - Change language
   about - About GrantBek
   ```

> The bot only auto-replies in groups when **@mentioned** or **replied to** — never to every keyword. This is deliberate: blanket keyword replies get bots muted or reported as spam.

---

## 4. Deploy to Railway

1. Push this folder to a GitHub repo and create a Railway project from it
   (or `railway up` with the CLI). Nixpacks auto-detects Python.
2. In **Variables**, set `BOT_TOKEN`, `BOT_USERNAME`, `WEBHOOK_SECRET`, and
   (optionally) `ANTHROPIC_API_KEY`. You do **not** need to set `WEBHOOK_URL`
   or `PORT` — Railway provides `RAILWAY_PUBLIC_DOMAIN` and `PORT` automatically.
3. Make sure the service has a **public domain** (Settings → Networking →
   Generate Domain). The app registers the Telegram webhook to that domain on startup.
4. Seed the database once. Either run `python scripts/seed_db.py` as a one-off
   command, or rely on first-run table creation and import your own data later.

Health check: `GET https://<your-domain>/health` → `{"status":"ok"}`.

The start command is `python main.py` (also defined in `Procfile` and `railway.toml`).

---

## 5. Adding real grant data

The bot ships with ~12 sample grants (including three real EduGrands posts as
examples). Each grant renders in the EduGrands channel format — bold title,
`Davlat / Moliyaviy ta'minot / Yosh toifasi` meta block, `➡️Imtiyozlari` bullets,
a `Havola` registration link, an Uzbek-formatted deadline, and the `⚡️@EduGrandsUz`
footer (set via `CHANNEL_HANDLE`). To load your own in bulk:

```bash
# Edit a spreadsheet with the columns in scripts/grants_template.csv, then:
python scripts/import_csv.py my_grants.csv --replace
```

`--replace` wipes existing grants first; omit it to append. The FTS index is
rebuilt automatically after import.

---

## 6. Cost note

The retrieval layer is free. If you enable `ANTHROPIC_API_KEY`, only the
answer-phrasing step spends tokens, on Claude Haiku (~$1 / $5 per million
input/output tokens). For a low-traffic grants bot that's pennies per month;
prompt caching can reduce it further. Pricing changes — check
<https://www.anthropic.com/pricing> for current rates.

---

## Project layout

```
grantbek/
├── main.py              # FastAPI webhook + /health + --polling
├── config.py            # env settings (Railway-aware)
├── bot/
│   ├── app.py           # Application builder + handler registration
│   ├── i18n.py          # EN/UZ strings + per-user language prefs
│   ├── filters.py       # BotMentionedFilter
│   └── handlers/        # commands, conversations, channel, inline
├── db/
│   ├── database.py      # async SQLite
│   ├── models.py        # schema + FTS5 (LIKE fallback)
│   └── seed_data.json   # bilingual grants + FAQs
├── services/
│   ├── grant_search.py  # FTS5 search + card formatting
│   ├── faq.py           # keyword FAQ matching
│   └── llm.py           # optional Claude grounding
└── scripts/
    ├── seed_db.py       # seed from JSON
    └── import_csv.py    # bulk import real grants
```
