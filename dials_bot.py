#!/usr/bin/env python3
"""
Telegram dial-tracking bot.

Runs a single long-lived process that:
  • answers commands (/today, /pace, /leaderboard, /rep ...)
  • keeps the local call cache warm from Trellus
  • posts scheduled digests on weekdays

Usage:
  python3 dials_bot.py              # run the bot (normally under launchd)
  python3 dials_bot.py --sync N     # one-off sync of the last N days, then exit
  python3 dials_bot.py --digest     # send the digest once, then exit
  python3 dials_bot.py --print CMD  # render a command to stdout (no Telegram)
"""
import json, os, sys, time, traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dials_core as C
import dials_report as R

STATE_FILE = os.path.join(C.STATE_DIR, "bot_state.json")
SYNC_TODAY_EVERY = 180        # seconds
SYNC_DEEP_EVERY = 1800        # seconds; re-pulls the recent window for late edits


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except ValueError:
            pass
    return {"offset": 0, "digests_sent": []}


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    json.dump(s, open(tmp, "w"))
    os.replace(tmp, STATE_FILE)


def allowed_chats(cfg):
    ids = {str(cfg["chat_id"])}
    ids.update(str(x) for x in cfg.get("allowed_chats", []))
    return ids


def persist_allowed(cfg, chat_id):
    """Add a chat to allowed_chats on disk so it survives a restart."""
    path = cfg["_file"]
    data = json.load(open(path))
    allowed = [str(x) for x in data.get("allowed_chats", [])]
    if str(chat_id) not in allowed:
        allowed.append(str(chat_id))
    data["allowed_chats"] = allowed
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    cfg["allowed_chats"] = allowed
    return allowed


def forget_allowed(cfg, chat_id):
    path = cfg["_file"]
    data = json.load(open(path))
    allowed = [str(x) for x in data.get("allowed_chats", []) if str(x) != str(chat_id)]
    data["allowed_chats"] = allowed
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    cfg["allowed_chats"] = allowed
    return allowed


# --------------------------------------------------------------------------- commands

def handle(text):
    """Map a command string to a reply. Returns None for non-commands."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split()
    cmd = parts[0][1:].split("@")[0].lower()
    args = parts[1:]

    if cmd in ("start", "help"):
        return R.help_text()
    if cmd in ("today", "yesterday", "week", "lastweek", "month"):
        return R.summary(cmd)
    if cmd == "dials":
        return R.summary(args[0] if args else "today")
    if cmd in ("leaderboard", "lb", "board"):
        return R.leaderboard(args[0] if args else "today")
    if cmd == "pace":
        return R.pace()
    if cmd == "rep":
        if not args:
            return "Usage: <code>/rep &lt;name&gt; [period]</code>  e.g. <code>/rep zarret week</code>"
        periods = {"today", "yesterday", "week", "lastweek", "month", "7d", "30d", "14d", "90d"}
        period, name_parts = "week", list(args)
        if name_parts[-1].lower() in periods:
            period = name_parts.pop().lower()
        return R.rep_report(" ".join(name_parts), period)
    if cmd == "reps":
        return R.reps_list()
    if cmd == "status":
        return R.status_text()
    if cmd == "sync":
        n = C.sync(days=int(args[0]) if args and args[0].isdigit() else 2)
        return "✅ Synced %d call records.\n\n%s" % (n, R.status_text())
    return "Unknown command. Try /help"


# --------------------------------------------------------------------------- digests

def digest_body():
    """The daily wrap-up: today's dials, conversations and interested."""
    return R.summary("today")


def digest_targets(cfg):
    """[(chat_id, thread_id)] the daily digest posts to. A pinned topic wins;
    otherwise any approved group; otherwise the owner's DM."""
    home = cfg.get("home")
    if home:
        return [(str(home["chat_id"]), home.get("thread_id"))]
    explicit = cfg.get("digest_chats")
    if explicit:
        return [(str(x), None) for x in explicit]
    groups = [str(x) for x in cfg.get("allowed_chats", [])]
    return [(g, None) for g in groups] or [(str(cfg["chat_id"]), None)]


def set_home(cfg, chat_id, thread_id):
    """Pin the bot to one chat — and, in a forum group, one topic."""
    path = cfg["_file"]
    data = json.load(open(path))
    home = {"chat_id": str(chat_id)}
    if thread_id:
        home["thread_id"] = int(thread_id)
    data["home"] = home
    allowed = [str(x) for x in data.get("allowed_chats", [])]
    if str(chat_id) not in allowed and str(chat_id) != str(data.get("chat_id")):
        allowed.append(str(chat_id))
    data["allowed_chats"] = allowed
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    cfg["home"] = home
    cfg["allowed_chats"] = allowed
    return home


def off_home(cfg, chat_id, thread_id):
    """True for anything outside the pinned chat/topic.

    Once /here has pinned the bot to a topic, that topic is the only place it
    answers: other topics in the group, other groups, and DMs are ignored. The
    owner's DM keeps working for the admin commands handled before this check,
    so there is always a way to re-pin it.
    """
    home = cfg.get("home")
    if not home:
        return False
    if str(chat_id) != str(home["chat_id"]):
        return True
    if home.get("thread_id"):
        return str(thread_id or "") != str(home["thread_id"])
    return False


def wrong_topic(cfg, chat_id, thread_id):
    """True when a message is in the pinned chat but the wrong topic, so the
    bot stays silent everywhere except its own topic."""
    home = cfg.get("home")
    if not home or not home.get("thread_id"):
        return False
    if str(chat_id) != str(home["chat_id"]):
        return False
    return str(thread_id or "") != str(home["thread_id"])


def maybe_digest(state, cfg):
    now = datetime.now(C.TZ)
    if now.weekday() not in C.CFG.get("digest_days", [0, 1, 2, 3, 4]):
        return
    for hhmm in C.CFG.get("digest_times", []):
        try:
            h, m = [int(x) for x in hhmm.split(":")]
        except ValueError:
            continue
        key = "%s %s" % (now.date().isoformat(), hhmm)
        if key in state["digests_sent"]:
            continue
        due = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if due > now:
            continue
        # Send late rather than not at all: if the Mac was asleep at 18:00, the
        # digest goes out whenever it wakes, still labelled with its own date.
        late_by = (now - due).total_seconds()
        C.sync(days=1)
        conn = C.db()
        n = C.stats(conn, *C.period_bounds("today")[1:],
                    owner_ids=C.bdr_ids() or None)["totals"]["dials"]
        conn.close()
        if n or not C.CFG.get("quiet_digest_if_zero", True):
            body = digest_body()
            if late_by > 3600:
                body += ("\n\n<i>Posted %d hours late — this Mac was asleep at %s ET.</i>"
                         % (late_by // 3600, hhmm))
            for target, thread in digest_targets(cfg):
                C.tg_send(body, chat_id=target, cfg=cfg, thread_id=thread)
        state["digests_sent"] = ([key] + state["digests_sent"])[:20]
        save_state(state)


# --------------------------------------------------------------------------- main loop

def run():
    cfg = C.tg_config()
    state = load_state()
    allowed = allowed_chats(cfg)
    warned = set()
    redirected = set()
    me = C.tg_call("getMe") or {}
    me_username = ((me.get("result") or {}).get("username") or "").lower()
    print("[bot] I am @%s" % me_username, flush=True)

    # Only /dials is advertised in the Telegram menu — the rest (/pace, /rep,
    # /week, /leaderboard …) still work if typed, they just stay out of the way.
    C.tg_call("setMyCommands", {"commands": [
        {"command": "dials", "description": "Dials across the BDR team"},
    ]}, cfg=cfg)

    def syncer():
        # Trellus syncs (~10s, far longer when the network blips) run on their
        # own thread so /dials never waits behind one.
        last_today_sync = last_deep_sync = 0.0
        while True:
            try:
                now = time.time()
                if now - last_deep_sync > SYNC_DEEP_EVERY:
                    C.sync(days=C.CFG.get("sync_recent_days", 2))
                    last_deep_sync = last_today_sync = now
                elif now - last_today_sync > SYNC_TODAY_EVERY:
                    C.sync(days=1)
                    last_today_sync = now
            except Exception:
                traceback.print_exc()
            time.sleep(15)

    import threading
    threading.Thread(target=syncer, name="syncer", daemon=True).start()
    print("[bot] started at %s" % datetime.now(C.TZ).isoformat(), flush=True)

    # Actions runs are capped at 6h, so a run exits cleanly after RUN_SECONDS
    # and the workflow starts its successor. Unset = run forever (Mac).
    deadline = time.time() + int(os.environ.get("RUN_SECONDS") or 0) or None
    while True:
        if deadline and time.time() > deadline:
            save_state(state)
            print("[bot] RUN_SECONDS reached; exiting for the next run", flush=True)
            return
        try:
            maybe_digest(state, cfg)

            upd = C.tg_call("getUpdates", {"offset": state["offset"], "timeout": 25,
                                           "allowed_updates": ["message"]},
                            cfg=cfg, timeout=40)
            if not upd or not upd.get("ok"):
                time.sleep(5)
                continue

            for u in upd.get("result", []):
                state["offset"] = u["update_id"] + 1
                msg = u.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = msg.get("text", "")
                # Forum groups tag each message with the topic it belongs to.
                thread_id = msg.get("message_thread_id")
                if not chat_id or not text.startswith("/"):
                    continue
                # Log every command that reaches the bot, so "it is not
                # responding" can be told apart from "it never arrived".
                print("[cmd] chat=%s thread=%s text=%r" % (chat_id, thread_id, text[:40]),
                      flush=True)
                # Owner-only chat administration, handled here because it needs
                # the live `allowed` set rather than just message text.
                # Telegram privacy mode still delivers EVERY slash command in a
                # group to EVERY bot in it, so "/daily@corgiventuresbot" lands
                # here too. Anything addressed to another bot is not ours.
                head = text.split()[0]
                if "@" in head and head.split("@", 1)[1].lower() != me_username:
                    print("[cmd] not for me: %s" % head, flush=True)
                    continue
                cmd0 = head.split("@")[0].lower()
                if chat_id == str(cfg["chat_id"]) and cmd0 in ("/allow", "/deny", "/allowed"):
                    parts = text.split()
                    if cmd0 == "/allowed":
                        C.tg_send("Chats allowed to use the bot:\n" +
                                  "\n".join("• <code>%s</code>" % c for c in sorted(allowed)),
                                  chat_id=chat_id, cfg=cfg)
                    elif len(parts) < 2:
                        C.tg_send("Usage: <code>%s &lt;chat_id&gt;</code>" % cmd0,
                                  chat_id=chat_id, cfg=cfg)
                    elif cmd0 == "/allow":
                        persist_allowed(cfg, parts[1])
                        allowed = allowed_chats(cfg)
                        C.tg_send("✅ Chat <code>%s</code> can now use the bot." % parts[1],
                                  chat_id=chat_id, cfg=cfg)
                    else:
                        forget_allowed(cfg, parts[1])
                        allowed = allowed_chats(cfg)
                        warned.discard(parts[1])
                        C.tg_send("🚫 Chat <code>%s</code> removed." % parts[1],
                                  chat_id=chat_id, cfg=cfg)
                    save_state(state)
                    continue

                # Pin the bot to the topic this was sent from.
                if cmd0 == "/here" and str(chat_id).startswith("-") and "@" not in head:
                    # Every bot in a group receives a bare /here, so they would
                    # all pin to the same topic. Make the sender say which bot.
                    C.tg_send("In a group, address me directly: "
                              "<code>/here@%s</code>" % me_username,
                              chat_id=chat_id, cfg=cfg, thread_id=thread_id)
                    continue
                if cmd0 == "/here" and chat_id in allowed:
                    home = set_home(cfg, chat_id, thread_id)
                    allowed = allowed_chats(cfg)
                    where = ("this topic" if home.get("thread_id") else "this chat")
                    C.tg_send("📌 Pinned to %s. The daily digest posts here, and I'll "
                              "stay quiet everywhere else." % where,
                              chat_id=chat_id, cfg=cfg, thread_id=thread_id)
                    continue

                if off_home(cfg, chat_id, thread_id):
                    home = cfg.get("home") or {}
                    print("[cmd] ignored: outside the pinned topic %s/%s"
                          % (home.get("chat_id"), home.get("thread_id")), flush=True)
                    # Say so once per chat rather than vanishing.
                    if chat_id not in redirected:
                        redirected.add(chat_id)
                        C.tg_call("sendMessage",
                                  {"chat_id": chat_id,
                                   "message_thread_id": thread_id,
                                   "text": "I only answer in the pinned Dials topic. "
                                           "Send /here from the topic you want me in."},
                                  cfg=cfg)
                    continue

                if chat_id not in allowed:
                    # Don't leak team numbers; tell the owner so they can approve.
                    if chat_id not in warned:
                        warned.add(chat_id)
                        title = (msg.get("chat") or {}).get("title") or "private chat"
                        C.tg_send("🔒 <b>%s</b> (id <code>%s</code>) tried to use the dial bot.\n\n"
                                  "Reply <code>/allow %s</code> to let it in."
                                  % (title, chat_id, chat_id), cfg=cfg)
                    C.tg_call("sendMessage", {"chat_id": chat_id,
                              "text": "This bot is restricted."}, cfg=cfg)
                    continue
                try:
                    # A sync can take ~30s; acknowledge so the chat isn't silent.
                    if text.split()[0].split("@")[0].lower() == "/sync":
                        C.tg_send("⏳ Pulling the latest calls from Trellus…",
                                  chat_id=chat_id, cfg=cfg, thread_id=thread_id)
                    reply = handle(text)
                except Exception:
                    traceback.print_exc()
                    reply = "⚠️ Error handling that command — check the log."
                if reply:
                    C.tg_send(reply, chat_id=chat_id, cfg=cfg, thread_id=thread_id)
            save_state(state)
        except KeyboardInterrupt:
            print("[bot] stopping"); return
        except Exception:
            traceback.print_exc()
            time.sleep(10)


def main():
    if "--sync" in sys.argv:
        i = sys.argv.index("--sync")
        days = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else None
        n = C.sync(days=days, verbose=True)
        print("synced %d records" % n)
        return
    if "--digest" in sys.argv:
        C.sync(days=1)
        cfg = C.tg_config()
        body = digest_body()
        for target, thread in digest_targets(cfg):
            C.tg_send(body, chat_id=target, cfg=cfg, thread_id=thread)
            print("digest sent to %s%s" % (target, " topic %s" % thread if thread else ""))
        return
    if "--print" in sys.argv:
        i = sys.argv.index("--print")
        cmd = " ".join(sys.argv[i + 1:]) or "/today"
        if not cmd.startswith("/"):
            cmd = "/" + cmd
        out = handle(cmd) or ""
        print(out.replace("<b>", "").replace("</b>", "").replace("<pre>", "")
                 .replace("</pre>", "").replace("<code>", "").replace("</code>", ""))
        return
    run()


if __name__ == "__main__":
    main()
