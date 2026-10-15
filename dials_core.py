#!/usr/bin/env python3
"""
Shared core for the dials bot: Trellus reporting API access, SQLite cache, and
period aggregation.

Local cache (SQLite) means repeat questions ("/week", "/leaderboard month") are
instant and cheap; only recent calls are re-pulled from Trellus.

Files:
  ~/.trellus_token                 Trellus API key (one line)
  ~/hubspot-dials-bot/config.json  timezone, backfill window, digest times
  ~/hubspot-dials-bot/dials.db     SQLite cache of call engagements
"""
import json, os, sqlite3, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HOME = os.path.expanduser("~")
BASE = os.path.dirname(os.path.abspath(__file__))
# On GitHub Actions everything mutable lives in DIALS_STATE_DIR (the Actions
# cache) and the keys come from env; unset = the Mac layout, unchanged.
STATE_DIR = os.environ.get("DIALS_STATE_DIR") or BASE
os.makedirs(STATE_DIR, exist_ok=True)
TR_TOKEN_FILE = os.path.join(HOME, ".trellus_token")
CONFIG_FILE = os.path.join(BASE, "config.json")
DB_FILE = os.path.join(STATE_DIR, "dials.db")

# Telegram config: dedicated file if present, else reuse the deal-notifier bot.
TG_FILES = [os.path.join(HOME, ".telegram_dials_bot"),
            os.path.join(HOME, ".telegram_hubspot_bot")]
TG_STATE_FILE = os.path.join(STATE_DIR, "telegram.json")

# Trellus dispositions are free-text labels with inconsistent casing
# ("Gatekeeper" / "Gate Keeper", "Not interested" / "Not Interested"), so they
# are compared by norm_label(). A human picked up the phone:
CONNECT_LABELS = {"booked", "meeting booked", "connected", "interested",
                  "high interest", "low interest", "not interested", "gatekeeper",
                  "left live message"}
# Reached the person you were actually calling (not a gatekeeper / message).
CONVO_LABELS = {"booked", "meeting booked", "connected", "interested",
                "high interest", "low interest", "not interested"}
# Trellus has no Booked disposition, so "Interested" stands in for a booking.
MEETING_LABELS = {"booked", "meeting booked", "interested"}
# Calls left undispositioned still count as a connect if a live conversation
# of at least this long happened (Trellus' own connected-call threshold).
# Pre-cutover history (ids "hs:…", imported from the old HubSpot cache) only
# has ring+talk duration, so the rule is not applied to it.
LIVE_CONNECT_S = 30


def norm_label(label):
    return " ".join((label or "").lower().replace("gate keeper", "gatekeeper").split())


# --------------------------------------------------------------------------- config

def load_config():
    cfg = {"timezone": "America/New_York", "backfill_days": 30,
           "sync_recent_days": 2, "portal_id": "", "digest_times": ["18:00"],
           "digest_days": [0, 1, 2, 3, 4], "quiet_digest_if_zero": True}
    if os.environ.get("DIALS_CONFIG_JSON"):
        cfg.update(json.loads(os.environ["DIALS_CONFIG_JSON"]))
    elif os.path.exists(CONFIG_FILE):
        cfg.update(json.load(open(CONFIG_FILE)))
    return cfg


CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])


# --------------------------------------------------------------------------- Trellus

RPT = "https://rpt-2.trellus.ai"
EXT = "https://ext.trellus.ai"


def tr_token():
    if os.environ.get("TRELLUS_TOKEN"):
        return os.environ["TRELLUS_TOKEN"].strip()
    with open(TR_TOKEN_FILE) as f:
        return f.read().strip()


def tr_api(method, url, body=None, token=None):
    """Trellus reporting API. RPT endpoints take a Bearer key; ext endpoints
    take it as an api_key header, so send both."""
    token = token or tr_token()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("api_key", token)
    req.add_header("Content-Type", "application/json")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 429 = rate limit, 5xx = transient; back off and retry.
            if (e.code == 429 or e.code >= 500) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise
    return None


# --------------------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id            TEXT PRIMARY KEY,
    ts_ms         INTEGER,
    owner_id      TEXT,
    direction     TEXT,
    disposition   TEXT,
    duration_ms   INTEGER,
    status        TEXT,
    to_number     TEXT,
    modified_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts_ms);
CREATE INDEX IF NOT EXISTS idx_calls_owner_ts ON calls(owner_id, ts_ms);

CREATE TABLE IF NOT EXISTS owners (
    id TEXT PRIMARY KEY, name TEXT, email TEXT
);
CREATE TABLE IF NOT EXISTS dispositions (
    id TEXT PRIMARY KEY, label TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
"""


def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()


# --------------------------------------------------------------------------- sync

def refresh_lookups(conn, token=None):
    """Trellus user names change rarely; refresh on each sync."""
    vis = tr_api("GET", EXT + "/visible", token=token) or {}
    emails = vis.get("user_id_to_email") or {}
    rows = [(u["user_id"], u.get("user_name") or emails.get(u["user_id"]) or u["user_id"],
             emails.get(u["user_id"]) or "") for u in vis.get("users", [])]
    if rows:
        conn.executemany("INSERT INTO owners(id,name,email) VALUES(?,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET name=excluded.name, email=excluded.email", rows)
    # Roster names win, so reports match config.json rather than Trellus profiles.
    conn.executemany("UPDATE owners SET name=? WHERE id=?",
                     [(b["name"], str(b["owner_id"])) for b in CFG.get("bdrs", []) if b.get("name")])
    conn.commit()


SESSION_COLS = ["session_id", "started_at", "resource_id", "dial_metric.is_inbound",
                "user_log.disposition", "dial_metric.live_duration", "customer_address"]
PAGE = 1000


def fetch_window(start_s, end_s, token=None):
    """All calls started in [start, end). session-list returns only the latest
    `limit` rows, so a full page means the window is split and retried."""
    body = {"start": int(start_s * 1e6), "end": int(end_s * 1e6), "limit": PAGE,
            "cnf": [[{"column_name": "conversation_type", "operator": "EQ", "value_safe": 1}]],
            "select": SESSION_COLS}
    rows = tr_api("POST", RPT + "/session-list-v6", body, token=token) or []
    if len(rows) >= PAGE and end_s - start_s > 60:
        mid = (start_s + end_s) // 2
        return fetch_window(start_s, mid, token) + fetch_window(mid, end_s, token)
    return [dict(zip(SESSION_COLS, r)) for r in rows]


def fetch_day(day_start_ms, day_end_ms, token=None):
    return fetch_window(day_start_ms // 1000, day_end_ms // 1000, token=token)


def upsert_calls(conn, results):
    """The Trellus disposition label is stored as-is; duration_ms holds live
    (talk) time, not ring time."""
    rows = []
    for r in results:
        if not r.get("session_id") or not r.get("started_at"):
            continue
        rows.append((r["session_id"], int(float(r["started_at"]) * 1000), r.get("resource_id") or "",
                     "INBOUND" if r.get("dial_metric.is_inbound") else "OUTBOUND",
                     r.get("user_log.disposition") or "",
                     int(float(r.get("dial_metric.live_duration") or 0) * 1000), "",
                     r.get("customer_address") or "", int(time.time() * 1000)))
    conn.executemany(
        "INSERT INTO calls(id,ts_ms,owner_id,direction,disposition,duration_ms,status,to_number,modified_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "ts_ms=excluded.ts_ms, owner_id=excluded.owner_id, direction=excluded.direction, "
        "disposition=excluded.disposition, duration_ms=excluded.duration_ms, status=excluded.status, "
        "to_number=excluded.to_number, modified_ms=excluded.modified_ms", rows)
    conn.commit()
    return len(rows)


def day_bounds_ms(d):
    """UTC ms bounds of local-timezone date `d`."""
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def sync(days=None, verbose=False, token=None):
    """Pull the last `days` local days into the cache. Defaults to a full
    backfill on first run, then just the recent window."""
    conn = db()
    refresh_lookups(conn, token=token)
    have_any = conn.execute("SELECT COUNT(*) c FROM calls").fetchone()["c"] > 0
    if days is None:
        days = CFG["sync_recent_days"] if have_any else CFG["backfill_days"]

    today = datetime.now(TZ).date()
    total = 0
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        s, e = day_bounds_ms(d)
        res = fetch_day(s, e, token=token)
        n = upsert_calls(conn, res) if res else 0
        total += n
        if verbose:
            print("  %s: %d calls" % (d.isoformat(), n), flush=True)
    meta_set(conn, "last_sync_ms", int(time.time() * 1000))
    conn.close()
    return total


# --------------------------------------------------------------------------- periods

def period_bounds(name, now=None):
    """(label, start_ms, end_ms) for a named period, in local time."""
    now = now or datetime.now(TZ)
    today = now.date()
    name = (name or "today").lower()
    if name in ("today", "t"):
        s, e = day_bounds_ms(today)
        return "Today (%s)" % today.strftime("%a %d %b"), s, e
    if name in ("yesterday", "y"):
        d = today - timedelta(days=1)
        s, e = day_bounds_ms(d)
        return "Yesterday (%s)" % d.strftime("%a %d %b"), s, e
    if name in ("week", "w"):
        start = today - timedelta(days=today.weekday())  # Monday
        s, _ = day_bounds_ms(start)
        _, e = day_bounds_ms(today)
        return "This week (from Mon %s)" % start.strftime("%d %b"), s, e
    if name in ("lastweek", "lw", "last-week"):
        start = today - timedelta(days=today.weekday() + 7)
        end = start + timedelta(days=6)
        s, _ = day_bounds_ms(start)
        _, e = day_bounds_ms(end)
        return "Last week (%s–%s)" % (start.strftime("%d %b"), end.strftime("%d %b")), s, e
    if name in ("month", "m"):
        start = today.replace(day=1)
        s, _ = day_bounds_ms(start)
        _, e = day_bounds_ms(today)
        return "This month (%s)" % start.strftime("%B"), s, e
    if name.endswith("d") and name[:-1].isdigit():   # e.g. "30d"
        n = int(name[:-1])
        start = today - timedelta(days=n - 1)
        s, _ = day_bounds_ms(start)
        _, e = day_bounds_ms(today)
        return "Last %d days" % n, s, e
    s, e = day_bounds_ms(today)
    return "Today (%s)" % today.strftime("%a %d %b"), s, e


# --------------------------------------------------------------------------- aggregation

def roster():
    """Everyone on the Trellus team is reported: [{name, owner_id, team}].
    Refreshed from /visible on every sync, so new Trellus users appear without
    a config change; config.json "bdrs" only overrides display names."""
    conn = db()
    rows = list(conn.execute("SELECT id, name FROM owners ORDER BY name"))
    conn.close()
    team = (CFG.get("teams") or [{"name": "Team"}])[0]["name"]
    skip = set(str(x) for x in CFG.get("exclude", []))
    return [{"name": r["name"], "owner_id": r["id"], "team": team}
            for r in rows if r["id"] not in skip]


def bdr_ids():
    """Owner ids of the dial team, or empty set if nothing has synced yet."""
    return set(str(b["owner_id"]) for b in roster())


def bdr_names():
    return [b["name"] for b in roster()]


def teams():
    """[(team name, emoji, [rep names])] — a single team of every Trellus user."""
    t = (CFG.get("teams") or [{"name": "Team", "emoji": ""}])[0]
    return [(t["name"], t.get("emoji", ""), bdr_names())]


def team_of(name):
    return teams()[0][0] if name in bdr_names() else ""


def _maps(conn):
    return {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM owners")}


def stats(conn, start_ms, end_ms, owner_id=None, cutoff_ms=None, owner_ids=None):
    """Aggregate outbound dials in a window. `cutoff_ms` trims to a time-of-day
    for like-for-like pace comparisons; `owner_ids` limits to a roster."""
    owners = _maps(conn)
    q = ("SELECT id, owner_id, disposition, duration_ms, ts_ms FROM calls "
         "WHERE ts_ms>=? AND ts_ms<? AND direction='OUTBOUND'")
    args = [start_ms, end_ms]
    if owner_id:
        q += " AND owner_id=?"
        args.append(owner_id)
    if owner_ids:
        ids = list(owner_ids)
        q += " AND owner_id IN (%s)" % ",".join("?" * len(ids))
        args.extend(ids)
    if cutoff_ms:
        q += " AND ts_ms<?"
        args.append(cutoff_ms)

    per_rep, per_disp = {}, {}
    totals = {"dials": 0, "connects": 0, "convos": 0, "meetings": 0, "talk_ms": 0}
    for r in conn.execute(q, args):
        key = norm_label(r["disposition"])
        label = key.capitalize() if key else "(none)"
        live = (not r["id"].startswith("hs:")
                and (r["duration_ms"] or 0) >= LIVE_CONNECT_S * 1000)
        name = owners.get(r["owner_id"], "Unassigned")
        rep = per_rep.setdefault(name, {"dials": 0, "connects": 0, "convos": 0,
                                        "meetings": 0, "talk_ms": 0, "owner_id": r["owner_id"]})
        rep["dials"] += 1
        totals["dials"] += 1
        per_disp[label] = per_disp.get(label, 0) + 1
        if key in CONNECT_LABELS or (not key and live):
            rep["connects"] += 1
            totals["connects"] += 1
            rep["talk_ms"] += r["duration_ms"] or 0
            totals["talk_ms"] += r["duration_ms"] or 0
        if key in CONVO_LABELS:
            rep["convos"] += 1
            totals["convos"] += 1
        if key in MEETING_LABELS:
            rep["meetings"] += 1
            totals["meetings"] += 1
    return {"totals": totals, "per_rep": per_rep, "per_disp": per_disp}


def daily_series(conn, start_ms, end_ms, owner_id=None, owner_ids=None):
    """[(date, dials)] by local day, including days with zero dials so the
    sparkline and bar charts show the true shape of the week."""
    rows = {}
    d = datetime.fromtimestamp(start_ms / 1000, TZ).date()
    last = datetime.fromtimestamp((end_ms - 1) / 1000, TZ).date()
    while d <= last:
        rows[d] = 0
        d += timedelta(days=1)

    q = ("SELECT ts_ms FROM calls WHERE ts_ms>=? AND ts_ms<? AND direction='OUTBOUND'")
    args = [start_ms, end_ms]
    if owner_id:
        q += " AND owner_id=?"
        args.append(owner_id)
    if owner_ids:
        ids = list(owner_ids)
        q += " AND owner_id IN (%s)" % ",".join("?" * len(ids))
        args.extend(ids)
    for r in conn.execute(q, args):
        day = datetime.fromtimestamp(r["ts_ms"] / 1000, TZ).date()
        rows[day] = rows.get(day, 0) + 1
    return sorted(rows.items())


def find_owner(conn, text):
    """Fuzzy rep lookup by name or email fragment."""
    t = (text or "").strip().lower()
    if not t:
        return None
    rows = list(conn.execute("SELECT id,name,email FROM owners"))
    for r in rows:
        if r["name"].lower() == t or r["email"].lower() == t:
            return r
    hits = [r for r in rows if t in r["name"].lower() or t in r["email"].lower()]
    if len(hits) == 1:
        return hits[0]
    # Prefer a rep who has actually dialled, if the fragment is ambiguous.
    if hits:
        ids = ",".join("'%s'" % h["id"] for h in hits)
        act = conn.execute("SELECT owner_id, COUNT(*) c FROM calls WHERE owner_id IN (%s) "
                           "GROUP BY owner_id ORDER BY c DESC LIMIT 1" % ids).fetchone()
        if act:
            for h in hits:
                if h["id"] == act["owner_id"]:
                    return h
        return hits[0]
    return None


# --------------------------------------------------------------------------- telegram

def tg_config():
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if tok:
        # Actions: chats/home live in the cached state dir so /allow and /here
        # survive restarts; the token is only ever taken from the secret.
        if os.path.exists(TG_STATE_FILE):
            cfg = json.load(open(TG_STATE_FILE))
        else:
            cfg = {"chat_id": os.environ["TELEGRAM_CHAT_ID"],
                   "allowed_chats": [c for c in os.environ.get("TELEGRAM_ALLOWED", "").split(",") if c]}
            home = os.environ.get("TELEGRAM_HOME", "")
            if home:
                hc, _, ht = home.partition(":")
                cfg["home"] = {"chat_id": hc, "thread_id": int(ht) if ht else None}
            json.dump(cfg, open(TG_STATE_FILE, "w"))
        cfg["bot_token"] = tok.strip()
        cfg["_file"] = TG_STATE_FILE
        return cfg
    for f in TG_FILES:
        if os.path.exists(f):
            cfg = json.load(open(f))
            cfg["_file"] = f
            return cfg
    raise SystemExit("No Telegram config found. Run: python3 %s/setup_bot.py <BOT_TOKEN>" % BASE)


def tg_call(method, payload=None, cfg=None, timeout=60):
    cfg = cfg or tg_config()
    url = "https://api.telegram.org/bot%s/%s" % (cfg["bot_token"], method)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read().decode()[:400]}
    except Exception as e:
        # A read timeout on the 25s long poll used to escape this function and
        # kill the process. Every network fault is now just a failed call.
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def tg_send(text, chat_id=None, cfg=None, thread_id=None):
    """thread_id targets a single topic in a forum group; omit it for a normal
    chat or the General topic."""
    cfg = cfg or tg_config()
    payload = {"chat_id": chat_id or cfg["chat_id"], "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    return tg_call("sendMessage", payload, cfg=cfg)


# --------------------------------------------------------------------------- formatting

def fmt_dur(ms):
    s = int((ms or 0) / 1000)
    h, m = s // 3600, (s % 3600) // 60
    if h:
        return "%dh %02dm" % (h, m)
    return "%dm" % m


def pct(a, b):
    return "%.0f%%" % (100.0 * a / b) if b else "—"
