"""Moving in (onboarding) and signing in, for a family's box.

Moving in: the seven steps of docs/design/handoff/ (Onboarding Step.dc.html), plus one step the design
does not have yet, "Connect your AI" (step 4 here, after the agent is chosen). Every step is a plain
form that posts back here; JavaScript only makes the Telegram code check start by itself.

Signing in: adults sign in by link (SPEC.md). A link works once; using it gives this browser a
session for 400 days. Links come from the box's welcome message (`dashboard.py --link`) or from Pip
on Telegram ("sign in"). Links, sessions and Telegram join codes are stored only as SHA-256 hashes.
"""
import hashlib
import html
import os
import secrets
import urllib.parse
from datetime import date, datetime
from zoneinfo import ZoneInfo

import dashboard as d
import pip

e = html.escape
STEPS = ["Welcome", "Your family", "Your agent", "Connect your AI", "Connect Telegram", "Say hello",
         "Your first dream", "Your home screen"]
COLOURS = ["teal", "sky", "plum", "sage", "rose", "clay", "lavender", "olive"]
ROLES = [("adult", "Grown-up"), ("child", "Child"), ("pet", "Pet")]
# The six characters from the design: (name, extra shape, its fill, its stroke, stroke width, mouth)
CHARACTERS = {
    "Pip": ("", "none", "none", 0, "M26.5 41q5.5 5 11 0"),
    "Hoot": ("M15 22L13 8l11 7zM49 22l2-14-11 7z", "var(--fb-mustard)", "var(--fb-mustard)", 2, "M29 41l3 3 3-3"),
    "Fen": ("M16 24L11 4l17 10zM48 24l5-20-17 10z", "var(--fb-mustard)", "var(--fb-mustard)", 2, "M26.5 41q5.5 4 11 0"),
    "Sprout": ("M32 16V9M32 10c0-5 4-8 10-8 0 5-4 8-10 8zM32 12c0-4-3-6-8-6 0 4 3 6 8 6z",
               "var(--fb-person-sage)", "var(--fb-person-sage)", 2, "M26.5 41q5.5 5 11 0"),
    "Bun": ("M22 18c-4-6-5-15-1-16s6 9 6 15zM42 18c4-6 5-15 1-16s-6 9-6 15z", "var(--fb-mustard)", "var(--fb-mustard)", 2,
            "M28 41q4 3.5 8 0"),
    "Sunny": ("M32 2v6M58 35h4M2 35h4M50.4 16.6l3-3M10.6 13.6l3 3M50.4 53.4l3 3M13.6 53.4l-3 3", "none",
              "var(--fb-mustard)", 4, "M25.5 40q6.5 7 13 0"),
}
COVERS = 4  # drawn covers, in welcome.css (.cover-0 to .cover-3)


# ---------- keys: sign-in links, sessions, Telegram join codes ----------

def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def mint(c, kind, hours):
    raw = secrets.token_urlsafe(24)
    c.execute("delete from family.keys where expires_at < now() - interval '1 day'")
    c.execute("insert into family.keys (hash, kind, expires_at) values (%s, %s, now() + make_interval(hours => %s))",
              (_hash(raw), kind, hours))
    return raw


def redeem(c, raw, kind):
    """Uses a one-time key. True once; never again."""
    return bool(raw) and c.execute("update family.keys set used_at = now() where hash = %s and kind = %s and used_at"
                                   " is null and expires_at > now()", (_hash(raw), kind)).rowcount == 1


def signed_in(c, raw):
    return bool(raw) and c.execute("select 1 from family.keys where hash = %s and kind = 'session' and expires_at > now()",
                                   (_hash(raw),)).fetchone() is not None


def base_url():
    """The box's own address, from the engine's box.env (FAMBOX_ADDRESS). None when unknown."""
    a = os.environ.get("FAMBOX_ADDRESS", "").strip().rstrip("/")
    return (a if "://" in a else "https://" + a) if a else None


def address(fam):
    a = base_url()
    return a.split("://", 1)[1] if a else f'{fam["slug"]}.fam.box'


def link_for(c, hours=24 * 7):
    base = base_url()
    return base and f"{base}/signin?k={mint(c, 'link', hours)}"


# ---------- drawing ----------

def face(character, size, mood=None):
    """An agent character from the design, `size` = the ring's width; ears and rays reach outside it.
    mood "sleepy" (Night on the wall): closed eyes, a small mouth (brand system, moods)."""
    extra, fill, stroke, sw, mouth = CHARACTERS.get(character, CHARACTERS["Pip"])
    ex = (f'<path d="{extra}" style="fill:{fill};stroke:{stroke}" stroke-width="{sw}" stroke-linecap="round" '
          f'stroke-linejoin="round"/>') if extra else ""
    eyes = ('<path d="M23 33q2.5 2.5 5 0M36 33q2.5 2.5 5 0" class="fsleep" stroke-width="2.4" stroke-linecap="round"/>'
            if mood == "sleepy" else '<ellipse cx="25.5" cy="33" rx="2.6" ry="3.3" class="feye"/><ellipse cx="38.5" cy="33" '
            'rx="2.6" ry="3.3" class="feye"/>')
    mouth = "M29 42q3 2 6 0" if mood == "sleepy" else mouth
    return (f'<svg class="face" width="{size}" height="{size}" viewBox="10.5 13.5 43 43" overflow="visible" '
            f'aria-hidden="true">{ex}<circle cx="32" cy="35" r="19" class="fring" stroke-width="5"/>'
            f'{eyes}<path d="{mouth}" class="fmouth" stroke-width="3" stroke-linecap="round"/></svg>')


LOGO = ('<svg width="32" height="32" viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="12" '
        'style="fill:var(--fb-ink)"/><circle cx="32" cy="9" r="7" style="fill:var(--fb-tomato)"/><circle cx="55" '
        'cy="32" r="7" style="fill:var(--fb-mustard)"/><circle cx="32" cy="55" r="7" style="fill:var(--fb-night)"/>'
        '<circle cx="9" cy="32" r="7" style="fill:var(--fb-person-sage)"/></svg>')
TICK = '<svg width="11" height="11" viewBox="0 0 10 10" aria-hidden="true"><path d="M2 5.2l2 2L8 3"/></svg>'


def shell(fam, n, reached, body, actions, live=False, title=None):
    """One moving-in page: the rail (laptop) or the dots (phone), the step, the buttons."""
    rail = []
    for i, name in enumerate(STEPS, 1):
        state = "now" if i == n else "done" if i < reached else "next"
        href = "/?tour=1" if i == len(STEPS) else f"/welcome/{i}"
        inner = f'<i class="rdot">{TICK if state == "done" else ""}</i>{e(name)}'
        rail.append(f'<a href="{href}" class="r {state}">{inner}</a>' if i <= reached else
                    f'<span class="r {state}">{inner}</span>')
    dots = "".join(f'<i class="{"done" if i < n else "now" if i == n else ""}"></i>' for i in range(1, len(STEPS) + 1))
    back = f'<a href="/welcome/{n - 1}" class="back" aria-label="Back">‹</a>' if n > 1 else '<span class="back"></span>'
    addr = address(fam)
    # The form's first button is a hidden plain one, so Enter in any field saves the step
    # (and never presses "Take ... off the list" or an idea that happens to come first).
    return d.document(title or f"Moving in · The {fam['name']} Family", f'''<main class="mv" data-tz="{e(fam["tz"])}"{" data-live" if live else ""}>
<aside class="rail laptop-only"><div class="logo">{LOGO}<span>FamBox</span></div><div class="label">Moving in</div>
<nav class="steps" aria-label="Moving in">{"".join(rail)}</nav>
<div class="railfoot">{e(addr)}<br>Saved as you go. You can stop and come back.</div></aside>
<div class="mvbody"><div class="phone-only phonebar"><b data-clock>{d.hm(datetime.now(ZoneInfo(fam["tz"])))}</b><span>{e(addr)}</span></div>
<div class="phone-only dots">{back}<span class="dotrow">{dots}</span><span class="of">Step {n} of {len(STEPS)}</span></div>
<form method="post" action="/welcome/{n}" class="mvform" id="step" autocomplete="off">
<button class="sr" tabindex="-1" aria-hidden="true"></button>{body}
<div class="actions">{actions}</div></form></div>
</main>''', "welcome")


def button(text, on=True, name=None, value=None):
    if not on:
        return f'<span class="btn big off" aria-disabled="true">{e(text)}</span>'
    nv = f' name="{e(name)}" value="{e(value or "1")}"' if name else ""
    return f'<button class="btn raised big"{nv}>{e(text)}</button>'


def quiet(text, href=None, name=None):
    if href:
        return f'<a class="quietlink" href="{e(href)}">{e(text)}</a>'
    return f'<button class="quietlink" name="{e(name)}" value="1" formnovalidate>{e(text)}</button>'


def chair_ring(people, width, r, dist):
    return d.ring(width, r, dist, [p["colour"] for p in people] or COLOURS[:4])  # no people yet: four empty chairs


# ---------- the steps ----------

def step1(c, fam, people, q):
    addr = address(fam)
    body = f'''<div class="opening"><div class="boxart">{chair_ring(people, 150, 13, 57).replace('class="sym"', 'class="sym rise"')}
<div class="lid"></div><div class="boxbody">{e(addr)}</div></div></div>
<div class="label">Welcome home</div><h1 class="mvtitle">Your box is ready, {e(fam["name"])} family.</h1>
<p class="lede">Everything is already set up at {e(addr)}. Moving in takes about ten minutes: your people, your agent, and one first dream.</p>'''
    if os.environ.get("FAMBOX_LEGAL") == "1":  # set when the fam.box legal pages are published (docs/legal/build.py --publish)
        body += ('<p class="note">By moving in you accept our <a href="https://fam.box/legal/terms/">Terms of Service</a>. '
                 'How we handle your family\'s data: <a href="https://fam.box/legal/privacy/">Privacy Policy</a>.</p>')
    return body, button("Let's move in") + quiet("What is a FamBox?", "https://fam.box")


def person_row(p, i, open_=False):
    col = p.get("colour") or "teal"
    fg = "light" if col in d.LIGHT_TEXT else "dark"
    sw = "".join(f'<label class="sw c-{k}" title="{k.capitalize()}"><input type="radio" name="colour_{i}" value="{k}"'
                 f'{" checked" if k == col else ""}><span class="sr">{k.capitalize()}</span></label>' for k in COLOURS)
    roles = "".join(f'<label class="pill"><input type="radio" name="role_{i}" value="{k}"'
                    f'{" checked" if k == (p.get("role") or "adult") else ""}><span>{v}</span></label>' for k, v in ROLES)
    name = p.get("name") or ""
    remove = (f'<button class="quietlink small" name="remove" value="{p["id"]}" formnovalidate data-confirm="Take {e(name)} '
              f'off the list? Their dates and jobs become everyone\'s.">Take {e(name)} off the list</button>'
              if p.get("id") else "")
    return f'''<details class="prow"{" open" if open_ else ""}><summary>
<span class="chair c-{col} {fg}" style="--s:44px">{e(name[:1] or "+")}</span>
<span class="pname"><b>{e(name) or "Someone new"}</b> <span>{e(p.get("detail") or "")}</span></span>
<span class="cname">{col.capitalize()}</span></summary>
<input type="hidden" name="id_{i}" value="{p.get("id") or "new"}">
<div class="pedit"><label class="field"><span>Name</span><input name="name_{i}" value="{e(name)}" maxlength="40"></label>
<label class="field"><span>Who they are <i>(Mum, 9, the dog)</i></span><input name="detail_{i}" value="{e(p.get("detail") or "")}" maxlength="40"></label>
<div class="swatches" role="radiogroup" aria-label="Colour">{sw}</div><div class="pills" role="radiogroup" aria-label="Role">{roles}</div>{remove}</div>
</details>'''


def step2(c, fam, people, q, error=""):
    rows = [person_row(p, i, open_=False) for i, p in enumerate(people)]
    if "add" in q or not people:
        used = {p["colour"] for p in people}
        rows.append(person_row({"colour": next((k for k in COLOURS if k not in used), "teal")}, len(people), open_=True))
    err = f'<p class="err" role="alert">{e(error)}</p>' if error else ""
    body = f'''<div class="two"><div class="col">
<h1 class="mvtitle">Who sits at your table?</h1><p class="lede">Each person gets a colour and a chair that follows them everywhere.</p>
<label class="famname"><span>Family name</span><input name="family" value="{e(fam["name"])}" maxlength="40" required></label>
<div class="plist">{"".join(rows)}
<button class="prow addrow" name="add" value="1"><span class="chair plus" style="--s:44px">+</span><b>Add someone</b></button></div>{err}</div>
<div class="col preview laptop-only">{chair_ring(people, 200, 16, 78)}<div class="ptitle">The {e(fam["name"])} Family</div>
<p class="note">Your table so far. It becomes your browser tab and {e(fam["agent_name"])}'s picture.</p></div></div>'''
    return body, button("Next: your agent") + quiet("Back", "/welcome/1")


def step3(c, fam, people, q):
    cards = "".join(f'<label class="agent"><input type="radio" name="character" value="{k}"'
                    f'{" checked" if k == fam["agent_character"] else ""}>{face(k, 43)}<b>{k}</b></label>'
                    for k in CHARACTERS)
    name = fam["agent_name"]
    body = f'''<h1 class="mvtitle">Who will help at the table?</h1>
<p class="lede">Pick a character and give it a name. It lives on your box and talks to you on Telegram.</p>
<div class="agents">{cards}</div>
<label class="field big"><span>Its name</span><input name="agent_name" value="{e(name)}" maxlength="20" required data-agent-name></label>
<div class="hello">{face(fam["agent_character"], 40)}<p>Hi, I'm <span data-agent-say>{e(name)}</span>. I'll look after the {e(fam["name"])} family's table. You can change my name any time.</p></div>'''
    return body, button("Next: connect your AI") + quiet("Back", "/welcome/2")


def step4(c, fam, people, q):
    a = e(fam["agent_name"])
    hosted = bool(os.environ.get("FAMBOX_AI_TOKEN"))
    mode = fam["ai_mode"] if fam["ai_mode"] != "fambox" or hosted else "rules"
    opts = [("fambox", "FamBox AI", ("Included with your table. " if hosted else "An add-on for EUR 5 a month, coming with your plan. ")
             + f"{a} understands anything you write and reads photos of letters."),
            ("own", "Use our own key", f"Anthropic, OpenAI, OpenRouter, Google Gemini or Mistral. Add the key after moving in, under {a}'s thinking."),
            ("rules", "Simple rules for now", f"{a} adds dates, jobs and reminders you write in plain words, and tells you what's on today.")]
    cards = "".join(f'<label class="plan{" soon" if k == "fambox" and not hosted else ""}"><input type="radio" name="plan" value="{k}"'
                    f'{" checked" if k == mode else ""}{" disabled" if k == "fambox" and not hosted else ""}><b>{t}</b><span>{s}</span></label>'
                    for k, t, s in opts)
    body = f'''<h1 class="mvtitle">Connect your AI</h1>
<p class="lede">With AI, {a} understands anything you write or photograph. Without it, {a} still works with simple rules. You can change this any time.</p>
<div class="plans">{cards}</div>
<p class="note">Your own key stays on your box: FamBox never shows it, and {a} only ever reaches your own box.</p>'''
    return body, button("Next: connect Telegram") + quiet("Skip for now", name="skip")


GUIDE = [
    ("Open BotFather", [("BotFather ✓ official", 0), ("Start", 1)], "Scan the code or tap the button. Check it has the tick, then tap Start."),
    ("Send it the message /newbot", [("/newbot", 1), ("Choose a name for your bot.", 0)], "Type it exactly, with the slash at the front."),
    ("Give {a} a name and a username", [("{a}", 1), ("{slug}_{al}_bot", 1)], 'The username must end in "bot". If it is taken, add your town.'),
    ("Copy the long code it sends back", [("Done! Here is your code:", 0), ("7212345678:AAH4kq…  tap to copy", 2)],
     "Press and hold the code to copy it. Keep it to yourself."),
]
CODE_PROBLEMS = {
    "short": "That code looks too short. Copy the whole line from BotFather, including the part after the colon, and paste it again.",
    "unknown": "Telegram didn't recognise that code. Copy it again from BotFather's last message.",
    "in_use": "Another program already uses this bot. Make a new bot in BotFather (send it /newbot) and paste the new code.",
    "offline": "We couldn't reach Telegram just now. Wait a minute and paste the code again.",
    "consent": "Tick the box first: it says what passes through Telegram.",
}
TELEGRAM_CONSENT = ("I want {a} on Telegram. I understand that what we write to {a} and {a}'s answers pass through Telegram, "
                    "a company outside the EU with no EU data protection agreement, so EU data protection cannot be guaranteed there. "
                    "We can stop at any time; the box works without it.")


def step5(c, fam, people, q, code="", problem=None):
    a, slug = fam["agent_name"], fam["slug"]
    fill = lambda s: s.replace("{a}", a).replace("{slug}", slug.replace("-", "_")).replace("{al}", a.lower())
    bub = lambda t, k: f'<div class="bub {("them", "me", "code")[k]}">{e(fill(t))}</div>'
    cards = "".join(f'''<details class="guide" id="guide-{i}" open><summary><i class="num">{i}</i><b>{e(fill(t))}</b></summary>
<div class="pic">{"".join(bub(x, k) for x, k in pic)}</div><p class="note">{e(h)}</p>
{'<a class="btn sunk phone-only" href="https://t.me/BotFather">Open BotFather in Telegram</a>' if i == 1 else ""}</details>'''
                    for i, (t, pic, h) in enumerate(GUIDE, 1))
    connected = fam["bot_username"] and "change" not in q and not problem
    if connected:
        paste = f'''<div class="pastecard ok" role="status">{face(fam["agent_character"], 40)}<div><b>Connected.</b>
<p>{e(a)} is now @{e(fam["bot_username"])} on Telegram.</p><a class="quietlink small" href="/welcome/5?change=1">Use a different bot</a></div></div>'''
        if not pip.DRY and c.execute("select 1 from family.members where consent_at is not null").fetchone():  # phase E: the family group
            paste += (f'<div class="pastecard"><b>Your family group</b><p class="note">Add {e(a)} to the group you already have. '
                      f'{e(a)} answers there when someone writes @{e(fam["bot_username"])} or replies to {e(a)}.</p>'
                      f'<a class="btn sunk" href="/telegram/group">Add {e(a)} to your family group</a></div>')
    else:
        state = "bad" if problem else ""
        msg = (f'<p class="err" role="alert">{e(CODE_PROBLEMS[problem])}</p><a class="quietlink small" href="#guide-4" '
               f'data-open="guide-4">Show me where to find it</a>' if problem else
               '<p class="note" data-hint>It looks like a long line of numbers and letters with a colon in it. We check it straight away.</p>')
        paste = f'''<div class="pastecard {state}"><div class="pastehead"><i class="num soft">5</i><b>Paste the code from BotFather</b></div>
<label class="consent"><input type="checkbox" name="consent" value="yes"{" checked" if problem and problem != "consent" else ""}><span>{e(TELEGRAM_CONSENT.replace("{a}", a))}</span></label>
<input name="code" value="{e(code)}" class="codefield" inputmode="text" spellcheck="false" autocapitalize="off" aria-label="The code from BotFather" data-autocheck>
<p class="checking" hidden><i></i><i></i><i></i> Checking with Telegram</p>{msg}
<button class="btn sunk" name="check" value="1">Check the code</button></div>'''
    body = f'''<div class="tg"><div class="col">
<h1 class="mvtitle">Connect {e(a)} to Telegram</h1>
<p class="lede">Telegram makes {e(a)} its own account through its official helper, called BotFather. It takes about three minutes. We'll go one step at a time.</p>
<div class="guides">{cards}</div></div>
<div class="col side"><div class="qr laptop-only"><img src="/static/botfather-qr.svg" width="124" height="124" alt="">
<div><b>Scan with your phone</b><p class="note">Point your phone's camera here and BotFather opens in Telegram.</p></div></div>
{paste}
<details class="help" id="help"><summary class="sr">Help</summary><p class="note">Stuck? Telegram's own guide shows the same steps with pictures:
<a href="https://core.telegram.org/bots/tutorial#obtain-your-bot-token">How to get a bot</a>. Or ask us at the address on fam.box.</p></details></div></div>'''
    return body, button("Next: say hello", on=bool(connected), name="next") + \
        f'<a class="quietlink" href="#help" data-open="help">I need help</a>'


IDEAS = [("Hello {a}! This is the {f} family.", "A simple hello", "M4 12h16M4 6h10M4 18h12"),
         ("Add the dentist on Thursday at 4", "{a} adds it to the calendar", "M4 6h16v14H4zM4 10h16M8 3v4M16 3v4"),
         ("Here's a photo of a school letter", "{a} finds the dates in it",
          "M4 8h3l2-3h6l2 3h3v11H4zM12 10a3.5 3.5 0 1 1 0 7a3.5 3.5 0 1 1 0-7z")]


def step6(c, fam, people, q):
    a = fam["agent_name"]
    fill = lambda s: s.replace("{a}", a).replace("{f}", fam["name"])
    ideas = "".join(f'''<button class="idea" name="say" value="{e(fill(t))}"><i class="ic"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
<path d="{icon}"/></svg></i><span><b>{e(fill(t))}</b><span>{e(fill(s))}</span></span></button>''' for t, s, icon in IDEAS)
    msgs = c.execute("select * from (select * from family.messages order by id desc limit 6) m order by id").fetchall()
    said = "".join(f'<div class="bub {"them" if m["from_pip"] else "me"}">{e(m["text"])}</div>' for m in msgs)
    head = (f'<div class="got"><i></i>{e(a)} got your message</div>' if msgs else
            f'<div class="got wait">Waiting for your first message</div>')
    if pip.DRY or not fam["bot_username"]:
        where = (f"Tap an idea and {e(a)} answers right here. This demo box isn't connected to Telegram."
                 if pip.DRY else f"Tap an idea and {e(a)} answers right here. Connect Telegram to talk to {e(a)} from your phone too.")
        tg = ""
    else:
        join = mint(c, "join", 24)  # made on every redraw, so short-lived
        where = f"Open Telegram and send {e(a)} a first message. Here are three ideas. Or tap one and {e(a)} answers right here."
        tg = (f'<a class="btn sunk" href="https://t.me/{e(fam["bot_username"])}?start={join}">'
              f'Open {e(a)} in Telegram</a>')
    body = f'''<div class="two"><div class="col"><h1 class="mvtitle">Say hello to {e(a)}</h1>
<p class="lede">{where}</p>{tg}<div class="ideas">{ideas}</div>
<div class="askform"><label class="sr" for="say">Write to {e(a)}</label><input id="say" name="say" maxlength="200" placeholder="Or write your own" autocomplete="off">
<button class="btn raised" name="send" value="1">Send</button></div></div>
<div class="col"><div class="chat" aria-live="polite">{head}{said}</div></div></div>'''
    return body, button("Next: your first dream", name="next") + quiet("I'll do it later", name="skip")


def step7(c, fam, people, q, error=""):
    dream = c.execute("select * from family.dreams order by id limit 1").fetchone() or {}
    cover = dream.get("cover", 0)
    covers = "".join(f'<label class="cover cover-{i}"><input type="radio" name="cover" value="{i}"'
                     f'{" checked" if i == cover else ""}><span class="sr">Cover {i + 1}</span></label>' for i in range(COVERS))
    sharing = dream.get("sharers") or [p["id"] for p in people]
    sharers = "".join(f'<label class="sharer" title="{e(p["name"])}"><input type="checkbox" name="sharer" value="{p["id"]}"'
                      f'{" checked" if p["id"] in sharing else ""}>{d.chair(p, 44)}</label>' for p in people)
    someday = not dream.get("on_date")
    err = f'<p class="err" role="alert">{e(error)}</p>' if error else ""
    body = f'''<div class="two"><div class="col"><h1 class="mvtitle">Your first dream</h1>
<p class="lede">What would your family love to do together one day? Add one dream to finish moving in.</p>
<div class="covers" role="radiogroup" aria-label="Cover">{covers}</div>
<label class="field big"><span>The dream</span><input name="title" value="{e(dream.get("title") or "")}" placeholder="See the northern lights" maxlength="80"></label>
<div class="fieldlabel">Who shares it?</div><div class="sharers">{sharers}</div>
<div class="when"><span class="fieldlabel">When?</span><label class="pill"><input type="radio" name="when" value="someday"{" checked" if someday else ""}><span>Someday</span></label>
<label class="pill"><input type="radio" name="when" value="date"{"" if someday else " checked"}><span>Pick a date</span></label>
<input type="date" name="on_date" value="{dream.get("on_date") or ""}" class="datefield" aria-label="The date"></div>
<label class="toggle"><span><b>Start a saving goal for it</b><span>You can do this later in Money</span></span>
<input type="checkbox" name="saving" value="1"{" checked" if dream.get("saving") else ""}><i></i></label>{err}</div>
<div class="col laptop-only"><div class="dreamcard"><div class="cover cover-{cover} big"></div><div class="label">Dream · someday</div>
<div class="ptitle">{e(dream.get("title") or "See the northern lights")}</div><div class="who">{"".join(d.chair(p, 28) for p in people)}</div></div></div></div>'''
    return body, button("Add our first dream") + quiet("Skip for now", name="skip")


PAGES = {1: step1, 2: step2, 3: step3, 4: step4, 5: step5, 6: step6, 7: step7}


def get(c, n, q, **extra):
    fam = c.execute("select * from family.household").fetchone()
    people = c.execute("select * from family.people order by sort, id").fetchall()
    body, actions = PAGES[n](c, fam, people, q, **extra)
    return shell(fam, n, max(fam["step"], n), body, actions, live=n == 6)


def post(c, n, form, forms):
    """Saves step n. Returns ("go", where) or ("show", html) to show the step again with a message."""
    fam = c.execute("select * from family.household").fetchone()
    advance = lambda: c.execute("update family.household set step = greatest(step, %s) where id = 1", (n + 1,))
    if n == 2:
        name = form.get("family", "").strip()[:40] or fam["name"]
        c.execute("update family.household set name = %s where id = 1", (name,))
        ids = {p["id"] for p in c.execute("select id from family.people").fetchall()}
        i = 0
        while f"id_{i}" in form:
            pid, nm = form[f"id_{i}"], form.get(f"name_{i}", "").strip()[:40]
            col = form.get(f"colour_{i}") if form.get(f"colour_{i}") in COLOURS else "teal"
            role = form.get(f"role_{i}") if form.get(f"role_{i}") in dict(ROLES) else "adult"
            det = form.get(f"detail_{i}", "").strip()[:40] or None
            if pid.isdecimal() and int(pid) in ids:
                if nm:
                    c.execute("update family.people set name = %s, colour = %s, role = %s, detail = %s where id = %s",
                              (nm, col, role, det, int(pid)))
            elif pid == "new" and nm:
                c.execute("insert into family.people (name, colour, role, detail, sort) values (%s, %s, %s, %s,"
                          " (select coalesce(max(sort), 0) + 1 from family.people))", (nm, col, role, det))
            i += 1
        if form.get("remove", "").isdecimal():
            if c.execute("select count(*) n from family.people").fetchone()["n"] > 1:  # someone always sits at the table
                c.execute("delete from family.people where id = %s", (int(form["remove"]),))
            return "go", "/welcome/2"
        if "add" in form:
            return "go", "/welcome/2?add=1"
        if not c.execute("select 1 from family.people").fetchone():
            return "show", get(c, 2, {"add": 1}, error="Add at least one person to sit at your table.")
    elif n == 3:
        ch = form.get("character") if form.get("character") in CHARACTERS else "Pip"
        name = form.get("agent_name", "").strip()[:20] or "Pip"
        c.execute("update family.household set agent_character = %s, agent_name = %s where id = 1", (ch, name))
    elif n == 4 and form.get("plan") in ("fambox", "own", "rules") and "skip" not in form:
        c.execute("update family.household set ai_mode = %s where id = 1", (form["plan"],))
    elif n == 5:
        if "next" not in form or not fam["bot_username"]:
            code = "".join(form.get("code", "").split())
            if form.get("consent") != "yes":
                return "show", get(c, 5, {}, code=code, problem="consent")
            username, problem = pip.check_code(code, fam["slug"], fam["agent_name"])
            if problem:
                return "show", get(c, 5, {}, code=code, problem=problem)
            c.execute("update family.household set bot_token = %s, bot_username = %s, telegram_consent_at = now() where id = 1",
                      (None if pip.DRY else code, username))
            return "go", "/welcome/5"
    elif n == 6:
        if form.get("say") or "send" in form:  # an empty Send stays on the step
            pip.reply(c, form.get("say", "")[:200], via="web")
            return "go", "/welcome/6"
    elif n == 7 and "skip" not in form:
        title = form.get("title", "").strip()[:80]
        if not title:
            return "show", get(c, 7, {}, error="Give your dream a name, or skip it for now.")
        people = {p["id"] for p in c.execute("select id from family.people").fetchall()}
        sharers = sorted({int(x) for x in forms.get("sharer", []) if x.isdecimal() and int(x) in people})
        on = form.get("on_date") if form.get("when") == "date" and form.get("on_date") else None
        try:
            on = on and date.fromisoformat(on)
        except ValueError:
            on = None
        cover = int(form["cover"]) if form.get("cover", "").isdecimal() and int(form["cover"]) < COVERS else 0
        first = c.execute("select id from family.dreams order by id limit 1").fetchone()
        vals = (title, cover, sharers, on, bool(form.get("saving")))
        if first:
            c.execute("update family.dreams set title = %s, cover = %s, sharers = %s, on_date = %s, saving = %s"
                      " where id = %s", vals + (first["id"],))
        else:
            c.execute("insert into family.dreams (title, cover, sharers, on_date, saving) values (%s, %s, %s, %s, %s)", vals)
    elif n == 8:  # the tour is done or skipped: moved in
        dream = c.execute("select 1 from family.dreams").fetchone()
        name = fam["name"]
        line = f"Welcome home, {name}{'es' if name.endswith('s') else 's'}." + (" Your first dream is on the board." if dream else "")
        c.execute("update family.household set moved_in_at = coalesce(moved_in_at, now()), step = 8,"
                  " agent_line = coalesce(agent_line, %s) where id = 1", (line,))
        return "go", "/"
    advance()
    return "go", "/?tour=1" if n == 7 else f"/welcome/{n + 1}"


# ---------- signed out ----------

def signed_out(c, problem=None, k=None):
    fam = c.execute("select * from family.household").fetchone()
    name, agent = (fam["name"], fam["agent_name"]) if fam else ("", "Pip")
    if k:
        text = "Tap the button to come in on this device. You stay signed in here."
        act = (f'<form method="post" action="/signin"><input type="hidden" name="k" value="{e(k)}">'
               f'<button class="btn raised big">Come in</button></form>')
    else:
        text = (f"That link has been used already, or it is too old. Ask {e(agent)} on Telegram for a new one: send it "
                f"\"sign in\"." if problem else
                f"This is a private family table. To come in, open the link from your welcome message, or ask "
                f"{e(agent)} on Telegram for a new one: send it \"sign in\".")
        act = ""
    who = f"The {e(name)} Family" if name else "FamBox"
    return d.document(who, f'''<main class="mv gate"><div class="gatecard">{LOGO}
<h1 class="mvtitle">{who}</h1><p class="lede">{text}</p>{act}</div></main>''', "welcome")


def session_cookie(c, secure):
    raw = mint(c, "session", 24 * 400)
    return (f"fb_session={raw}; Path=/; Max-Age={400 * 86400}; HttpOnly; SameSite=Lax" + ("; Secure" if secure else ""))


def cookie(headers, name):
    for kv in (headers.get("Cookie") or "").split(";"):
        k, _, v = kv.strip().partition("=")
        if k == name:
            return urllib.parse.unquote(v)
    return None
