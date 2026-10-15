#!/usr/bin/env python3
"""Report builders — turn cached call data into Telegram-ready messages."""
from datetime import datetime, timedelta

from dials_core import (TZ, db, stats, daily_series, period_bounds, find_owner,
                        fmt_dur, pct, bdr_ids, bdr_names, teams, team_of)

BAR = "▁▂▃▄▅▆▇█"


def _scope_ids(scope=None):
    """Always the BDR roster. Other people's dials are never reported."""
    return bdr_ids() or None


def _flag(name):
    for team, emoji, members in teams():
        if name in members:
            return emoji
    return ""


def _zero_fill_bdrs(per_rep, scope):
    """A BDR who hasn't dialled is the most important row in the report, so
    make sure they appear as a zero rather than vanishing."""
    if scope == "all" or not bdr_ids():
        return per_rep
    for name in bdr_names():
        per_rep.setdefault(name, {"dials": 0, "connects": 0, "convos": 0,
                                  "meetings": 0, "talk_ms": 0, "owner_id": ""})
    return per_rep


def _team_block(per_rep, team, emoji, members):
    """One team's header line and table. Members with no dials still appear."""
    rows = sorted(((n, per_rep.get(n, {"dials": 0, "convos": 0, "meetings": 0}))
                   for n in members), key=lambda kv: -kv[1]["dials"])
    d = sum(s["dials"] for _, s in rows)
    c = sum(s["convos"] for _, s in rows)
    b = sum(s["meetings"] for _, s in rows)

    head = "%s <b>%s — %d dials · %d convo%s · %d interested</b>" % (
        emoji, team, d, c, "" if c == 1 else "s", b)
    table = ["%-11s %6s %6s %6s" % ("Rep", "Dials", "Convo", "Int")]
    for name, s in rows:
        parts = name.split()
        short = "%s %s" % (parts[0], parts[-1][0]) if len(parts) > 1 else parts[0]
        table.append("%-11s %6d %6d %6d" % (short[:11], s["dials"], s["convos"], s["meetings"]))
    return head + "\n<pre>" + "\n".join(table) + "</pre>"


def sparkline(values):
    if not values:
        return ""
    hi = max(values)
    if hi == 0:
        return BAR[0] * len(values)
    return "".join(BAR[min(len(BAR) - 1, int(v / hi * (len(BAR) - 1)))] for v in values)


def _rep_table(per_rep, limit=None):
    """Dials / conversations / interested — the three numbers that matter."""
    rows = sorted(per_rep.items(), key=lambda kv: -kv[1]["dials"])
    if limit:
        rows = rows[:limit]
    if not rows:
        return "  (no dials)"
    width = min(16, max(len(n.split()[0]) + 3 for n, _ in rows) + 1)
    out = ["%-*s %6s %6s %6s" % (width, "Rep", "Dials", "Convo", "Bkd")]
    for name, s in rows:
        short = name.split()[0]
        if len(name.split()) > 1:
            short = "%s %s" % (name.split()[0], name.split()[-1][0])
        out.append("%-*s %6d %6d %6d" % (width, short[:width], s["dials"],
                                         s["convos"], s["meetings"]))
    return "\n".join(out)


def summary(period="today", top=None, scope="bdr"):
    conn = db()
    ids = bdr_ids() or None          # BDRs only — nobody else is ever reported
    label, s_ms, e_ms = period_bounds(period)
    st = stats(conn, s_ms, e_ms, owner_ids=ids)
    t = st["totals"]

    lines = ["📞 <b>Dials — %s</b>" % label, ""]
    lines.append("<b>%d dials</b>   •   <b>%d conversation%s</b>   •   <b>%d interested</b>"
                 % (t["dials"], t["convos"], "" if t["convos"] == 1 else "s",
                    t["meetings"]))
    lines.append("Conversation rate %s  ·  interested from %s of conversations"
                 % (pct(t["convos"], t["dials"]), pct(t["meetings"], t["convos"])))

    for team, emoji, members in teams():
        if members:
            lines += ["", _team_block(st["per_rep"], team, emoji, members)]
    conn.close()
    return "\n".join(lines)


def leaderboard(period="today", scope="bdr"):
    conn = db()
    ids = _scope_ids(scope)
    label, s_ms, e_ms = period_bounds(period)
    st = stats(conn, s_ms, e_ms, owner_ids=ids)
    rows = sorted(_zero_fill_bdrs(st["per_rep"], scope).items(), key=lambda kv: -kv[1]["dials"])
    lines = ["🏆 <b>Dial leaderboard — %s</b>" % label, ""]
    if not rows:
        lines.append("No dials logged in this period.")
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, s) in enumerate(rows):
        badge = medals[i] if i < 3 else "  %d." % (i + 1)
        lines.append("%s %s <b>%s</b> — %d dials · %d convo · %d interested"
                     % (badge, _flag(name), name, s["dials"], s["convos"], s["meetings"]))
    conn.close()
    return "\n".join(lines)


def rep_report(query, period="week"):
    conn = db()
    o = find_owner(conn, query)
    if not o:
        conn.close()
        return "No rep matching “%s”. Try /reps to list everyone." % query
    label, s_ms, e_ms = period_bounds(period)
    st = stats(conn, s_ms, e_ms, owner_id=o["id"])
    t = st["totals"]
    lines = ["👤 <b>%s — %s</b>" % (o["name"], label), ""]
    lines.append("<b>%d dials</b> · %d connects (%s) · %d convos · %d interested · %s talk"
                 % (t["dials"], t["connects"], pct(t["connects"], t["dials"]),
                    t["convos"], t["meetings"], fmt_dur(t["talk_ms"])))
    series = daily_series(conn, s_ms, e_ms, owner_id=o["id"])
    if series:
        vals = [v for _, v in series]
        lines += ["", "<pre>" + "\n".join("%s  %4d  %s" % (d.strftime("%a %d"), v,
                  "█" * min(30, int(v / max(vals) * 30) if max(vals) else 0))
                  for d, v in series) + "</pre>"]
    if st["per_disp"]:
        lines.append("Outcomes: " + " · ".join(
            "%s %d" % (k, v) for k, v in sorted(st["per_disp"].items(), key=lambda kv: -kv[1])[:8]))
    conn.close()
    return "\n".join(lines)


def pace(scope="bdr"):
    """Today so far vs the same clock time on recent weekdays."""
    conn = db()
    ids = _scope_ids(scope)
    now = datetime.now(TZ)
    _, s_ms, e_ms = period_bounds("today", now)
    today = stats(conn, s_ms, e_ms, owner_ids=ids)["totals"]["dials"]

    seconds_into_day = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()
    comps, d = [], now.date()
    for back in range(1, 15):
        prev = d - timedelta(days=back)
        if prev.weekday() > 4:
            continue
        ps, pe = period_bounds("today", datetime(prev.year, prev.month, prev.day, tzinfo=TZ))[1:]
        pcut = ps + int(seconds_into_day * 1000)
        n = stats(conn, ps, pe, cutoff_ms=pcut, owner_ids=ids)["totals"]["dials"]
        full = stats(conn, ps, pe, owner_ids=ids)["totals"]["dials"]
        comps.append((prev, n, full))
        if len(comps) == 5:
            break
    conn.close()

    lines = ["⏱ <b>Pace%s — %s</b>" % ("" if ids else " (everyone)",
                                       now.strftime("%a %d %b, %H:%M %Z")), "",
             "<b>%d dials so far today</b>" % today]
    if comps:
        avg = sum(c[1] for c in comps) / len(comps)
        avg_full = sum(c[2] for c in comps) / len(comps)
        delta = today - avg
        arrow = "🟢 ahead" if delta >= 0 else "🔴 behind"
        lines.append("%s of the weekday average by this hour (%.0f) — %+.0f" % (arrow, avg, delta))

        # Project from the median by-now→full-day ratio, and only from days with a
        # real baseline: the floor starts ~10am ET, so a 09:30 sample of 3 dials
        # would otherwise imply a wildly inflated close.
        ratios = sorted(f / float(n) for _, n, f in comps if n >= 25 and f)
        if ratios and today >= 25:
            med = ratios[len(ratios) // 2]
            lines.append("Projected close: <b>~%d</b> (avg full day %.0f)"
                         % (round(today * med), avg_full))
        else:
            lines.append("Too early to project — typical full day is %.0f." % avg_full)
        lines += ["", "<pre>" + "\n".join("%s  %4d by now   %4d full day" %
                  (dt.strftime("%a %d"), n, f) for dt, n, f in comps) + "</pre>"]
    else:
        lines.append("Not enough history yet for a comparison.")
    return "\n".join(lines)


def reps_list():
    conn = db()
    _, s, e = period_bounds("30d")
    st = stats(conn, s, e, owner_ids=_scope_ids())
    conn.close()
    per_rep = _zero_fill_bdrs(st["per_rep"], "bdr")
    out = ["<b>Dial activity — last 30 days</b>"]
    for team, emoji, members in teams():
        if not members:
            continue
        rows = sorted(((n, per_rep.get(n, {"dials": 0})) for n in members),
                      key=lambda kv: -kv[1]["dials"])
        out += ["", "%s <b>%s</b>" % (emoji, team)]
        out += ["• %s — %d dials" % (n, s["dials"]) for n, s in rows]
    return "\n".join(out)


def help_text():
    return (
        "📞 <b>Dial tracker</b> — dials, conversations and interested from Trellus\n\n"
        "<b>/dials</b> [period] — BDR team summary (defaults to today)\n"
        "<b>/today</b> · <b>/yesterday</b> · <b>/week</b> · <b>/lastweek</b> · <b>/month</b>\n"
        "<b>/pace</b> — today's dials vs the usual pace by this hour\n"
        "<b>/leaderboard</b> [period] — BDRs ranked by dials\n"
        "<b>/rep</b> &lt;name&gt; [period] — one rep's detail (e.g. <code>/rep zarret week</code>)\n"
        "<b>/reps</b> — who's been dialling\n"
        "<b>/sync</b> — pull the latest calls from Trellus now\n"
        "<b>/status</b> — cache freshness and size\n\n"
        "Periods: today, yesterday, week, lastweek, month, 7d, 30d.\n"
        "Reports cover the <b>%d BDRs</b> only: %s.\n"
        "Digests land automatically on weekdays; edit <code>bdrs</code> / "
        "<code>digest_times</code> in config.json."
        % (len(bdr_names()), ", ".join(n.split()[0] for n in bdr_names()))
    )


def status_text():
    import os
    from dials_core import meta_get, DB_FILE, CFG
    conn = db()
    n = conn.execute("SELECT COUNT(*) c FROM calls").fetchone()["c"]
    rng = conn.execute("SELECT MIN(ts_ms) a, MAX(ts_ms) b FROM calls").fetchone()
    last = meta_get(conn, "last_sync_ms")
    conn.close()
    lines = ["⚙️ <b>Status</b>", "", "Cached calls: <b>%d</b>" % n]
    if rng and rng["a"]:
        lines.append("Range: %s → %s" % (
            datetime.fromtimestamp(rng["a"] / 1000, TZ).strftime("%d %b %Y"),
            datetime.fromtimestamp(rng["b"] / 1000, TZ).strftime("%d %b %Y %H:%M")))
    if last:
        ago = (datetime.now(TZ) - datetime.fromtimestamp(int(last) / 1000, TZ)).total_seconds()
        lines.append("Last sync: %d min ago" % (ago // 60))
    lines.append("Timezone: %s" % CFG["timezone"])
    lines.append("DB: %.1f MB" % (os.path.getsize(DB_FILE) / 1e6))
    return "\n".join(lines)
