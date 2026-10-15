#!/usr/bin/env python3
"""
One-time Telegram setup for the dial bot.

  python3 setup_bot.py <BOT_TOKEN>     use a dedicated bot (recommended)
  python3 setup_bot.py --reuse         reuse the existing deal-notifier bot

For a dedicated bot: talk to @BotFather → /newbot → copy the token → send your
new bot any message in Telegram → run this with the token.
"""
import json, os, sys, urllib.error, urllib.request

OUT = os.path.expanduser("~/.telegram_dials_bot")
REUSE = os.path.expanduser("~/.telegram_hubspot_bot")


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def post(tok, method, payload):
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (tok, method),
                                 data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    if sys.argv[1] == "--reuse":
        if not os.path.exists(REUSE):
            print("No existing bot config at %s" % REUSE)
            raise SystemExit(1)
        cfg = json.load(open(REUSE))
        json.dump({"bot_token": cfg["bot_token"], "chat_id": cfg["chat_id"], "allowed_chats": []},
                  open(OUT, "w"))
        os.chmod(OUT, 0o600)
        print("Wrote %s (reusing the deal-notifier bot)." % OUT)
        tok, chat = cfg["bot_token"], cfg["chat_id"]
    else:
        tok = sys.argv[1].strip()
        try:
            me = get("https://api.telegram.org/bot%s/getMe" % tok)
        except urllib.error.HTTPError as e:
            me = {"ok": False} if e.code in (401, 404) else None
            if me is None:
                print("Telegram returned HTTP %s. Try again in a moment." % e.code)
                raise SystemExit(1)
        except Exception as e:
            print("Could not reach Telegram (%s). Check your connection and retry." % e)
            raise SystemExit(1)
        if not me.get("ok"):
            print("That token was rejected by Telegram. Copy the whole string "
                  "BotFather sent, including the digits before the colon.")
            raise SystemExit(1)
        print("Bot: @%s" % me["result"].get("username"))

        upd = get("https://api.telegram.org/bot%s/getUpdates" % tok)
        if not upd.get("ok"):
            print("Bad token or API error:", upd)
            raise SystemExit(1)
        chats = {}
        for u in upd.get("result", []):
            msg = u.get("message") or u.get("channel_post") or {}
            c = msg.get("chat", {})
            if c.get("id"):
                chats[c["id"]] = c.get("title") or c.get("username") or c.get("first_name") or "chat"
        if not chats:
            print("No messages found. Open Telegram, send your new bot ANY message, then re-run.")
            raise SystemExit(1)
        chat = list(chats.keys())[-1]
        print("Using chat: %s (id=%s)" % (chats[chat], chat))
        json.dump({"bot_token": tok, "chat_id": str(chat), "allowed_chats": []}, open(OUT, "w"))
        os.chmod(OUT, 0o600)
        print("Wrote %s" % OUT)

    post(tok, "sendMessage", {"chat_id": chat,
         "text": "📞 Dial tracker connected. Send /help to see what it can do."})
    print("Test ping sent — check Telegram.")


if __name__ == "__main__":
    main()
