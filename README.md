# EduGrants Finder

An agent that finds **fresh opportunities that fit @EduGrandsUz** and sends them to your team in
Telegram, one card each, with the facts checked on the organiser's own website.

## What counts as "ours"

Learned from the channel's own history (1,336 opportunity posts, April 2024 to September 2026)
plus your rules. Every find must be:

- open to applicants from Uzbekistan
- for ages overlapping 12 to 40
- free to apply (no application fee)
- free for the participant: fully funded, **or** a free online programme, **or** a free competition
  entered remotely, **or** a free event inside Uzbekistan
- open now or opening soon, with at least 7 days left

Change these in `.env` (`AGE_MIN`, `AGE_MAX`, `ALLOW_FULL_AID`, `REQUIRE_UZ_YES`, `MIN_DAYS_LEFT`).

## The vibe filter

Rules alone would let through every free essay contest and researcher fellowship on the
internet. The agent also learns *what the channel actually picks* from its history:

- Every past programme gets a score: its reactions divided by that month's median, so a 2024 post
  (8K subscribers) is judged fairly against a 2026 one (31K).
- For each type (essays, fellowships, olympiads, camps, forums...) the profile lists the hits and
  the flops. Example from your data: essays from Columbia, Harvard International Review and
  Cambridge's R.A. Butler Prize are hits; IvyPanda and unknown "essay prizes" flop. 0 of 850
  programmes were for PhD/postdoc.
- Each candidate is scored 1 to 5 against that: is the organiser a name your audience knows, is the
  benefit concrete, is it for pupils or bachelor students? Only 4 and 5 reach your team
  (`MIN_FIT_SCORE`, set 3 to also see borderline ones).
- The team's ✅/❌ choices are added on every run, so it keeps adjusting.

`config/channel_profile.md` is what the agent reads; open it to see exactly what it learned.

## Where it looks

1. **Your own history (the biggest source).** 225 of your 850 programmes come back every year. The
   agent watches each one's official page from 75 days before the date you posted it last year,
   and flags it the day a new round opens. Today that's 234 programmes in season.
2. **Aggregator feeds**, only items from the last 3 days, so everything is fresh.
3. **International Telegram channels** (Russian-language: @grantscholar, @studygrants,
   @check_opportunities, @school_grants, @edu_traveler, @opportunity_ladder, @grantinum),
   checked in the same daily search.
4. **Other Uzbek channels and grantlar.uz** are read too, but not as sources: each card says
   `🥇 O'zbek kanallarida hali yo'q` or `⚠️ @grantgouz 3 soat oldin joylagan`, and cards nobody
   has posted yet come first.
5. **@EduGrandsUz itself**: anything you post, even by hand, is marked as posted automatically.

Telegram needs a spare account: get an API id/hash at my.telegram.org, then run
`python -m edugrants_agent tg-login` once and paste the printed `TG_STRING_SESSION` into `.env`.

It never suggests something you posted in the last 60 days (`RECENT_POST_DAYS`).

## What your team sees

```
🆕 Summer Science Program
🇺🇸 United States · Yozgi maktab · Oflayn · 👤 15-18 yosh
💰 To'liq moliyalashtirilgan · ariza bepul
📅 Muddat: 15-fevral (138 kun qoldi)
⏱ Manbada 40 daqiqa oldin · @grantscholar
🥇 O'zbek kanallarida hali yo'q
🔁 Yangi bosqich: kanalda 4 marta joylangan, oxirgi 2026-01-20, eng ko'p 133 reaksiya
✔️ rasmiy sahifadan tekshirildi
🔗 Rasmiy sahifa · Ariza
[✅ Olamiz] [❌ Kerak emas]
```

**❌** asks for a one-tap reason (pullik, mos emas, allaqachon bor, kech, yosh, ishonchsiz).
The last 20 taken and 20 skipped items are shown to the agent on every run, so it adapts to your
taste. **✅** marks it as posted, so it never comes back; with `WRITE_ON_ACCEPT=true` it also
writes the Uzbek post and platform listing and sends them for one-tap publishing.

Programmes that break a rule for good (tuition, application fee, wrong ages) are remembered and
no longer watched. After changing the rules, run `python -m edugrants_agent reset-exclusions`.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # add ANTHROPIC_API_KEY, BOT_TOKEN, ADMIN_CHAT_ID
# put your channel export at config/messages.html (loaded automatically on first start)
python -m edugrants_agent check  # tests every part for real, then 5 real listings end to end
python -m edugrants_agent run    # one full search, prints the cards, sends nothing
python -m edugrants_agent bot    # the real thing: searches daily at 09:00
```

**Searching whenever you want:** make the bot an admin of your editors' group, then send `/panel`
there once. It pins a message with **🔎 Hozir qidirish** and **📊 Statistika** buttons, and puts the
same two buttons at the bottom of the chat. One tap starts a search (usually 2 to 5 minutes); the
cards arrive in the group. Pressing again while a search is running just says it's still going.
`/find` does the same thing.

Re-run `import-history` with a fresh export every month or two so the profile stays current.

---

The sections below describe the full setup, including the optional post-writing mode
(`MODE=full`) from the first version.

## Setup (about 30 minutes)

1. **Bot.** Create a bot with @BotFather. Add it as an admin of @EduGrandsUz with *Post messages*.
   Create a private group for editors, add the bot, send `/help` there. It replies with the chat id
   and your user id.
2. **Config.** `cp .env.example .env` and fill in `ANTHROPIC_API_KEY`, `BOT_TOKEN`,
   `ADMIN_CHAT_ID`, `ADMIN_USER_IDS`.
3. **Platform dropdowns.** Put the exact dropdown values from the Filament form into
   `config/platform_options.yaml`. The AI can only choose from these lists.
4. **Yearly programmes.** Add every programme you post each year (FLEX, UWC, UGRAD, JACAFA...)
   as `page_watch` entries in `config/sources.yaml`.
5. **Don't repost old ones.** Export your current listings as a CSV with `title,url` columns and run
   `python -m edugrants_agent seed listings.csv`.
6. **Test locally** without posting anything:
   ```bash
   pip install -r requirements.txt
   python -m edugrants_agent run      # prints the drafts it would send
   python -m edugrants_agent health   # which sources worked
   ```
7. **Run it for real:** `python -m edugrants_agent bot`

### Hosting

It needs a process that runs all the time, so it can't live on the aHOST shared hosting.
Railway works well: new service from this repo (it uses the `Dockerfile`), add a **volume mounted
at `/app/data`** (the database lives there), paste the `.env` values into Variables. A small
instance is enough.

## Daily use (editors)

Each draft arrives with a header the channel never sees:

```
🆕 #142 · opportunitydesk · muddat: 18-noyabr
✔️ rasmiy sahifada tekshirildi · ishonch: high
Manbalar: e'lon | rasmiy sahifa
━━━━━━━━━━━━━━
<the post exactly as it will appear>
[✅ Chop etish] [✏️ Tahrirlash]
[📋 Platforma]  [❌ Rad etish]
```

- **✅** posts to the channel, then sends the listing to edugrants.uz with the post link attached.
- **✏️** sends the text back; edit it, reply to it, and the corrected draft comes back for approval.
- **📋** shows the platform form fields.
- Look closer at drafts marked **⚠️ faqat agregatordan** (organiser page couldn't be read) or
  **❓** (Uzbekistan eligibility unclear).

Commands: `/run` search now · `/queue` · `/stats` (includes AI cost) · `/health` · `/errors` · `/retry`

## Connecting edugrants.uz

Until `PLATFORM_WEBHOOK_URL` is set, approved listings are saved to `data/platform_exports/<id>.json`
so nothing is lost. To make it automatic, add one authenticated endpoint to the Laravel backend
that receives this JSON (fields mirror the *Create Extracurriculars* form):

```json
{
  "title": "...", "country": "Germaniya", "official_link": "...", "registration_link": "...",
  "deadline_type": "fixed", "deadline": "2026-11-18", "opening_date": null,
  "imkoniyat_turi": "Stipendiya", "daraja": "Bakalavr", "moliyalashtirish": "To'liq",
  "format": "Oflayn", "davomiylik": "1 yildan ortiq", "ariza_tolovi": "Bepul",
  "description": "...", "eligibility": "...", "benefits": "...",
  "application_process": "...", "additional_information": "...",
  "source_item_id": 142, "telegram_post_url": "https://t.me/EduGrandsUz/1234"
}
```

Sketch (adjust the model and column names to yours):

```php
// routes/api.php
Route::post('/agent/opportunities', AgentOpportunityController::class)->middleware('agent.token');

// app/Http/Controllers/AgentOpportunityController.php
public function __invoke(Request $r) {
    $o = Extracurricular::create([ /* map fields from $r->all() */ ]);
    return response()->json(['id' => $o->id]);
}
```

Set `PLATFORM_WEBHOOK_URL` and `PLATFORM_TOKEN` (sent as `Authorization: Bearer ...`).

## Cost

Rough estimate: a batched triage call per 20 titles, a link-finding and extraction call per
survivor (Haiku, about 1 cent each), and one writing call per draft (Sonnet, about 3 cents).
At 20 to 40 drafts a day that is roughly $1 to $2 a day, $30 to $60 a month. `/stats` shows the
real figure; set current prices in `.env` so it's accurate.

## Files

| File | What it does |
|---|---|
| `config/sources.yaml` | Where to look, audience description, keyword blocklist |
| `config/platform_options.yaml` | Allowed dropdown values for the platform form |
| `edugrants_agent/collectors.py` | RSS, official page watching, Telegram, newsletter inbox |
| `edugrants_agent/dedupe.py` | URL and title matching |
| `edugrants_agent/pipeline.py` | The stages and the filters (deadline, eligibility) |
| `edugrants_agent/llm.py` | Every Claude prompt |
| `edugrants_agent/render.py` | Exact post layout and platform payload |
| `edugrants_agent/bot.py` | Editors' bot and scheduler |

Run `python -m pytest` after changes.
