"""Pip: plain rules for dates, jobs, money and "what's on", the family's AI for the rest, and Pip's Telegram account.

Pip answers the same way on the home screen ("Ask Pip") and on Telegram. The rules come first (exact, fast, free):
  "Add the dentist on Thursday at 4"   -> an event; Pip asks who it is for
  "Remind Oskar to pack his jacket"    -> a job for Oskar
  "What's on today?"                   -> today's list
  "sign in" (Telegram only)            -> a one-time sign-in link for this box, only ever in a private chat
Anything else, and questions, go to the family's AI (ai.py) when it has one: its tools turn into sentences the rules
understand, so AI changes take the same path as typed ones, and the ones that take something away ask first. With no AI,
over the month's cap, or while the AI is not switched on, Pip stays with the rules and says so at most once a day.
A photo of a letter becomes one suggestion with its dates ("Add all 3"); the photo is not kept.
Telegram: the family's own bot (its code from BotFather), long polling, no webhook, so the box needs no public port.
Private chats join through the family's own link; a group joins only through the home screen's "Add to your family
group" link, added by someone who already said YES. Every person says YES once, privately; STOP forgets that person.
In a group Pip answers only when @mentioned, on /pip, or in reply to itself (BotFather's privacy mode stays on).
Each person who said YES gets a morning message at their own time (default 07:00, off for children).

Environment: FAMBOX_TELEGRAM=dry-run checks the code's shape only and never calls Telegram (demo box);
FAMBOX_TELEGRAM_API points at a stand-in Telegram for the tests.
"""
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

import ai
import money
import welcome

API = os.environ.get("FAMBOX_TELEGRAM_API", "https://api.telegram.org")
DRY = os.environ.get("FAMBOX_TELEGRAM") == "dry-run"
CODE_SHAPE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")  # from the design's prototype
seen = {"at": 0.0, "problem": None, "started": time.time()}  # Pip's last good contact with Telegram

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
HELP = ("I work with simple rules for now, so I understand dates (\"Add swimming on Friday at 5\"), "
        "jobs (\"Remind Oskar to pack his jacket\"), \"What's on today?\", money (\"40 groceries\", \"paid electricity\", "
        "\"put 20 in the box for Lapland\", \"salary 3200\"; or send me your bank statement file), "
        "chores with points (\"Tidy the shoes is worth 10 points\"), a dream's journal (\"Note for Lapland: found cheap flights\"), "
        "dreams (\"We want to go to Lapland next winter\"), countdowns (\"Count down to Oskar's birthday on 15 October\") "
        "and the table itself (\"Make the calendar bigger\").")
HELP_END = " Once an AI is connected on your box, I'll understand anything you write."
SEASONS = {"winter": 2, "spring": 4, "summer": 7, "autumn": 10, "fall": 10}
BLOCK_WORDS = {"clock": "clock", "time": "clock", "calendar": "today", "today": "today", "plan": "today", "agent": "pip",
               "jobs": "tasks", "job": "tasks", "tasks": "tasks", "chores": "tasks", "dreams": "dreams", "dream": "dreams",
               "countdowns": "countdowns", "countdown": "countdowns", "money": "money", "budget": "money",
               "routines": "routines", "routine": "routines", "weather": "weather", "photos": "photos", "pictures": "photos",
               "meals": "meals", "dinners": "meals", "shopping": "meals", "notes": "notes", "day": "day", "week": "day",
               "needs": "needs", "questions": "needs"}
SIZE_WORDS = {"bigger": +1, "larger": +1, "smaller": -1, "small": "S", "medium": "M", "large": "L", "big": "L"}
NUM = r"€?\s?(\d[\d.,]*)\s?(?:€|euros?|eur)?"


def db():
    return psycopg.connect(os.environ.get("FAMBOX_DB", "dbname=builder"), autocommit=True, row_factory=dict_row)


# ---------- Telegram ----------

class TelegramError(Exception):
    def __init__(self, code, what=""):
        super().__init__(f"{code} {what}")
        self.code = code


def tg(token, method, wait=10, **params):
    """One call to Telegram's bot API. The code is only ever in the address, never logged."""
    req = urllib.request.Request(f"{API}/bot{token}/{method}", data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=wait) as r:
            return json.load(r)["result"]
    except urllib.error.HTTPError as ex:
        try:
            what = json.load(ex).get("description", "")
        except Exception:
            what = ""
        raise TelegramError(ex.code, what) from None
    except (urllib.error.URLError, TimeoutError, OSError) as ex:
        raise TelegramError(0, type(ex).__name__) from None


def check_code(code, slug, agent):
    """The family pasted their bot's code. Returns (username, None) or (None, problem)."""
    if not CODE_SHAPE.match(code):
        return None, "short"
    if DRY:
        return re.sub(r"[^a-z0-9_]", "", f"{slug}_{agent}".lower().replace("-", "_"))[:28] + "_bot", None
    try:
        me = tg(code, "getMe")
        if tg(code, "getWebhookInfo").get("url"):
            return None, "in_use"  # another program already receives this bot's messages
    except TelegramError as ex:
        return None, "unknown" if ex.code in (401, 404) else "offline"
    return me.get("username") or None, None


def offline():
    """True when a connected Pip has not reached Telegram for two minutes (or Telegram refused it)."""
    if DRY:
        return False
    return bool(seen["problem"]) or time.time() - max(seen["at"], seen["started"]) > 120


def run():
    """Pip's Telegram loop: ask Telegram for new messages, answer them, repeat. Runs as a thread."""
    token, offset = None, None
    while True:
        try:
            with db() as c:
                row = c.execute("select bot_token from family.household").fetchone()
            if not row or not row["bot_token"]:
                token, seen["problem"] = None, None
                time.sleep(5)
                continue
            if row["bot_token"] != token:
                token, offset = row["bot_token"], None
            updates = tg(token, "getUpdates", wait=35, timeout=25, offset=offset,
                         allowed_updates=["message", "my_chat_member", "callback_query"])
            seen.update(at=time.time(), problem=None)
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    if u.get("callback_query"):
                        tap(token, u["callback_query"])
                    elif u.get("my_chat_member"):
                        membership(u["my_chat_member"])
                    else:
                        handle(token, u.get("message") or {})
                except Exception as ex:  # one bad message never stops Pip
                    print(f"pip: could not answer a message ({type(ex).__name__}: {ex})", file=sys.stderr)
        except TelegramError as ex:
            seen["problem"] = {401: "unknown", 404: "unknown", 409: "in_use"}.get(ex.code, "offline")
            print(f"pip: Telegram said {ex.code}", file=sys.stderr)
            time.sleep(30 if ex.code in (401, 404, 409) else 10)
        except Exception as ex:
            print(f"pip: {type(ex).__name__}: {ex}", file=sys.stderr)
            time.sleep(10)


def send(token, chat, a):
    """One answer: plain text, or {"text", "reply_markup"} with buttons."""
    a = a if isinstance(a, dict) else {"text": a}
    tg(token, "sendMessage", chat_id=chat, link_preview_options={"is_disabled": True}, **a)


def said_yes(c, user, name):
    """This person said YES to Pip (privately): their messages may be read, in their chat and in the family group."""
    who = c.execute("select id, role from family.people where lower(name) = lower(%s) order by id limit 1", (name or "",)).fetchone()
    c.execute("insert into family.members (tg_user, name, person_id, consent_at, morning) values (%s, %s, %s, now(), %s)"
              " on conflict (tg_user) do update set name = excluded.name, person_id = excluded.person_id, consent_at = now(),"
              " morning = case when family.members.consent_at is null then excluded.morning else family.members.morning end",
              (user, name, who and who["id"], None if who and who["role"] == "child" else "07:00"))


def forget(c, user):
    """STOP: this one person is forgotten: their YES, their private chat, what they wrote and Pip's answers to them."""
    c.execute("delete from family.members where tg_user = %s", (user,))
    c.execute("delete from family.chats where chat_id = %s", (user,))
    c.execute("delete from family.messages where via = 'telegram' and (sender = %s or chat_id = %s)", (user, user))
    c.execute("delete from family.pending where sender = %s or chat_id = %s", (user, user))


def handle(token, msg):
    ch = msg.get("chat") or {}
    chat, kind = ch.get("id"), ch.get("type")
    if chat and kind in ("group", "supergroup"):
        return group(token, msg)
    if not chat or kind != "private":
        return
    text = (msg.get("text") or msg.get("caption") or "").strip()
    with db() as c:
        member = c.execute("select consent_at from family.chats where chat_id = %s", (chat,)).fetchone()
        fam = c.execute("select * from family.household").fetchone()
        name = (msg.get("from") or {}).get("first_name") or ""
        hello = (f"Hello{(' ' + name) if name else ''}! I'm {fam['agent_name']}, the {fam['name']} family's "
                 f"helper. Tell me a date and I'll put it on your table, for example \"Add the dentist on Thursday at 4\".")
        if text.startswith("/start"):
            code = text[6:].strip()
            if member and member["consent_at"]:
                answer = f"Hello again! I'm {fam['agent_name']}. " + HELP
            elif member:
                answer = ASK_CONSENT
            elif (code and welcome.redeem(c, code, "join")) or \
                    c.execute("select 1 from family.members m join family.chats g on g.kind = 'group' where m.tg_user = %s",
                              (chat,)).fetchone():  # met in the family's group: they may open a private chat to say YES
                c.execute("insert into family.chats (chat_id, name) values (%s, %s) on conflict do nothing", (chat, name))
                answer = ASK_CONSENT
            else:
                answer = None
            if answer:
                save(c, "telegram", chat, False, text.split()[0], chat)
                save(c, "telegram", chat, True, answer, chat)
                return send(token, chat, answer)
        if not member:  # a stranger found the bot: polite, and nothing about the family
            return send(token, chat, "Hello! I only talk to the family I belong to. Open me from your family's home screen to join.")
        if text.lower().strip(" .!") == "stop":  # consent withdrawn: the person is forgotten (privacy policy section 5)
            forget(c, chat)
            return send(token, chat, "Done: I've forgotten this chat and what you wrote to me here. "
                        "Your family's box keeps working. To come back, open me again from the family's home screen.")
        if not member["consent_at"]:  # nothing is read or kept until this person said yes (privacy policy section 5)
            if text.lower().strip(" .!") in ("yes", "jah", "да", "так"):
                c.execute("update family.chats set consent_at = now() where chat_id = %s", (chat,))
                said_yes(c, chat, name)
                save(c, "telegram", chat, True, hello, chat)
                return send(token, chat, hello)
            return send(token, chat, ASK_CONSENT)
        answers = content(c, token, msg, text, chat, chat)
    for a in answers:
        send(token, chat, a)


def content(c, token, msg, text, chat, sender):
    """A message from someone who said YES: a statement file, a photo, or words."""
    doc = msg.get("document") or {}
    if doc and re.search(r"\.(csv|xml|txt|json)$", doc.get("file_name") or "", re.I):  # a bank statement file
        last = c.execute("select coalesce(max(id), 0) n from family.bank_lines").fetchone()["n"]
        said = statement_from_telegram(c, token, doc)
        if said.startswith("I read") and "Reply YES" in said:  # YES accepts this file's lines only
            ids = [r["id"] for r in c.execute("select id from family.bank_lines where id > %s and state = 'new'", (last,)).fetchall()]
            set_pending(c, (chat, sender), {"statement": ids})
        return [save_reply(c, "telegram", chat, f"(a file: {doc.get('file_name')})", said, sender)]
    if msg.get("photo") and text:
        got = photo_for_journal(c, token, msg, text)
        if got:
            return [save_reply(c, "telegram", chat, text, got, sender)]
    if msg.get("photo") and ai.setting(c):  # a letter from school: Pip finds the dates in it
        data = fetch_photo(token, msg)
        if data is None:
            return [save_reply(c, "telegram", chat, "(a photo)", "I couldn't fetch that photo from Telegram. Please send it again in a minute.", sender)]
        said, sid = letter(c, data)
        save_reply(c, "telegram", chat, "(a photo of a letter)", said, sender)
        return [letter_buttons(said, sid, c)]
    if doc or msg.get("photo"):
        return [save_reply(c, "telegram", chat, text or "(a photo)", PHOTO if msg.get("photo") or not doc else FILES, sender)]
    return reply(c, text, via="telegram", chat=chat, sender=sender)


def addressed(msg, token, username):
    """Is this group message for Pip (@mentioned, /pip, or a reply to Pip)? Returns its words without the address, or None."""
    text = (msg.get("text") or msg.get("caption") or "").strip()
    at = "@" + (username or "").lower()
    to_pip = ((msg.get("reply_to_message") or {}).get("from") or {}).get("id") == int(token.split(":")[0])
    m = re.match(r"^/pip(?:@\w+)?\b\s*", text, re.I)
    if m:
        return text[m.end():].strip()
    if username and at in text.lower():
        i = text.lower().index(at)
        return (text[:i] + text[i + len(at):]).strip(" ,:")
    return text if to_pip else None


def group(token, msg):
    """The family's Telegram group: Pip answers only when addressed, and only people who said YES to it privately."""
    chat = msg["chat"]["id"]
    who = msg.get("from") or {}
    sender, name = who.get("id"), who.get("first_name") or ""
    with db() as c:
        moved = msg.get("migrate_to_chat_id") and (chat, msg["migrate_to_chat_id"]) or \
            msg.get("migrate_from_chat_id") and (msg["migrate_from_chat_id"], chat)
        if moved:  # the group became a supergroup: it has a new id
            old, new = moved
            if c.execute("select 1 from family.chats where chat_id = %s", (old,)).fetchone():
                c.execute("delete from family.chats where chat_id = %s", (new,))
                c.execute("update family.chats set chat_id = %s where chat_id = %s", (new, old))
                c.execute("update family.messages set chat_id = %s where chat_id = %s", (new, old))
                c.execute("update family.pending set chat_id = %s where chat_id = %s", (new, old))
            return
        if not sender or who.get("is_bot"):
            return
        fam = c.execute("select * from family.household").fetchone()
        a, bot = fam["agent_name"], fam["bot_username"]
        joined = c.execute("select 1 from family.chats where chat_id = %s and kind = 'group'", (chat,)).fetchone()
        person = c.execute("select consent_at from family.members where tg_user = %s", (sender,)).fetchone()
        yes = bool(person and person["consent_at"])
        raw = (msg.get("text") or "").strip()
        if not joined:
            start = re.match(r"^/start(?:@\w+)?\s+(\S+)", raw)
            if start and yes and welcome.redeem(c, start[1], "join"):
                c.execute("insert into family.chats (chat_id, name, kind, consent_at) values (%s, %s, 'group', now())"
                          " on conflict (chat_id) do update set kind = 'group'", (chat, msg["chat"].get("title") or "family group"))
                return send(token, chat, f"Hello, {fam['name']} family! I'm {a}. In this group I answer when you write @{bot}, "
                            f"start with /pip, or reply to me. I only read people who said YES to me in a private chat.")
            if start or addressed(msg, token, bot) is not None:
                send(token, chat, f"I join a family group only from the family's home screen (\"Add {a} to your family group\"), "
                     f"added by someone who already talks to me.")
                return tg(token, "leaveChat", chat_id=chat)
            return
        text = addressed(msg, token, bot)
        if text is None:
            return  # not for Pip: not read, not kept
        if not yes:
            if not person:
                c.execute("insert into family.members (tg_user, name) values (%s, %s) on conflict do nothing", (sender, name))
            return send(token, chat, f"Hello {name}! Before I read what you write, please say YES to me privately: "
                        f"open @{bot}, tap Start, and I'll explain. Until then I don't read or keep your messages.")
        low = text.lower().strip(" .!")
        if low == "stop":
            forget(c, sender)
            return send(token, chat, f"Done, {name}: I've forgotten you and what you wrote to me. "
                        f"The others can still talk to me here. To come back, say YES to me privately.")
        if re.search(r"\b(sign ?in|log ?in)\b", low) and len(low) < 40:
            private = c.execute("select 1 from family.chats where chat_id = %s and consent_at is not null", (sender,)).fetchone()
            if not private:
                return send(token, chat, f"Sign-in links only go in a private chat. Open @{bot}, tap Start, then ask me there.")
            link = welcome.link_for(c, hours=1)
            send(token, sender, f"Here is your sign-in link. It works once, within an hour: {link}" if link
                 else "I can't make links on this box yet. Ask whoever set up your box.")
            save(c, "telegram", chat, False, text, sender)
            return send(token, chat, f"{name}, I've sent you a sign-in link privately.")
        answers = content(c, token, msg, text, chat, sender)
    for x in answers:
        send(token, chat, x)


def membership(upd):
    """Pip was added to a group, removed from it, or the group changed. Joining needs the home screen's link (group())."""
    chat = (upd.get("chat") or {}).get("id")
    if chat and (upd.get("new_chat_member") or {}).get("status") in ("left", "kicked"):
        with db() as c:
            c.execute("delete from family.chats where chat_id = %s and kind = 'group'", (chat,))
            c.execute("delete from family.pending where chat_id = %s", (chat,))


def tap(token, q):
    """A button under one of Pip's messages: "Add all 3" for the dates in a letter."""
    user, data = (q.get("from") or {}).get("id"), q.get("data") or ""
    m = re.fullmatch(r"add:(\d+)", data)
    with db() as c, c.transaction():
        yes = c.execute("select 1 from family.members where tg_user = %s and consent_at is not null", (user,)).fetchone()
        if not yes or not m:
            return tg(token, "answerCallbackQuery", callback_query_id=q["id"], text="Say YES to me in a private chat first.")
        import dashboard as d
        sg = c.execute("select items from family.suggestions where id = %s and state = 'pending'", (int(m[1]),)).fetchone()
        if sg:
            d.accept(c, int(m[1]), ZoneInfo(c.execute("select tz from family.household").fetchone()["tz"]))
    words = f"Added {len(sg['items'])} to your calendar. Undo on the home screen." if sg else "That's already decided on the home screen."
    tg(token, "answerCallbackQuery", callback_query_id=q["id"], text=words)
    msg = q.get("message") or {}
    if msg.get("chat"):
        tg(token, "editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
           text=(msg.get("text") or "") + "\n\n" + words)


ASK_CONSENT = ("Before we start: what you write to me, and my answers, pass through Telegram. Telegram is a company "
               "outside the EU with no EU data protection agreement, so EU data protection cannot be guaranteed there. "
               "Reply YES if that's fine (you can send STOP at any time later). If not, just leave it: your family's box does "
               "everything I do, without Telegram. Details: https://fam.box/legal/privacy")


# ---------- the rules ----------

PHOTO = ("I can't read photos yet: that needs an AI connected on your box. Tell me the date in words "
         "(\"Photo day on 23 October\") and I'll add it. A photo for a dream's journal: write \"Note for Lapland: ...\" under it.")
FILES = ("I can read bank statements saved as CSV (LHV: Statement, then CSV; Revolut: Statement, then Excel or CSV). "
         "PDFs I can't read yet.")


def save(c, via, chat, from_pip, text, sender=None):
    c.execute("insert into family.messages (via, chat_id, from_pip, text, sender) values (%s, %s, %s, %s, %s)",
              (via, chat, from_pip, text[:2000], sender))


def save_reply(c, via, chat, said, answer, sender=None):
    save(c, via, chat, False, said, sender)
    save(c, via, chat, True, re.sub(r"https?://\S+/signin\?k=\S+", "(a one-time sign-in link)", answer), sender)  # never kept
    return answer


def get_pending(c, key):
    """Pip's open question to one person in one chat (key = (chat, sender); the home screen is (0, 0))."""
    r = c.execute("select data from family.pending where chat_id = %s and sender = %s", key).fetchone()
    return r and r["data"]


def set_pending(c, key, data):
    if data is None:
        return c.execute("delete from family.pending where chat_id = %s and sender = %s", key)
    c.execute("insert into family.pending (chat_id, sender, data) values (%s, %s, %s) on conflict (chat_id, sender)"
              " do update set data = excluded.data", key + (json.dumps(dict(data, at=time.time())),))


def reply(c, text, via="web", chat=None, sender=None):
    """Pip's answer(s) to one message, saved with the message. `chat` is the Telegram chat, None on the web;
    `sender` the Telegram person (in a private chat the same as `chat`)."""
    text = text.strip()[:500]
    if not text:
        return []
    key = (chat, sender if sender is not None else chat) if chat is not None else (0, 0)  # the home screen: one conversation
    fam = c.execute("select * from family.household").fetchone()
    tz = ZoneInfo(fam["tz"])
    now = datetime.now(tz)
    people = c.execute("select id, name, colour, role from family.people order by sort, id").fetchall()
    low = text.lower().strip(" .!?")
    say = lambda words: [save_reply(c, via, chat, text, words, sender)]
    m = re.fullmatch(r"(?:my )?morning(?: message)?(?: at)?\s+(off|on|stop|(\d{1,2})(?:[:.](\d{2}))?\s*(am)?)", low)
    if m and via == "telegram" and sender:  # "morning off", "morning 6:30"
        h, mi = (int(m[2]), int(m[3] or 0)) if m[2] else (7, 0)
        if m[1] in ("off", "stop"):
            c.execute("update family.members set morning = null where tg_user = %s", (sender,))
            return say("Done: no morning message for you. Say \"morning 7:00\" to have it again.")
        if h > 23 or mi > 59:
            return say("Tell me a time like \"morning 6:30\".")
        c.execute("update family.members set morning = %s where tg_user = %s", (f"{h:02d}:{mi:02d}", sender))
        return say(f"Done: your morning message comes at {h:02d}:{mi:02d}, in our private chat.")
    pending = get_pending(c, key)
    if pending and pending.get("dream"):
        set_pending(c, key, None)
        got = saving_goal(c, pending["dream"], text, now) if time.time() - pending.get("at", 0) < 600 else None
        if got:
            return say(got)
    elif pending and (pending.get("payday") or pending.get("statement") or pending.get("ai")) and (low in YES or low in NO) \
            and time.time() - pending.get("at", 0) < 2 * 86400:
        set_pending(c, key, None)
        if pending.get("ai"):  # changes the AI proposed that ask first
            if low in NO:
                return say("Fine, I've left it as it is.")
            return say(" ".join(rules(c, fam, people, x, x.lower().strip(" .!?"), now, via, key, True, False) or
                                f"I couldn't do \"{x}\"." for x in pending["ai"]))
        return say(yes_no(c, pending, low in YES, tz))
    elif pending and pending.get("kakeibo") and time.time() - pending.get("at", 0) < 2 * 86400 and kakeibo_answer(c, text, now):
        set_pending(c, key, None)
        c.execute("update family.kakeibo set answer = concat_ws(E'\\n', answer, %s::text), answered_at = now() where month = %s",
                  (text[:2000], pending["kakeibo"]))
        month_ = date.fromisoformat(pending["kakeibo"])
        return say(f"Thank you. It's written down with {month_:%B}'s numbers, in Money.")
    elif pending and pending.get("event"):
        set_pending(c, key, None)
        who = next((p for p in people if p["name"].lower() == low), None)
        if who or low in ("everyone", "all", "all of us", "us", "the family", "nobody"):
            c.execute("update family.events set person_id = %s where id = %s", (who and who["id"], pending.get("event")))
            said = (f"Done, it's in {who['name']}'s colour. It's on your table now." if who
                    else "Done, it's for everyone. It's on your table now.")
            return say(said)
    return say(answer(c, fam, people, text, low, now, via, key))


def answer(c, fam, people, text, low, now, via, key):
    """The rules first (exact, fast, free); the family's AI for anything they don't understand and for questions."""
    on = bool(ai.setting(c))
    question = on and (text.rstrip().endswith("?") or re.match(r"(when|where|who|what|which|why|how|is|are|do|does|did|can|could|should|will)\b", low))
    said = rules(c, fam, people, text, low, now, via, key, ai_on=on, dates=not question)
    if said:
        return said
    why = "no_ai"
    if on:
        said, why = ai_turn(c, fam, people, text, now, via, key)
        if said:
            return said
    if question:  # the AI could not help: the date rule may still
        said = rules(c, fam, people, text, low, now, via, key, ai_on=False, dates=True)
        if said:
            return said
    return HELP + ai_note(c, why, now.date())


def rules(c, fam, people, text, low, now, via, key, dates=True, ai_on=False):
    if re.match(r"^(hello|hi|hey|tere|hei|good (morning|afternoon|evening)|привет|здравствуй)\b", low):
        return (f"Hello, {fam['name']} family! I'm {fam['agent_name']}. Tell me a date and I'll put it on your "
                f"table, for example \"Add the dentist on Thursday at 4\".")
    if "photo" in low and ("letter" in low or "here" in low):
        return "Send me the photo and I'll find the dates in it." if ai_on else PHOTO
    if re.search(r"\b(sign ?in|log ?in|link)\b", low) and len(low) < 40:
        if via != "telegram":
            return "You're already signed in here."
        if key[0] != key[1]:  # a group: links only ever go privately
            return "Sign-in links only go in a private chat: ask me there."
        link = welcome.link_for(c, hours=1)
        return (f"Here is your sign-in link. It works once, within an hour: {link}" if link
                else "I can't make links on this box yet. Ask whoever set up your box.")
    if re.search(r"\b(what'?s on|what is on|today'?s plan|plans? today|what do we have)\b", low):
        tz = now.tzinfo
        start = datetime.combine(now.date(), datetime.min.time(), tz)
        rows = c.execute("select e.title, e.starts_at, e.all_day, p.name from family.events e left join family.people p"
                         " on p.id = e.person_id where e.starts_at >= %s and e.starts_at < %s order by e.all_day desc,"
                         " e.starts_at", (start, start + timedelta(days=1))).fetchall()
        if not rows:
            return "Nothing planned today."
        return "Today:\n" + "\n".join(
            f"{'All day' if r['all_day'] else r['starts_at'].astimezone(tz).strftime('%H:%M')}  {r['title']}"
            + (f" ({r['name']})" if r["name"] else "") for r in rows)
    said = more_rules(c, fam, people, text, low, now, key)
    if said:
        return said
    m = re.match(r"^remind (\w+) to (.+?)(?:\s+for\s+(\d{1,4})\s+points?)?$", text.strip(" .!"), re.I)
    if m:
        who = next((p for p in people if p["name"].lower() == m.group(1).lower()), None)
        job = m.group(2).strip()
        job = job[0].upper() + job[1:]
        pts = min(1000, int(m[3])) if m[3] and int(m[3]) else None
        c.execute("insert into family.tasks (title, person_id, points) values (%s, %s, %s)", (job, who and who["id"], pts))
        worth = f" It's worth {pts} points." if pts else ""
        return f"Done: {who['name'] if who else 'a new job'}: \"{job}\". It's on your table now.{worth}"
    when = dates and parse_when(text, now)
    if when:
        d, t, rest = when
        who, rest = pick_person(rest, people)
        title = re.sub(r"^(please\s+)?(add|put|book|schedule|plan|we have|there'?s|count ?down to)\s+", "", rest.strip(" .,!?"), flags=re.I)
        title = re.sub(r"\s+(is|are)$", "", title.strip(" .,!?"), flags=re.I)
        title = re.sub(r"^(the|a|an|our)\s+", "", title, flags=re.I).strip(" .,!?") or "Something"
        tz = now.tzinfo
        start = datetime.combine(d, t or datetime.min.time(), tz)
        cd = bool(re.search(r"\b(birthday|anniversary|holiday|trip)\b", title, re.I)) or bool(re.match(r"count ?down to", low))
        ev = c.execute("insert into family.events (title, starts_at, all_day, person_id, countdown) values (%s, %s, %s, %s, %s)"
                       " returning id", (title[0].upper() + title[1:], start, t is None, who and who["id"], cd)).fetchone()
        day = d.strftime("%A") if 0 <= (d - now.date()).days < 7 else f"{d.strftime('%a')} {d.day} {d.strftime('%B')}"
        at = f"{day} at {t.strftime('%H:%M')}" if t else f"{day}, all day"
        if who:
            return f"Added: {title} for {who['name']}, {at}. It's on your table now, in {who['name']}'s colour."
        if len(people) > 1:
            set_pending(c, key, {"event": ev["id"]})
            return f"Added: {title}, {at}. Who is it for?"
        return f"Added: {title}, {at}. It's on your table now."
    return None


def pick_person(text, people):
    for p in people:
        m = re.search(rf"\bfor {re.escape(p['name'])}\b", text, re.I)
        if m:
            return p, text[:m.start()] + text[m.end():]
    return None, text


def parse_when(text, now):
    """Finds a day (and maybe a time) in a sentence. Returns (date, time or None, the rest) or None."""
    today, rest, d = now.date(), text, None
    month = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\b"
    pats = [  # explicit dates first: "Wed 23 October" means the 23rd
        (r"\b(on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+" + month,
         lambda m: on_date(today, int(m.group(2)), MONTHS.index(m.group(3)[:3].lower()) + 1)),
        (r"\b(on\s+)?" + month + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b",
         lambda m: on_date(today, int(m.group(3)), MONTHS.index(m.group(2)[:3].lower()) + 1)),
        (r"(?<!at )(?<!at)\b(on\s+)?(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?\b",  # not "at 5.10", that is a time
         lambda m: on_date(today, int(m.group(2)), int(m.group(3)), m.group(4) and int(m.group(4)))),
        (r"\b(on\s+)?(\d{4})-(\d{2})-(\d{2})\b", lambda m: on_date(today, int(m.group(4)), int(m.group(3)), int(m.group(2)))),
        (r"\b(on\s+)?(today|tonight)\b", lambda m: today),
        (r"\b(on\s+)?tomorrow\b", lambda m: today + timedelta(1)),
        (r"\b(on\s+|next\s+|this\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday"
         r"|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)\b",
         lambda m: next_day(today, m.group(2), (m.group(1) or "").strip().lower() == "next")),
    ]
    for pat, make in pats:
        m = re.search(pat, rest, re.I)
        if m:
            d = make(m)
            if d:
                rest = rest[:m.start()] + " " + rest[m.end():]
                break
    if not d:
        return None
    t = None
    m = (re.search(r"\b(?:at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b", rest, re.I)
         or re.search(r"\bat\s+(\d{1,2})(?:[:.](\d{2}))?\b()", rest, re.I)
         or re.search(r"\b(\d{1,2})[:.](\d{2})\b()", rest))
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        elif not ap and 1 <= h <= 7 and not m.group(2):
            h += 12  # "at 4" in a family's day means the afternoon
        if h < 24 and mi < 60:
            t = datetime.min.time().replace(hour=h, minute=mi)
            rest = rest[:m.start()] + " " + rest[m.end():]
    return d, t, re.sub(r"\s+", " ", rest).strip()


def next_day(today, word, next_week=False):
    i = next(i for i, n in enumerate(DAYS) if n.startswith(word[:3].lower()))
    gap = (i - today.weekday()) % 7
    return today + timedelta(gap or (7 if next_week else 0))


def on_date(today, day, month, year=None):
    try:
        d = date(year or today.year, month, day)
    except ValueError:
        return None
    if year or d >= today - timedelta(days=1):
        return d
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29 February, and next year has none
        return None


# ---------- prompts 07 to 09: money, dreams, countdowns, routines, meals, notes, the table itself ----------

def more_rules(c, fam, people, text, low, now, key):
    """Pip's rules for the rest of the table. Returns an answer, or None to let the date and job rules try."""
    import dashboard as d
    t = text.strip(" .!?")
    eur, today = d.eur, now.date()
    if re.search(r"\b(how much|what)('?s| is)? (money )?left\b|\bmoney left\b", low):
        return money.left_words(c, today)
    m = re.match(r"^(?:the\s+)?(.+?)\s+bill\s+(?:is\s+)?" + NUM + r"\s+(?:on|every|due on)\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b", t, re.I)
    if m and d.amount(m[2]) and 1 <= int(m[3]) <= 31:
        c.execute("insert into family.bills (title, amount, due_day) values (%s, %s, %s)", (m[1].capitalize()[:60], d.amount(m[2]), int(m[3])))
        return f"Added the {m[1].lower()} bill: {eur(d.amount(m[2]))} on the {int(m[3])}{suffix(int(m[3]))} of each month. It's in Money."
    it = money.line(c, t, today, loose=False)  # the same rules as the Money page's line
    if it:
        return money.apply(c, it, today)
    said = chores_and_journal(c, people, t, today)
    if said:
        return said
    m = re.match(r"^(?:we(?:'d| would)? (?:really )?(?:want|wish|love|like|dream) to|we(?:'d| would) love to|our dream is to|"
                 r"one day we(?:'ll| will)|someday we(?:'ll| will)|dream:)\s+(.+)$", t, re.I)
    if m:
        f = dream_from(t, today, c)
        sharers = [int(x) for x in f["sharers"].split(",") if x]
        did = c.execute("insert into family.dreams (title, cover, sharers, on_date, saving) values (%s, %s, %s, %s, true)"
                        " returning id", (f["title"], int(f["cover"]), sharers, f.get("on_date") or None)).fetchone()["id"]
        set_pending(c, key, {"dream": did})
        when = date.fromisoformat(f["on_date"]).strftime("%B %Y") if f.get("on_date") else "someday"
        return (f"Lovely. \"{f['title']}\" is on your dream board, {when}. How much will it cost, roughly? "
                f"I'll make a saving goal in Money (or say \"no goal\").")
    m = re.match(r"^(\w+)'?s (morning|evening)(?: routine)?\s*:\s*(.+)$", t, re.I)
    if m:
        who = next((p for p in people if p["name"].lower() == m[1].lower()), None)
        steps = [x.strip() for x in re.split(r",|\band\b", m[3]) if x.strip()][:8]
        if who and steps:
            c.execute("insert into family.routines (person_id, part, steps) values (%s, %s, %s)",
                      (who["id"], m[2].lower(), json.dumps([{"name": x[0].upper() + x[1:], "icon": d.icon_for(x)} for x in steps])))
            return f"Done: {who['name']}'s {m[2].lower()} has {len(steps)} picture steps. {who['name']} taps each one for a star."
    m = re.match(r"^(?:dinner|supper|tea)\b\s*(.*?)\s*:\s*(.+)$", t, re.I)
    if m:
        w = parse_when(m[1] or "today", now) if m[1] else (today, None, "")
        day = w[0] if w else today
        c.execute("insert into family.meals (on_date, title) values (%s, %s)", (day, m[2][:80].capitalize()))
        when = d.day_name(day, today) if timedelta(0) <= day - today < timedelta(7) else day.strftime("%-d %B")
        return f"Dinner {when.lower() if when in ('Today', 'Tomorrow') else 'on ' + when}: {m[2]}. It's in Meals."
    m = re.match(r"^(?:add\s+(.+?)\s+to the shopping(?: list)?|buy\s+(.+)|we need\s+(?!to\b)(.+))$", t, re.I)
    if m and not parse_when(t, now):
        item = (m[1] or m[2] or m[3] or "").strip()[:60]
        if not item:
            return None
        c.execute("insert into family.shopping (title) values (%s)", (item[0].upper() + item[1:],))
        return f"On the shopping list: {item}."
    m = re.match(r"^(?:note|pin)\s*:\s*(.+)$", t, re.I)
    if m:
        c.execute("insert into family.notes (text) values (%s)", (m[1][:200],))
        return "Pinned. It's in Notes."
    m = re.match(r"^(?:problem|bug|support|something'?s (?:wrong|not right))\s*:\s*(.+)$", t, re.I)
    if m:
        c.execute("insert into family.support (text, page) values (%s, 'told Pip')", (m[1][:2000],))
        return "Thank you. I've written it down for the people who look after your box."
    if re.search(r"\b(night|dark|paper|day|auto|warm day) (theme|mode|look)\b|\bdark mode\b", low):
        return ("Each screen chooses its own look: tap Day, Night, Paper or Auto at the top of the screen. "
                "Auto turns to Night at sunset and back to Day at sunrise.")
    return table_rule(c, fam, low)


def table_rule(c, fam, low):
    """"Make the calendar bigger", "Hide the money block", "Show the meals block", "Move dreams to the top"."""
    import dashboard as d
    words = dict(BLOCK_WORDS, **{fam["agent_name"].lower(): "pip"})
    shown = {b["kind"] for b in d.layout(c) if b["shown"]}
    kind = lambda w: ("day" if words.get(w.lower().strip()) == "today" and "day" in shown and "today" not in shown
                      else words.get(w.lower().strip()))  # on the new home, "the calendar" is the day block
    name = lambda k: fam["agent_name"] if k == "pip" else "list of what needs you" if k == "needs" else d.BLOCKS[k][0].lower()
    m = re.match(r"^(?:please\s+)?(?:make|set)\s+(?:the\s+|our\s+)?(\w+)(?:\s+block)?\s+(bigger|larger|smaller|small|medium|large|big)$", low)
    if m and kind(m[1]):
        k = kind(m[1])
        cur = next(b for b in d.layout(c) if b["kind"] == k)
        z = SIZE_WORDS[m[2]]
        new = "SML"[max(0, min(2, "SML".index(cur["size"]) + z))] if isinstance(z, int) else z
        if new == cur["size"]:
            return f"The {name(k)} is already as {'big' if new == 'L' else 'small' if new == 'S' else 'medium'} as it goes."
        d.arrange(c, k, size=new, shown=True)
        return f"Done: the {name(k)} is now {dict(S='small', M='medium', L='large')[new]} on your table."
    m = re.match(r"^(?:please\s+)?(hide|remove|take away|show|add|bring back)\s+(?:the\s+|our\s+)?(\w+)(?:\s+block)?(?:\s+(?:from|to|on) the (?:table|home screen))?$", low)
    if m and kind(m[2]) and (m[1] in ("hide", "remove", "take away") or "block" in low or m[1] != "add"):
        k = kind(m[2])
        show = m[1] in ("show", "add", "bring back")
        d.arrange(c, k, shown=show)
        return f"Done: the {name(k)} is {'back on' if show else 'off'} your table." + ("" if show else " Say \"show the " + m[2] + " block\" to bring it back.")
    m = re.match(r"^(?:please\s+)?move\s+(?:the\s+)?(\w+)(?:\s+block)?\s+(?:to the\s+)?(top|first|start|bottom|end|last|up|down)$", low)
    if m and kind(m[1]):
        k = kind(m[1])
        shown = [b["kind"] for b in d.layout(c) if b["shown"]]
        i = shown.index(k) if k in shown else len(shown)
        to = {"top": 0, "first": 0, "start": 0, "bottom": len(shown), "end": len(shown), "last": len(shown),
              "up": max(0, i - 1), "down": i + 1}[m[2]]
        d.arrange(c, k, shown=True, to=to)
        return f"Done: moved the {name(k)} {'to the top' if to == 0 else 'down' if m[2] == 'down' else 'up' if m[2] == 'up' else 'to the end'}."
    return None


def suffix(n):
    return "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def dream_from(text, today, c):
    """One sentence to the add-a-dream sheet: "We want to go to Lapland next winter" -> Lapland in winter, February."""
    t = re.sub(r"^(?:we(?:'d| would)? (?:really )?(?:want|wish|love|like|dream) to|we(?:'d| would) love to|our dream is to|"
               r"one day we(?:'ll| will)|someday we(?:'ll| will)|dream:)\s+", "", text.strip(" .!?"), flags=re.I)
    on, season = None, None
    m = re.search(r"\b(?:next|this|in(?: the)?)\s+(winter|spring|summer|autumn|fall)\b", t, re.I)
    if m:
        season = m[1].lower().replace("fall", "autumn")
        on = date(today.year, SEASONS[season], 1)
        if on <= today:
            on = on.replace(year=on.year + 1)
        t = (t[:m.start()] + t[m.end():]).strip()
    else:
        w = parse_when(t, datetime.combine(today, datetime.min.time()))
        if w and w[0] > today:
            on, t = w[0], w[2]
    t = re.sub(r"\s+", " ", t).strip(" .,!?") or "A family dream"
    m = re.match(r"^(?:go|travel|fly|get) to\s+(.+)$|^visit\s+(.+)$", t, re.I)
    if m:
        place = m[1] or m[2]
        t = place[0].upper() + place[1:] + (f" in {season}" if season else "")
    else:
        t = t[0].upper() + t[1:]
    people = c.execute("select id, role from family.people order by sort, id").fetchall()
    cover = 0 if re.search(r"lapland|north|snow|winter|ski", t, re.I) else 1 if re.search(r"ital|beach|sun|sea|summer", t, re.I) else \
        2 if re.search(r"sail|boat|lake|swim", t, re.I) else 4 if re.search(r"tree|garden|build", t, re.I) else \
        5 if re.search(r"cycle|bike|island", t, re.I) else 3 if re.search(r"puppy|dog|cat|pet", t, re.I) else 6
    return {"title": t[:80], "cover": str(cover), "when": "date" if on else "someday", "on_date": on.isoformat() if on else "",
            "sharers": ",".join(str(p["id"]) for p in people if p["role"] != "pet"), "saving": "1", "target": ""}


def saving_goal(c, dream_id, text, now):
    """The answer to "How much will it cost?": a saving goal for the dream, with what to put in each month."""
    import dashboard as d
    low = text.lower().strip(" .!?")
    if low in ("no", "no goal", "none", "not now", "skip", "no thanks", "later"):
        c.execute("update family.dreams set saving = false where id = %s", (dream_id,))
        return "Fine, no saving goal for now. You can add one in Money any time."
    words = re.sub(r"\b(about|around|roughly|maybe|perhaps|probably|it'?s|it is|in total|total)\b", "", low)
    m = re.fullmatch(r"\s*" + NUM + r"\s*", words)  # only an answer that is just an amount; anything else is a new message
    target = d.amount(m[1]) if m else None
    dr = c.execute("select * from family.dreams where id = %s", (dream_id,)).fetchone()
    if not target or not dr:
        return None
    if c.execute("select 1 from family.goals where dream_id = %s", (dream_id,)).fetchone():
        return None
    c.execute("insert into family.goals (title, target, by_date, dream_id) values (%s, %s, %s, %s)",
              (d.fund_name(dr["title"]), target, dr["on_date"], dream_id))
    months = d.months_to(dr["on_date"], now.date())
    each = f", about {d.eur(math.ceil(target / months / 10) * 10)} a month" if months else ""
    by = f" by {dr['on_date']:%B}" if dr["on_date"] else ""
    return (f"Saving goal made: {d.eur(target)}{by}{each}. It's in Money and on your dream board"
            + (", and I'll remind you on the first of each month." if months else "."))


def monthly(c, today):
    """Pip's quiet reminder on the first days of a month: put this month's share into each saving goal with a date.
    A family with a payday hears it on payday instead (payday())."""
    import dashboard as d
    if today.day > 3 or c.execute("select payday from family.household").fetchone()["payday"]:
        return 0
    month, made = today.strftime("%Y-%m"), 0
    for g in c.execute("select * from family.goals where by_date > %s and saved < target and (dream_id is null or dream_id in"
                       " (select id from family.dreams where lived_on is null))", (today,)).fetchall():
        if c.execute("select 1 from family.suggestions where items @> %s", (json.dumps([{"goal": g["id"], "month": month}]),)).fetchone():
            continue
        c.execute("update family.suggestions set state = 'dismissed', decided_at = now() where state = 'pending'"
                  " and items @> %s", (json.dumps([{"goal": g["id"]}]),))  # last month's reminder makes way
        each = math.ceil(float(g["target"] - g["saved"]) / d.months_to(g["by_date"], today) / 10) * 10
        c.execute("insert into family.suggestions (text, items, accept_label, quiet) values (%s, %s, 'Put it in', true)",
                  (f"A new month: put {d.eur(each)} in the {g['title']}?",
                   json.dumps([{"label": g["title"], "goal": g["id"], "amount": each, "month": month}])))
        made += 1
    return made


YES = {"yes", "y", "ok", "okay", "sure", "do it", "yes please", "please do", "jah", "да", "так"}
NO = {"no", "n", "not now", "no thanks", "later", "skip", "ei", "нет", "ні"}


def payday(c, today):
    """On payday (or the two days after, if the box was asleep): "Save first?" with each pot's share, once a month.
    Returns the Telegram message to send, or None."""
    import dashboard as d
    fam = c.execute("select payday from family.household").fetchone()
    if not fam or not fam["payday"]:
        return None
    last = (money.next_month(today) - timedelta(days=1)).day
    if not 0 <= today.day - min(fam["payday"], last) <= 2:
        return None
    month = today.strftime("%Y-%m")
    if c.execute("select 1 from family.suggestions where items @> %s", (json.dumps([{"payday": month}]),)).fetchone():
        return None
    items = []
    for g in c.execute("select * from family.goals where saved < target and (dream_id is null or dream_id in"
                       " (select id from family.dreams where lived_on is null)) order by by_date nulls last, id").fetchall():
        each = money.per_month(g["target"], g["saved"], g["by_date"], today)
        if each:
            items.append({"label": f"{g['title']} · {d.eur(each)}", "goal": g["id"], "amount": each, "payday": month})
    if not items:
        return None
    c.execute("update family.suggestions set state = 'dismissed', decided_at = now() where state = 'pending' and items @> %s",
              (json.dumps([{"payday": (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")}]),))
    sid = c.execute("insert into family.suggestions (text, items, accept_label) values (%s, %s, 'Save first') returning id",
                    ("Payday! Save first, before the month spends it?", json.dumps(items))).fetchone()["id"]
    plan = ", ".join(f"{d.eur(it['amount'])} to the {it['label'].split(' · ')[0]}" for it in items)
    return tell(c, f"Payday! Save first? {plan}. Reply YES and I'll put it in the pots, or change it on the home screen.",
                {"payday": sid})


def month_end(c, now):
    """The month's last evening: the four Kakeibo questions, with the month's numbers, once. Returns the message or None."""
    today = now.date()
    if money.next_month(today) - today != timedelta(days=1) or now.hour < 18:
        return None
    m0 = today.replace(day=1)
    if c.execute("select 1 from family.kakeibo where month = %s", (m0,)).fetchone():
        return None
    m = money.month(c, today)
    facts = {k: round(m[k], 2) for k in ("came", "bills", "living", "dreams")}
    c.execute("insert into family.kakeibo (month, facts) values (%s, %s)", (m0, json.dumps(facts)))
    e = money.eur
    return tell(c, f"The end of {m0:%B}. In numbers: {e(m['came'])} came in; {e(m['bills'])} went on bills, {e(m['living'])} on "
                   f"living and {e(m['dreams'])} into pots.\nFour questions, answer in one message:\n1. How much came in?\n"
                   f"2. How much do we want to save?\n3. How much did we spend?\n4. How can we do better next month?",
                {"kakeibo": m0.isoformat()})


def tell(c, text, pending=None):
    """A message from Pip to every private family chat that said YES, with what Pip now waits for. Returns [(chat, text)]
    to send after the transaction (nothing is sent from inside it)."""
    out = []
    for r in c.execute("select chat_id from family.chats where consent_at is not null and chat_id <> 0 and kind = 'private'").fetchall():
        if pending:
            set_pending(c, (r["chat_id"], r["chat_id"]), pending)
        save(c, "telegram", r["chat_id"], True, text, r["chat_id"])
        out.append((r["chat_id"], text))
    return out


def yes_no(c, pending, yes, tz):
    """The answer to Pip's payday question or to a statement it read."""
    import dashboard as d
    if pending.get("payday"):
        sg = c.execute("select * from family.suggestions where id = %s and state = 'pending'", (pending["payday"],)).fetchone()
        if not sg:
            return "That's already decided on the home screen."
        if not yes:
            c.execute("update family.suggestions set state = 'dismissed', decided_at = now() where id = %s", (sg["id"],))
            return "Fine, not this time. The pots wait for next payday."
        d.accept(c, sg["id"], tz)
        return "Done, saved first: " + ", ".join(f"{d.eur(it['amount'])} in the {it['label'].split(' · ')[0]}" for it in sg["items"]) + "."
    if not yes:
        return "Fine, they wait on the Money page until you look at them."
    ids = pending["statement"] if isinstance(pending["statement"], list) else None
    return "Done: " + money.done_words(money.accept(c, only=ids or [-1])) + ". It's all in Money."


def kakeibo_answer(c, text, now):
    """Is this message the answer to the four month-end questions? Not a question, not an order, not a money line or a date."""
    t = text.strip()
    if t.endswith("?") or len(t.split()) < 3 or money.line(c, t, now.date(), loose=False) or parse_when(t, now):
        return False
    return not re.match(r"(add|remind|what|when|where|who|how|show|hide|make|move|buy|note|pin|put|paid|spent|sign|log|"
                        r"dinner|supper|problem|count|we need|we want|our dream|done|stop|hello|hi|hey)\b", t, re.I)


def chores_and_journal(c, people, t, today):
    """"Tidy the shoes is worth 10 points", "A Lego castle costs 300 points", "Oskar did tidy the shoes",
    "Note for Lapland: found cheap flights"."""
    import dashboard as d
    m = re.fullmatch(r"(?:note|journal|diary)\s+(?:for|in|on)\s+(?:the\s+|our\s+)?(.+?)\s*:\s*(.+)|(.+?)\s+(?:note|journal)\s*:\s*(.+)", t, re.I)
    if m:
        dreams = c.execute("select id, title from family.dreams order by lived_on nulls first, id").fetchall()
        dr = money.best(m[1] or m[3], dreams, lambda x: [x["title"]])
        if dr and d.add_journal(c, dr["id"], m[2] or m[4]):
            return f"Written in the {dr['title']} journal."
    m = re.fullmatch(r"(?:(.+?)\s+(?:is worth|gives|earns)\s+(\d{1,4})\s+points?|(\d{1,4})\s+points?\s+for\s+(.+))", t, re.I)
    if m:
        what, n = (m[1], int(m[2])) if m[1] else (m[4], int(m[3]))
        task = money.best(what, c.execute("select id, title from family.tasks where done_at is null order by id desc").fetchall(),
                          lambda x: [x["title"]])
        if task and 0 < n <= 1000:
            c.execute("update family.tasks set points = %s where id = %s", (n, task["id"]))
            return f"\"{task['title']}\" is worth {n} points now. Tick it on the table when it's done."
    m = re.fullmatch(r"(?:(\w+)'?s\s+)?(.+?)\s+(?:costs?|needs?|is)\s+(\d{1,6})\s+points?", t, re.I)
    if m:
        dr = money.best(m[2], c.execute("select id, title, sharers from family.dreams where lived_on is null").fetchall(),
                        lambda x: [x["title"]])
        kids = [p for p in people if p.get("role") == "child"]
        who = next((p for p in people if m[1] and p["name"].lower() == m[1].lower()), None) or (
            next((p for p in kids if dr and p["id"] in (dr["sharers"] or [])), None))
        if dr and who and 0 < int(m[3]) <= 100000:
            c.execute("update family.dreams set points_for = %s, points_target = %s where id = %s", (who["id"], int(m[3]), dr["id"]))
            return f"{who['name']} earns {dr['title']} with {int(m[3])} points: chores and routine steps with points add up."
    m = re.fullmatch(r"(?:done:?\s+|finished:?\s+|(\w+)\s+(?:did|has done|finished|has finished)\s+)(?:the\s+)?(.+)", t, re.I)
    if m:
        tasks = c.execute("select id, title, person_id from family.tasks where done_at is null order by id").fetchall()
        who = next((p for p in people if m[1] and p["name"].lower() == m[1].lower()), None)
        mine = [x for x in tasks if not who or x["person_id"] in (who["id"], None)]
        task = money.best(m[2], mine, lambda x: [x["title"]])
        if task:
            c.execute("update family.tasks set done_at = now() where id = %s", (task["id"],))
            d.task_points(c, task["id"])
            pts = c.execute("select points from family.tasks where id = %s", (task["id"],)).fetchone()["points"]
            return f"Ticked: \"{task['title']}\"." + (f" {pts} points for {who['name'] if who else 'it'}!" if pts else "")
    return None


def statement_from_telegram(c, token, doc):
    """A statement file sent to Pip: read it, keep its lines for the family, and say what they look like."""
    name = doc.get("file_name") or ""
    if (doc.get("file_size") or 0) > 8_000_000:
        return "That file is too big for me. A month's statement is usually small: download one month and send it again."
    try:
        f = tg(token, "getFile", file_id=doc["file_id"])
        with urllib.request.urlopen(f"{API}/file/bot{token}/{f['file_path']}", timeout=30) as r:
            raw = r.read(8_000_001)
        bank, lines = money.read_statement(raw, name)
    except ValueError as ex:
        return str(ex)
    except (TelegramError, urllib.error.URLError, OSError, KeyError):
        return "I couldn't fetch that file from Telegram. Please send it again in a minute."
    n_new, _ = money.store(c, bank, lines)
    return money.summary(c, n_new, bank)


def photo_for_journal(c, token, msg, caption):
    """A photo with "Note for Lapland: ..." under it goes into that dream's journal."""
    import dashboard as d
    m = re.fullmatch(r"(?:note|journal|diary)\s+(?:for|in|on)\s+(?:the\s+|our\s+)?(.+?)\s*:\s*(.+)", caption.strip(" .!"), re.I)
    if not m:
        return None
    dr = money.best(m[1], c.execute("select id, title from family.dreams").fetchall(), lambda x: [x["title"]])
    if not dr:
        return None
    data = fetch_photo(token, msg)
    if data is None:
        return "I couldn't fetch that photo from Telegram. Please send it again in a minute."
    if len(data) > 8_000_000 or not d.add_journal(c, dr["id"], m[2], data):
        return "I couldn't keep that photo. Try a smaller one."
    return f"Written in the {dr['title']} journal, with your photo."


def fetch_photo(token, msg):
    """The biggest size of a Telegram photo, as bytes (None when Telegram does not give it)."""
    try:
        f = tg(token, "getFile", file_id=max(msg["photo"], key=lambda p: p.get("file_size") or 0)["file_id"])
        with urllib.request.urlopen(f"{API}/file/bot{token}/{f['file_path']}", timeout=30) as r:
            return r.read(8_000_001)
    except (TelegramError, urllib.error.URLError, OSError, KeyError, ValueError):
        return None


# ---------- Pip with AI (phase E): the rules first, the family's AI for the rest ----------

def tool(name, does, **params):
    need = [k for k, v in params.items() if not v.endswith("(optional)")]
    return {"name": name, "description": does, "parameters": {"type": "object", "required": need,
            "properties": {k: {"type": "string", "description": v} for k, v in params.items()}}}


TOOLS = [  # each one becomes a sentence the rules already understand, so AI changes go through the same code as typed ones
    tool("add_event", "Put something on the family calendar.", title="what it is, short", date="YYYY-MM-DD",
         time="HH:MM, 24-hour (optional)", person="one family member's name, if it is only for them (optional)"),
    tool("add_job", "A job or chore for someone.", job="the job, short", person="one family member's name (optional)",
         points="points it earns, a whole number (optional)"),
    tool("money", "Write money down, in the family's own words exactly like: '40 groceries', 'paid electricity', 'salary 3200', "
         "'put 20 in the box for Lapland', 'take 50 from Lapland', 'electricity bill 45 on the 15th', 'new pot bikes 600 by June'.",
         line="the money line"),
    tool("add_dream", "Add a family dream to the dream board.", dream="the dream, like 'go to Lapland next winter'"),
    tool("dream_note", "Write a line in one dream's journal.", dream="the dream's name", text="the note"),
    tool("shopping", "Add one thing to the shopping list.", item="the thing"),
    tool("dinner", "Plan a dinner.", date="YYYY-MM-DD", meal="what's for dinner"),
    tool("note", "Pin a note on the family's notes.", text="the note"),
    tool("change_table", "Change the home screen, in words like 'make the calendar bigger', 'hide the money block', "
         "'show the meals block', 'move dreams to the top'.", change="the change"),
]


def sentence(name, a):
    """A tool call -> the words the rules understand."""
    g = lambda k: re.sub(r"\s+", " ", str(a.get(k) or "")).strip()[:120]
    return {"add_event": lambda: f"Add {g('title')}" + (f" for {g('person')}" if g("person") else "") + f" on {g('date')}"
            + (f" at {g('time')}" if re.fullmatch(r"\d{1,2}:\d{2}", g("time")) else ""),
            "add_job": lambda: f"Remind {g('person') or 'everyone'} to {g('job')}" + (f" for {g('points')} points" if g("points").isdecimal() else ""),
            "money": lambda: g("line"), "add_dream": lambda: f"We want to {g('dream')}",
            "dream_note": lambda: f"Note for {g('dream')}: {g('text')}", "shopping": lambda: f"Add {g('item')} to the shopping list",
            "dinner": lambda: f"Dinner {g('date')}: {g('meal')}", "note": lambda: f"Note: {g('text')}",
            "change_table": lambda: g("change")}.get(name, lambda: "")()


SECRETS = [(re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "(a code)"), (re.compile(r"https?://\S*(?:signin\?k=|/feed/)\S*"), "(a link)"),
           (re.compile(r"\b(?:sk-|AIza)[A-Za-z0-9_-]{16,}"), "(a key)")]


def scrub(t):
    """Never to the AI: bot codes, sign-in and calendar links, keys."""
    for pat, put in SECRETS:
        t = pat.sub(put, t)
    return t


def table_words(c, fam, people, now):
    """The family's table in a few lines, for the AI. Nothing secret is in the tables these come from."""
    import dashboard as d
    today, tz = now.date(), now.tzinfo
    t0 = datetime.combine(today, datetime.min.time(), tz)
    names = {p["id"]: p["name"] for p in people}
    ev = c.execute("select title, starts_at, all_day, person_id from family.events where starts_at >= %s and starts_at < %s"
                   " order by starts_at limit 40", (t0, t0 + timedelta(days=21))).fetchall()
    out = [f"Family: the {fam['name']} family; people: " + ", ".join(f"{p['name']} ({p['role']})" for p in people),
           "Calendar, next three weeks: " + ("; ".join(
               f"{e['starts_at'].astimezone(tz):%a %Y-%m-%d}{'' if e['all_day'] else e['starts_at'].astimezone(tz).strftime(' %H:%M')} "
               f"{e['title']}" + (f" ({names.get(e['person_id'])})" if e["person_id"] else "") for e in ev) or "nothing")]
    jobs = c.execute("select title, person_id, points from family.tasks where done_at is null order by id limit 25").fetchall()
    out.append("Open jobs: " + ("; ".join(j["title"] + (f" ({names.get(j['person_id'])})" if j["person_id"] else "") for j in jobs) or "none"))
    out.append("Money: " + money.left_words(c, today))
    bills = c.execute("select title, amount, due_day, paid_month from family.bills order by due_day").fetchall()
    if bills:
        out.append("Bills: " + "; ".join(f"{b['title']} {d.eur(b['amount'])} on the {b['due_day']}"
                                         + (" (paid this month)" if b["paid_month"] and b["paid_month"] >= today.replace(day=1) else "")
                                         for b in bills))
    pots = c.execute("select title, saved, target, place from family.goals order by id").fetchall()
    if pots:
        out.append("Pots: " + "; ".join(f"{g['title']} {d.eur(g['saved'])} of {d.eur(g['target'])}" + (f" ({g['place']})" if g["place"] else "")
                                        for g in pots))
    dreams = c.execute("select title, on_date from family.dreams where lived_on is null order by id limit 12").fetchall()
    if dreams:
        out.append("Dreams: " + "; ".join(x["title"] + (f" ({x['on_date']:%B %Y})" if x["on_date"] else "") for x in dreams))
    shop = c.execute("select title from family.shopping where done_at is null order by id limit 20").fetchall()
    if shop:
        out.append("Shopping list: " + ", ".join(x["title"] for x in shop))
    return "\n".join(out)


def ai_state(c, state):
    c.execute("update family.household set ai_state = %s where ai_state is distinct from %s", (state, state))


def ai_turn(c, fam, people, text, now, via, key):
    """One message the rules did not understand, to the family's AI. Returns (answer or None, why not)."""
    chat, sender = (None, None) if key == (0, 0) else key
    hist = c.execute("select from_pip, text from family.messages where via = %s and chat_id is not distinct from %s"
                     " and sender is not distinct from %s order by id desc limit 6", (via, chat, sender)).fetchall()
    msgs = []
    for h in reversed(hist):
        role = "assistant" if h["from_pip"] else "user"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + scrub(h["text"])
        elif msgs or role == "user":
            msgs.append({"role": role, "content": scrub(h["text"])})
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n" + scrub(text)
    else:
        msgs.append({"role": "user", "content": scrub(text)})
    who = sender and c.execute("select name from family.members where tg_user = %s", (sender,)).fetchone()
    system = scrub(f"You are {fam['agent_name']}, the helper of the {fam['name']} family, on their private family table (FamBox). "
                   f"Now: {now:%A %-d %B %Y, %H:%M} ({fam['tz']}). " + (f"Writing to you: {who['name']}. " if who and who["name"] else "")
                   + "Answer short and warm, in the language they write in, in plain words with no markdown. "
                   "To change the table, use the tools; never say you changed something without a tool. If you are unsure "
                   "what they want, ask one short question. You only know what is below.\n" + table_words(c, fam, people, now))
    try:
        r = ai.ask(c, msgs, system=system, tools=TOOLS)
    except ai.AIError as ex:
        ai_state(c, ex.reason)
        return None, ex.reason
    ai_state(c, "ok")
    done, ask = [], []
    for call in r.get("tool_calls") or []:
        words = sentence(call.get("name"), call.get("arguments") or {})
        if not words:
            continue
        it = call.get("name") == "money" and money.line(c, words, now.date(), loose=False)
        if (it and it["do"] == "pot_out") or re.match(r"(hide|remove|take away)\b", words, re.I):  # asks first
            ask.append(words)
            continue
        done.append(rules(c, fam, people, words, words.lower().strip(" .!?"), now, via, key, True, False)
                    or f"I couldn't do \"{words}\".")
    if ask:
        set_pending(c, key, {"ai": ask})
        done.append("Shall I " + "; and ".join(x[0].lower() + x[1:] for x in ask) + "? Reply YES or NO.")
    said = " ".join(done) or (r.get("text") or "").strip()
    return (scrub(said)[:1500] or None), "empty"


HELP_AI = {"cap_reached": "I've used this month's AI, so until the 1st I understand only the simple things above.",
           "ai_not_set_up": "My AI isn't switched on yet, so for now I understand only the simple things above."}


def ai_note(c, why, today):
    """Why the AI did not answer, at most once a day."""
    if why == "no_ai":
        return HELP_END
    if not c.execute("update family.household set ai_said_on = %s where ai_said_on is distinct from %s returning 1",
                     (today, today)).fetchone():
        return ""
    return " " + HELP_AI.get(why, "My AI isn't answering right now, so I'm using the simple rules.")


def letter(c, data):
    """A photo of a letter: the dates in it, as one suggestion ("Add all 3"). The photo is not kept.
    Returns (Pip's words, the suggestion's id or None)."""
    fam = c.execute("select * from family.household").fetchone()
    now = datetime.now(ZoneInfo(fam["tz"]))
    names = [p["name"] for p in c.execute("select name from family.people order by sort, id").fetchall()]
    ask = (f"Today is {now:%A %-d %B %Y}. The family: {', '.join(names)}. This is a photo of a letter, notice or timetable. "
           "List each date in it that a family would put on its calendar. Answer with JSON only: a list like "
           '[{"title": "Photo day", "date": "YYYY-MM-DD", "time": "HH:MM" or null, "end_date": "YYYY-MM-DD" or null, '
           '"person": one of the family\'s names if it is clearly only for them, else null}]. An empty list if there are no dates.')
    try:
        r = ai.ask(c, [{"role": "user", "content": ask}], image=data)
    except ai.AIError as ex:
        ai_state(c, ex.reason)
        return ("I've used this month's AI, so I can't read photos until the 1st. " if ex.reason == "cap_reached" else
                "I can't read photos right now. ") + "Tell me the date in words (\"Photo day on 23 October\") and I'll add it.", None
    ai_state(c, "ok")
    items = letter_items(r.get("text") or "", names)
    if not items:
        return "I couldn't find any dates in that photo. Tell me the date in words and I'll add it.", None
    n = len(items)
    sid = c.execute("insert into family.suggestions (text, items, accept_label) values (%s, %s, %s) returning id",
                    (f"I found {n} date{'s' if n > 1 else ''} in the letter. Add {'them' if n > 1 else 'it'}?", json.dumps(items),
                     "Add all {n}" if n > 1 else "Add it")).fetchone()["id"]
    return (f"I found {n} date{'s' if n > 1 else ''} in the letter:\n" + "\n".join("• " + it["label"] for it in items)
            + f"\nAdd {'them' if n > 1 else 'it'} to your calendar?"), sid


def letter_items(text, names):
    """The AI's list -> suggestion items, keeping only what reads as a real date."""
    try:
        got = json.loads(text[text.index("["):text.rindex("]") + 1])
    except ValueError:
        return []
    items = []
    for x in got if isinstance(got, list) else []:
        try:
            title, d0 = str(x.get("title") or "").strip()[:80], date.fromisoformat(str(x.get("date")))
            end = date.fromisoformat(str(x["end_date"])) if x.get("end_date") else None
        except (AttributeError, ValueError, TypeError):
            continue
        at = str(x.get("time") or "")
        at = at if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at) else None
        who = next((n for n in names if n.lower() == str(x.get("person") or "").lower()), None)
        if not title:
            continue
        label = f"{title} · {d0:%a} {d0.day} {d0:%b}" + (f" at {at}" if at else "") + (
            f" to {end.day} {end:%b}" if end and end > d0 else "")
        items.append({k: v for k, v in dict(label=label, title=title, date=d0.isoformat(), time=at,
                                             end_date=end.isoformat() if end and end > d0 and not at else None, person=who).items() if v})
    return items[:12]


def letter_buttons(said, sid, c):
    """Pip's answer to a letter photo on Telegram: one tap adds every date; "Look first" opens the home screen."""
    if not sid:
        return said
    n = len(c.execute("select items from family.suggestions where id = %s", (sid,)).fetchone()["items"])
    row = [{"text": f"Add all {n}" if n > 1 else "Add it", "callback_data": f"add:{sid}"}]
    if welcome.base_url():
        row.append({"text": "Look first", "url": f"{welcome.base_url()}/?look={sid}#pip"})
    return {"text": said, "reply_markup": {"inline_keyboard": [row]}}


# ---------- the morning message, one per person who said YES ----------

def morning_words(c, fam, m, now):
    """Rules only: this person's day, their jobs, bills due today, the weather and one dream."""
    import dashboard as d
    today, tz = now.date(), now.tzinfo
    people = c.execute("select id, name, role from family.people").fetchall()
    p = next((x for x in people if x["id"] == m["person_id"]), None) or \
        next((x for x in people if x["name"].lower() == (m["name"] or "").lower()), None)
    t0 = datetime.combine(today, datetime.min.time(), tz)
    evs = [e for e in c.execute("select title, starts_at, all_day, person_id from family.events where starts_at < %s"
                                " and coalesce(ends_at, starts_at) >= %s order by all_day desc, starts_at",
                                (t0 + timedelta(days=1), t0)).fetchall() if not p or e["person_id"] in (None, p["id"])]
    out = [f"Good morning{', ' + m['name'] if m['name'] else ''}!"]
    out.append("Today: " + "; ".join(("all day" if e["all_day"] else f"{e['starts_at'].astimezone(tz):%H:%M}") + " " + e["title"]
                                    for e in evs) + "." if evs else "Nothing on the calendar today.")
    if p:
        jobs = c.execute("select title from family.tasks where done_at is null and person_id = %s order by id limit 5", (p["id"],)).fetchall()
        if jobs:
            out.append("Your jobs: " + "; ".join(j["title"] for j in jobs) + ".")
    last = (money.next_month(today) - timedelta(days=1)).day
    due = [b for b in c.execute("select title, amount, due_day, paid_month from family.bills").fetchall()
           if min(b["due_day"], last) == today.day and not (b["paid_month"] and b["paid_month"] >= today.replace(day=1))]
    if due:
        out.append("Due today: " + "; ".join(f"{b['title']} {d.eur(b['amount'])}" for b in due) + ".")
    w = d.weather(fam, now)
    if w:
        out.append(f"Weather: {w['temp']}°" + (f", {w['words'].lower()}" if w["words"] else "")
                   + (f", rain from {w['rain']:%H:%M}" if w["rain"] else "") + ".")
    dreams = c.execute("select d.title, d.on_date, g.saved, g.target from family.dreams d left join family.goals g on g.dream_id = d.id"
                       " where d.lived_on is null order by d.id").fetchall()
    if dreams:
        dr = dreams[today.toordinal() % len(dreams)]
        out.append(f"A dream to keep in mind: {dr['title']}" + (f", {d.eur(dr['saved'])} of {d.eur(dr['target'])} saved" if dr["target"]
                   else f", {(dr['on_date'] - today).days} days to go" if dr["on_date"] and dr["on_date"] > today else "") + ".")
    out.append("(\"morning off\" stops these; \"morning 6:30\" changes the time.)")
    return "\n".join(out)


def morning_due(c, now):
    """The morning messages due now: each person once a day, within three hours of their time. Returns [(chat, text)]."""
    fam = c.execute("select * from family.household").fetchone()
    today, mins = now.date(), now.hour * 60 + now.minute
    out = []
    for m in c.execute("select m.* from family.members m join family.chats ch on ch.chat_id = m.tg_user and ch.consent_at is not null"
                       " where m.consent_at is not null and m.morning is not null and m.morning_sent is distinct from %s",
                       (today,)).fetchall():
        if not 0 <= mins - (m["morning"].hour * 60 + m["morning"].minute) < 180:
            continue
        if c.execute("update family.members set morning_sent = %s where tg_user = %s and morning_sent is distinct from %s returning 1",
                     (today, m["tg_user"], today)).fetchone():  # marked before sending: never twice
            text = morning_words(c, fam, m, now)
            save(c, "telegram", m["tg_user"], True, text, m["tg_user"])
            out.append((m["tg_user"], text))
    return out


def mornings():
    """Every five minutes, beside the dashboard: morning messages, the first-of-month reminders, payday's "save first?"
    and the month-end questions."""
    while True:
        time.sleep(300)  # first after five minutes: a new version's schema is in place by then
        try:
            out, token = [], None
            with db() as c, c.transaction():
                fam = c.execute("select tz, bot_token from family.household").fetchone()
                if fam:
                    now = datetime.now(ZoneInfo(fam["tz"]))
                    monthly(c, now.date())
                    out = morning_due(c, now) + (payday(c, now.date()) or []) + (month_end(c, now) or [])
                    token = fam["bot_token"]
            if token and not DRY:
                for chat, text in out:
                    tg(token, "sendMessage", chat_id=chat, text=text)
        except Exception as ex:  # never stops the dashboard; tries again in five minutes
            print(f"pip mornings: {type(ex).__name__}: {ex}", file=sys.stderr)
