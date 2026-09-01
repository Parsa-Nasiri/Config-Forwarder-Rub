# Tele2Rubika

A bridge that watches Telegram config channels and forwards fresh, scored proxy
configs to a **Rubika bot** — for you *and* anyone else who starts the bot.

```
Telegram channels ──► Telethon monitor ──► extract · dedupe · score ──► delivery engine ──► Rubika bot chat
                                            (vless vmess trojan ss      (per-user filters,      (digest cards,
                                             ssr hysteria2 tuic ...      adaptive batching,      keypads, i18n
                                                                          rate limiting)          en/fa)
```

- **Multi-user** — every Rubika chat that presses *Start* gets its own queue,
  filters, language and personal protocol affinity.
- **Selective, not spammy** — configs are fingerprinted (reposts across
  channels collapse to one), scored 0–100, and delivered as tidy numbered
  digests with copy buttons.
- **Self-correcting** — tapping ❌ *dead* penalises the config, its server and
  the channel that published it; tapping ✅ *works* or 📋 *copy* rewards it.
- **24/7 on GitHub Actions** — chained runs hand over before the 6-hour job
  limit, with zero-gap leadership and a watchdog as backup.
- **Bilingual** — English and فارسی throughout.

## How it works

| Piece | What it does |
|---|---|
| `bot/telegram_side/` | Telethon user-client watches your channels (live events + catch-up + subscription links + attached files) |
| `bot/engine/extractor.py` | Parses vless / vmess / trojan / ss / ssr / hysteria2 / tuic / snell / wireguard URIs, base64 subscription blobs and Clash YAML/JSON; strips zero-width junk |
| `bot/engine/scorer.py` | 0–100 score: protocol + transport + encryption + freshness + corroboration + source reputation + optional health probe + crowd feedback + anti-spam |
| `bot/engine/dispatcher.py` | Per-user fan-out, adaptive batching (channel storms become a few fat digests), round-robin fairness, token-bucket rate limits, retries with backoff |
| `bot/rubika_side/` | Rubika Bot API v3 client (long polling), keypads, command handling |
| `bot/storage/` | Supabase (Postgres) persistence — users, configs, delivery queue, leader lease, Telethon session — with an in-memory fallback for local dev |
| `bot/runner/` | Leader election, GitHub handoff, heartbeat |
| `.github/workflows/` | The 24/7 machinery (see below) |

## Setup

### 1. Telegram (one-time, on your own machine)

1. Get an API ID/hash at <https://my.telegram.org> → *API development tools*.
2. Install and log in:

   ```bash
   pip install -r requirements.txt
   python scripts/auth_telegram.py
   ```

   It asks for your phone, the login code and (if enabled) your 2FA password,
   verifies each channel in `TELEGRAM_CHANNELS` is reachable with your account,
   then prints a **session string**. That string is a logged-in session — treat
   it like a password.

### 2. Rubika bot

1. Create a bot at <https://rubika.ir/bot> (BotFather) and copy the token.

### 3. Supabase

1. Create a project at <https://supabase.com>.
2. Open the SQL editor and run the whole of
   [`bot/storage/schema.sql`](bot/storage/schema.sql). It is idempotent — safe
   to re-run.
3. Copy the **Project URL** and the **service_role** key
   (Settings → API). The service key bypasses row-level security; every table
   has RLS enabled so the anon key gets nothing even if it leaks.

### 4. GitHub secrets and variables

In your repo: *Settings → Secrets and variables → Actions*.

**Secrets** (required):

| Secret | Value |
|---|---|
| `TELEGRAM_API_ID` | from my.telegram.org |
| `TELEGRAM_API_HASH` | from my.telegram.org |
| `TELEGRAM_SESSION_STRING` | printed by `scripts/auth_telegram.py` |
| `TELEGRAM_CHANNELS` | comma-separated `@channel1,@channel2` (or set as a variable) |
| `RUBIKA_BOT_TOKEN` | from Rubika BotFather |
| `SUPABASE_URL` | `https://xxxx.supabase.co` |
| `SUPABASE_SERVICE_KEY` | service_role key |

**Variables** (optional tuning — sensible defaults are baked in):

`MIN_SCORE` (55), `INSTANT_SCORE` (88), `BATCH_MAX` (5), `BATCH_WINDOW` (45s),
`USER_HOURLY_CAP` (60), `RATE_LIMIT_PER_CHAT` (12), `RATE_LIMIT_GLOBAL` (120),
`HEALTH_CHECK_ENABLED` (false), `TELEGRAM_CATCHUP_LIMIT` (50),
`DEFAULT_LANGUAGE` (en), `MAX_RUNTIME` (20400), `HANDOFF_LEAD` (180).

### 5. Start it

Push to `main`, then run the workflow manually once:
*Actions → Tele2Rubika Bot → Run workflow*. The chain takes over from there.

## The 24/7 chain (how it beats the 6-hour limit)

GitHub kills any job at 6 hours, so the bot never tries to live that long:

1. Each run lives **5h40m** (`MAX_RUNTIME=20400`).
2. **3 minutes before** its own deadline it fires a `repository_dispatch`
   (`bot-handoff`) that queues the next run.
3. A **concurrency group** keeps at most one run active; the new run boots
   while the old one is still finishing.
4. A **leader lease in Supabase** (90s TTL, compare-and-swap) guarantees that
   even if two runners overlap, exactly one polls Telegram/Rubika — the other
   idles as a hot standby and takes over in under a second.
5. A **watchdog workflow** checks the lease hourly and restarts the chain if
   nobody holds it.
6. A scheduled run every 2 hours is a final safety net (it no-ops while the
   bot is healthy), and the committed `status/last-run.json` heartbeat keeps
   the repo "active" so GitHub never disables the schedule on a public repo.

If a run dies before handing over, the workflow's fallback step re-arms the
chain from the shell.

## Using the bot

From Rubika, open your bot and press **Start**. Then:

- **⚙️ Filters** — pick protocols, minimum score (40/55/70/85), digest size,
  and *live mode* (instant push for top configs).
- **⚡ Send now** — flush whatever is queued immediately.
- **📊 Stats** — your delivery numbers plus global counts.
- **🏆 Top** — the best configs seen recently.
- **⏸ Pause** — snooze for 1/6/24 hours.
- **🌐 Language** — switch between English and فارسی.
- On each digest, numbered buttons copy an individual config; the expanded
  view adds ✅ *works* / ❌ *dead* feedback, which teaches the scorer.

Text commands also work: `/start` `/help` `/latest` `/filters` `/stats`
`/top` `/pause` `/resume` `/lang fa`.

## Local development

```bash
pip install -r requirements.txt
python -m bot.main --check        # validate configuration
python -m bot.main --self-test    # parser corpus (8 real-world URIs)
pytest -q                         # 130 unit tests, no network needed
python -m bot.main --max-runtime 120   # short local run
```

Without Supabase credentials the bot runs on an in-memory store — fine for a
quick look, but state resets on every restart.

## Project layout

```
bot/
  config.py              all settings, from environment only
  main.py                orchestrator + CLI
  engine/                extractor, models, geo, scorer, dispatcher, rate limits, health
  telegram_side/         Telethon monitor (session persisted in Supabase)
  rubika_side/           Bot API v3 client, keypads, update poller
  handlers/              command + button routing (per-user state)
  ux/                    bilingual message rendering
  storage/               Store contract, Supabase + memory implementations, schema.sql
  runner/                leader lock, GitHub handoff, heartbeat
scripts/
  auth_telegram.py       one-time interactive Telegram login
  watchdog.py            liveness probe used by the watchdog workflow
tests/                   130 tests (extractor, scorer, dispatcher, storage, UX, wire format, runner)
.github/workflows/       bot.yml (the chain), watchdog.yml, ci.yml
```

## Notes

- The Telethon session string grants full access to your Telegram account.
  Keep it in secrets, never in the repo.
- Rubika Bot API v3 quirks are handled: every method is POST, `getUpdates`
  long-polls with `offset_id`, keypads use the documented
  `rows → buttons → {id, type, button_text}` model, and rich-text metadata
  uses UTF-16 offsets (so emoji in titles bold correctly).
