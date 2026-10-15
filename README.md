# Dial tracker (Telegram) — Trellus

> **Source switched to Trellus on 2026-09-22.** Calls come from the Trellus
> Reporting API (`rpt-2.trellus.ai/session-list-v6`, key in `~/.trellus_token`).
> The roster is every user on the Trellus account (from `ext.trellus.ai/visible`);
> `bdrs` in config.json only overrides display names. History before 21 Sep is
> imported from the old HubSpot cache (ids `hs:…`); the pre-switch files are in
> `pre_trellus_backup/`. Trellus has no Booked disposition, so **Interested
> counts as booked**. Undispositioned Trellus calls with ≥30s live talk count as
> connects. Sections below that mention HubSpot describe the old setup.

A Telegram bot that reports everyone's dial activity — team totals,
per-rep breakdowns, connect rates, and how today is pacing against a normal day.

Calls are already logged in HubSpot by the dialer, so this bot only **reads**.
It never writes to the CRM.

## Setup (one time)

1. In Telegram, message **@BotFather** → `/newbot` → pick a name → copy the token.
2. Send your new bot any message (this is what reveals your chat id).
3. Connect it and start it:

```bash
cd ~/hubspot-dials-bot
python3 setup_bot.py <BOT_TOKEN>
bash install.sh
```

`install.sh` registers a launchd agent, so the bot starts at login and restarts
if it crashes.

To use it in a team group instead: add the bot to the group, send `/today`, and
the bot will DM you the group's chat id. Add that id to `allowed_chats` in
`~/.telegram_dials_bot`, then `bash install.sh` again.

## Commands

| Command | What you get |
|---|---|
| `/dials [period]` | BDR summary — dials, connects, convos, booked, talk time, per-rep table |
| `/today` `/yesterday` `/week` `/lastweek` `/month` | Same summary for that period |
| `/pace` | Dials so far today vs the usual pace by this hour, plus a projected close |
| `/leaderboard [period]` | BDRs ranked by dials |
| `/rep <name> [period]` | One rep's detail with a per-day bar chart (works for anyone, not just BDRs) |
| `/all [period]` | Everyone in the portal, not just the BDR team |
| `/reps` | Who has been dialling in the last 30 days, BDRs listed first |
| `/sync` | Pull the latest calls from HubSpot immediately |
| `/status` | Cache freshness, row count, date range |

Periods: `today`, `yesterday`, `week`, `lastweek`, `month`, `7d`, `30d`.

## The BDR roster

Reports default to the BDR team, set in `bdrs` in `config.json`:

> Joshua Galveias · Kristian Fraser · Zarret Mills · Malik Djotni · Ayush Chavan ·
> Nate Babin · Jake Schnur · Price Davis · Cooper Stevens

Two deliberate behaviours:

- A BDR with **zero dials** still appears as a `0` row — that absence is the whole
  point of the report, so it is never dropped for being empty.
- Dials by non-BDRs are **not hidden**. Every summary ends with a line like
  *"Outside the BDR team: 891 dials from 8 others (Louis 267, Thomas 190…)"*, and
  `/all` shows the unfiltered picture.

Add or remove people by editing `bdrs` (name plus HubSpot `owner_id`) and
restarting the bot. Get owner ids from `/reps` or the HubSpot owners API.

## Automatic digests

Once a day on weekdays, at **18:00 ET**, the bot posts that day's dials,
conversations and bookings without being asked. Change the time (or add more) in
`digest_times` in `config.json`. Days with zero dials stay silent
(`quiet_digest_if_zero`).

## How the metrics are defined

The daily display shows three numbers. Only **outbound** calls count as dials, and
the other two are derived from HubSpot call dispositions:

- **Dial** — any outbound call
- **Conversation** — reached the actual person, not a gatekeeper or a voicemail:
  Connected, Booked, High Interest, Low Interest, Not Interested
- **Booking** — the Booked disposition

A fourth measure, **connect** (anyone answered, including Gate Keeper and Left
live message), is still tracked and shown in `/rep` and `/leaderboard`.

Change the mapping in `CONVO_LABELS` / `CONNECT_LABELS` in `dials_core.py`.

## How it works

`dials_core.py` keeps a local SQLite cache (`dials.db`) of call engagements,
synced from the HubSpot search API in one-day windows (which keeps each query
under HubSpot's 10,000-result ceiling). The bot refreshes today's calls every
3 minutes and re-pulls the last 2 days every 30 minutes to catch late edits.
Reports read the cache, so `/month` answers instantly instead of making hundreds
of API calls.

Day boundaries use `timezone` from `config.json` (America/New_York), so "today"
means the reps' working day, not UTC and not UK time.

## Files

```
config.json      timezone, backfill window, digest schedule
dials_core.py    HubSpot API, SQLite cache, aggregation
dials_report.py  message formatting
dials_bot.py     Telegram long-poll loop, digests, CLI entrypoints
setup_bot.py     one-time Telegram connection
install.sh       launchd install
dials.db         local cache (rebuildable: python3 dials_bot.py --sync 45)
```

## Command line

```bash
python3 dials_bot.py --print /today     # render a report to the terminal
python3 dials_bot.py --sync 7           # sync the last 7 days
python3 dials_bot.py --digest           # send a digest now
```

Credentials: HubSpot token in `~/.hubspot_token`, Telegram in
`~/.telegram_dials_bot`.

## Hosting on GitHub Actions (since 2026-10-15)

`poll.yml` runs the bot for ~5h45m, saves state to the Actions cache, then
starts its successor; `watchdog.yml` (every 15 min) restarts the chain if it
stops. Gated on repo variable `DIALS_BOT_ON_GITHUB=true`. Secrets:
`TRELLUS_TOKEN`, `TELEGRAM_BOT_TOKEN`. Variable `DIALS_CONFIG_JSON` holds
config.json (kept out of this public repo). Never run the Mac launchd agent at
the same time — two pollers steal each other's Telegram updates (409s).
