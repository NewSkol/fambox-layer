"""The family's rooms beyond the home screen: calendar, money, dreams, routines, photos, and "Something not right?".

Each page is plain HTML from the Table design (README sections 4 and 5, FamBox Dream Board.dc.html), with forms
that post to /do/... (dashboard.act). render() returns (main, the bottom bar's section, title), or None.
"""
import html
import math
import os
import urllib.parse
from datetime import date, datetime, timedelta

import calsync
import dashboard as d
import money
import welcome

e = html.escape


def render(c, s, path, q):
    one = lambda k, default="": (q.get(k) or [default])[0]
    if path == "/calendar":
        return calendar(c, s, one), "calendar", "Calendar"
    if path == "/money":
        return money_room(c, s, one), "money", "Money"
    if path == "/dreams":
        return dreams(s, one), "dreams", "Dreams"
    if path == "/dreams/new":
        return dream_sheet(s, one), "dreams", "A new dream"
    parts = path.strip("/").split("/")
    if len(parts) in (2, 3) and parts[0] == "dreams" and parts[1].isdecimal():
        dr = next((x for x in s["dreams"] if x["id"] == int(parts[1])), None)
        if not dr:
            return None
        if len(parts) == 3:
            return (dream_sheet(s, one, dr), "dreams", "Change the dream") if parts[2] == "edit" else None
        return dream_page(c, s, dr, one), "dreams", dr["title"]
    if path == "/routines":
        return routines(s), "home", "Routines"
    if path == "/photos":
        return photos(s, one), "home", "Photos"
    if path == "/support":
        return support(s, one), "home", "Something not right?"
    if path == "/pip":
        return thinking(c, s, one), "home", f"{s['fam']['agent_name']}'s thinking"
    return None


def room(s, inner, cls=""):
    return f'<main class="room {cls}" data-who="" {d.main_attrs(s)}>{d.header(s, edit_button=False)}{inner}</main>'


def who_select(s, name="person", chosen=None, everyone="Everyone"):
    opts = f'<option value="">{e(everyone)}</option>' + "".join(
        f'<option value="{p["id"]}"{" selected" if p["id"] == chosen else ""}>{e(p["name"])}</option>' for p in s["people"])
    return f'<select name="{name}" class="sel">{opts}</select>'


def problem(text):
    return f'<p class="err" role="alert">{e(text)}</p>'


# ---------- calendar: week and month, a private link for phone calendars ----------

def calendar(c, s, one):
    today, tz = s["today"], s["now"].tzinfo
    focus = d.day_of(one("d")) or d.day_of(one("add")) or today
    month_view = one("view") == "month"
    if month_view:
        first = focus.replace(day=1)
        start = first - timedelta(days=first.weekday())
        end = start + timedelta(days=42)
    else:
        start = focus - timedelta(days=focus.weekday())
        end = start + timedelta(days=7)
    t0 = datetime.combine(start, datetime.min.time(), tz)
    evs = c.execute("select * from family.events where starts_at < %s and coalesce(ends_at, starts_at) >= %s"
                    " order by all_day desc, starts_at", (t0 + (end - start), t0)).fetchall()
    for ev in evs:
        ev["start"] = ev["starts_at"].astimezone(tz)
        ev["end"] = (ev["ends_at"] or ev["starts_at"]).astimezone(tz)
    if month_view:
        prev_, next_ = (first - timedelta(days=1)).replace(day=1), (first + timedelta(days=32)).replace(day=1)
        title = first.strftime("%B %Y")
        cells = []
        for i in range(42):
            day = start + timedelta(days=i)
            todays = d.on_day(evs, day)
            items = "".join(f'<span><i class="pdot {d.colour_of(ev, s["by_id"])}"></i>{e(ev["title"])}</span>' for ev in todays[:3])
            more = f'<span class="note">and {len(todays) - 3} more</span>' if len(todays) > 3 else ""
            cells.append(f'<a href="/calendar?d={day.isoformat()}" class="mday{" now" if day == today else ""}'
                         f'{" out" if day.month != first.month else ""}"><b>{day.day}</b>{items}{more}</a>')
        heads = "".join(f'<span>{n}</span>' for n in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
        grid = f'<div class="month"><div class="mhead">{heads}</div><div class="mgrid">{"".join(cells)}</div></div>'
    else:
        prev_, next_ = start - timedelta(days=7), start + timedelta(days=7)
        title = f'Week of {start.day} {start.strftime("%B")}'
        cols = []
        for i in range(7):
            day = start + timedelta(days=i)
            rows = []
            for ev in d.on_day(evs, day):
                t = "All day" if ev["all_day"] else d.hm(ev["start"])
                future = ev["start"].date() >= today
                cd = (d.post(f'/do/event/{ev["id"]}/countdown', "Stop counting down" if ev["countdown"] else "Count down", "link small")
                      if future else "")
                rows.append(f'<div class="cev" data-key="ce{ev["id"]}"><div class="cline"><b>{t}</b>'
                            f'<i class="pdot {d.colour_of(ev, s["by_id"])}"></i><span>{e(ev["title"])}</span></div>'
                            f'<div class="cact">{cd}<form method="post" action="/do/event/{ev["id"]}/remove" class="act">'
                            f'<button class="link small" data-confirm="Take {e(ev["title"])} off the calendar?">Remove</button></form></div></div>')
            cols.append(f'<div class="cday{" now" if day == today else ""}"><div class="chead"><span>{day.strftime("%a")}</span>'
                        f'<b>{day.day}</b></div>{"".join(rows) or "<div class=empty>Free</div>"}</div>')
        grid = f'<div class="cweek">{"".join(cols)}</div>'
    view = "month" if month_view else "week"
    nav = (f'<div class="calnav"><a class="btn sunk" href="/calendar?view={view}&amp;d={prev_.isoformat()}" aria-label="Earlier">‹</a>'
           f'<a class="btn sunk" href="/calendar?view={view}" >Today</a>'
           f'<a class="btn sunk" href="/calendar?view={view}&amp;d={next_.isoformat()}" aria-label="Later">›</a>'
           f'<div class="themes"><a href="/calendar?d={focus.isoformat()}" class="{"" if month_view else "on"}">Week</a>'
           f'<a href="/calendar?view=month&amp;d={focus.isoformat()}" class="{"on" if month_view else ""}">Month</a></div></div>')
    add_day = d.day_of(one("add")) or focus
    add = f'''<section class="block card" id="add"><h2>Add to the calendar</h2>{problem("Give it a name and a date; a time like 16:30, or none for all day.") if one("problem") else ""}
<form method="post" action="/do/event/0/add" class="formrow">
<label class="field"><span>What</span><input name="title" maxlength="120" placeholder="Swimming" required></label>
<label class="field"><span>Day</span><input type="date" name="date" value="{add_day.isoformat()}" required></label>
<label class="field short"><span>Time <i>(none: all day)</i></span><input name="time" value="{e(one("at"))}" placeholder="16:30" inputmode="numeric"></label>
<label class="field"><span>Who</span>{who_select(s)}</label>
<label class="check"><input type="checkbox" name="countdown" value="1"> Count down to it</label>
<button class="btn raised">Add to the calendar</button></form>
<p class="note">Or tell {e(s["fam"]["agent_name"])}: "Add swimming for Lena on Friday at 5".</p></section>'''
    return room(s, f'<div class="roomhead"><h1 class="display">{e(title)}</h1>{nav}</div>{grid}{add}{both_ways(c, s, one)}{phone_feed(s)}', "calroom")


CAL_PROBLEMS = {"icloud-empty": "Write your Apple ID and the app-specific password.",
                "icloud-signin": "Apple didn't accept that. Check the Apple ID, or make a new app-specific password.",
                "icloud-offline": "We couldn't reach iCloud just now. Wait a minute and try again.",
                "icloud-none": "That Apple ID has no calendar we can add events to.",
                "google": "Google didn't connect. Try again, and allow FamBox to see and change your calendar's events.",
                "google-off": "Google connection not switched on yet."}


def both_ways(c, s, one):
    """Two-way calendars (calsync.py). Not in the design: drawn in the Table system like "Pip's thinking" (one card,
    sunk fields, one button each). The password or Google's key is never shown again."""
    accts = {a["kind"]: a for a in c.execute("select * from family.cal_accounts").fetchall()}
    now = datetime.now(calsync.UTC)
    rows = []
    for kind in ("icloud", "google"):
        a, name = accts.get(kind), calsync.NAMES[kind]
        stop = lambda words: (f'<form method="post" action="/do/cal/{a["id"]}/stop" class="act"><button class="quietlink small" '
                              f'data-confirm="Stop keeping the table and {name} in step? Events that came from {name} leave the table; '
                              f'nothing changes in {name}.">{words}</button></form>')
        if a and a["calendar"]:
            mins = int((now - a["synced_at"]).total_seconds() // 60) if a["synced_at"] else None
            line = (calsync.problem_line(a) or ("Getting in step…" if mins is None else "In step just now" if mins < 2 else
                    f"In step {mins} minutes ago" if mins < 60 else f'In step at {d.hm(a["synced_at"].astimezone(s["now"].tzinfo))}'))
            again = ""
            if a["problem"] == "signin":
                again = (icloud_form(a["login"], "Sign in again") if kind == "icloud" else
                         '<a class="btn raised" href="/cal/google/start">Sign in to Google again</a>')
            who = f' · {e(a["login"])}' if a["login"] else ""
            rows.append(f'<div class="calrow"><div class="feedrow"><b>{name}</b><span>“{e(a["calendar_name"] or "")}”{who}</span>'
                        f'<span class="note">{e(line)}</span>{stop("Stop")}</div>{again}</div>')
        elif a:
            fav = next((x["url"] for x in a["choices"] if x["name"].lower() in ("family", "pere", "perekond", "home", "kodu")), None)
            cards = "".join(f'<label class="plan"><input type="radio" name="calendar" value="{e(x["url"])}" required'
                            f'{" checked" if x["url"] == fav else ""}><b>{e(x["name"])}</b></label>' for x in a["choices"])
            rows.append(f'<div class="calrow"><h3>Which {name} calendar should the table share?</h3>'
                        f'<form method="post" action="/do/cal/{a["id"]}/pick" class="calpick"><div class="plans">{cards}</div>'
                        f'<button class="btn raised">Use this calendar</button></form>{stop("Cancel")}</div>')
        elif kind == "icloud":
            rows.append(f'<details class="calrow calconnect"><summary class="btn sunk">Connect iCloud</summary>{icloud_form()}</details>')
        elif calsync.google_ready():
            rows.append('<div class="calrow"><a class="btn sunk" href="/cal/google/start">Connect Google</a></div>')
        else:
            rows.append('<div class="calrow"><div class="feedrow"><b>Google</b><span class="note">Google connection not switched on yet.</span></div></div>')
    if os.environ.get("FAMBOX_DEMO"):
        rows = ['<p class="note">On the demo table this is switched off. On your own table you connect iCloud or Google here.</p>']
    said = problem(CAL_PROBLEMS[one("problem")]) if one("problem") in CAL_PROBLEMS else ""
    return (f'<section class="block card" id="both"><h2>Both ways with your phones</h2>'
            f'<p class="lede">Connect the calendar your family already uses. What you add here appears there, and what you add, '
            f'move or remove there appears here, within a few minutes.</p>{said}{"".join(rows)}</section>')


def icloud_form(login="", label="Connect iCloud"):
    return (f'<form method="post" action="/do/cal/0/icloud" class="formrow">'
            f'<label class="field wide"><span>Apple ID</span><input name="login" type="email" autocomplete="username" required '
            f'value="{e(login)}"></label><label class="field wide"><span>App-specific password</span><input name="password" '
            f'type="password" autocomplete="off" spellcheck="false" required placeholder="xxxx-xxxx-xxxx-xxxx"></label>'
            f'<button class="btn raised">{e(label)}</button></form>'
            '<p class="note">Apple asks for a special password for this, not your usual one. On account.apple.com, sign in, open '
            'Sign-In and Security, choose App-Specific Passwords, make one called FamBox and paste it here. It stays on your box '
            'and is never shown again. Then choose the calendar to share, for example "Family".</p>')


def phone_feed(s):
    fam = s["fam"]
    if not fam["feed_key"]:
        return (f'<section class="block card" id="phones"><h2>On your phones</h2><p class="lede">Your family\'s calendar can '
                f'show in the calendar on each phone. It updates by itself about every hour.</p>'
                f'{d.post("/do/feed/0/new", "Make a private link", "btn sunk")}</section>')
    addr = welcome.address(fam)
    url = lambda pid=None: f'{addr}/feed/{fam["feed_key"]}' + (f'/{pid}' if pid else "") + ".ics"
    rows = [("Everyone", url())] + [(p["name"], url(p["id"])) for p in s["people"] if p["role"] != "pet"]
    links = "".join(f'<div class="feedrow"><b>{e(n)}</b><a class="link" href="webcal://{e(u)}">Add to this phone</a>'
                    f'<input readonly value="https://{e(u)}" aria-label="The link for {e(n)}" onfocus="this.select()"></div>'
                    for n, u in rows)
    return f'''<section class="block card" id="phones"><h2>On your phones</h2>
<p class="lede">Tap "Add to this phone" on an iPhone. For Google Calendar, copy the link and add it under "From URL". Only people with the link see the calendar; keep it in the family.</p>
{links}{d.post("/do/feed/0/new", "Make new links (the old ones stop working)", "quietlink small")}</section>'''


# ---------- money: every euro has a pot. One big number, one bar, one line to write in; never red ----------

def money_room(c, s, one):
    m, today = s["month"], s["today"]
    m0, m1 = d.month_bounds(today)
    review = c.execute("select * from family.bank_lines where state = 'new' order by on_date, id").fetchall()
    agent = e(s["fam"]["agent_name"])
    days = f'{m["to_go"]} day{"s" if m["to_go"] != 1 else ""} to go' if m["to_go"] else "the last day"
    if m["base"] is None:
        hero = (f'<div class="label">{m["name"]}</div><h1 class="display">What comes in, what is left.</h1>'
                f'<p class="lede">Write in what came in this month, like "salary 3200". Then each bill, each pot and what you spend, '
                f'and the big number here is always what is left to live on.</p>')
    else:
        hero = (f'<div class="label">{m["name"]} · {days}</div><div class="mbig"><span class="big">{d.eur(round(abs(m["left"])))}</span>'
                f'<span class="words">{d.left_words(m)}</span></div>{d.month_bar(m, label=True)}')
    said = problem("I couldn't read that line.") if one("problem") == "line" else e(one("said"))  # never trust the address
    line = f'''<form method="post" action="/do/money/0/line" class="mlinein" data-line>
<label class="sr" for="mtext">Write it in one line</label><input id="mtext" name="text" value="{e(one("text"))}" maxlength="200" autocomplete="off"
placeholder="40 groceries, paid electricity, salary 3200" enterkeyhint="done">
<button class="btn {"sunk" if review else "raised"}">Write it</button>
<p class="mpreview" aria-live="polite" data-line-out>{said}</p></form>'''
    # bills: Paid / Due in 3 days / Late at a glance, one tap
    states = sorted(((money.bill_state(b, today), b) for b in s["bills"]), key=lambda x: (x[0][0] == "paid", x[0][2], x[1]["id"]))
    paid_n = sum(1 for st, _ in states if st[0] == "paid")
    brows = "".join(
        f'<div class="bill {st[0]}" data-key="b{b["id"]}-{st[0]}"><i class="bdot" aria-hidden="true"></i><span class="bname">{e(b["title"])}'
        f'<span class="bstate">{st[1]}{"" if st[0] == "paid" else f" · {st[2].day} {st[2]:%b}"}</span></span><b class="bamt">{d.eur(b["amount"])}</b>'
        + (d.post(f"/do/bill/{b['id']}/unpaid", "Not yet", "quietlink small", label=None) if st[0] == "paid"
           else d.post(f"/do/bill/{b['id']}/paid", "Paid", "btn sunk small")) + "</div>"
        for st, b in states)
    bills = f'''<section class="block card" id="bills"><div class="top"><h2>Bills</h2><span class="note">{paid_n} of {len(states)} paid</span></div>
{problem("A bill needs a name, an amount and a day of the month (1 to 31).") if one("problem") == "bill" else ""}
<div class="billrows">{brows or '<div class="empty">No bills yet. Add the ones that come every month.</div>'}</div>
<details class="more"><summary class="quietlink small">Add a bill</summary><form method="post" action="/do/bill/0/add" class="formrow">
<label class="field"><span>Bill</span><input name="title" maxlength="60" placeholder="Electricity" required></label>
<label class="field short"><span>Euros</span><input name="amount" inputmode="decimal" placeholder="86" required></label>
<label class="field short"><span>Day of the month</span><input name="day" inputmode="numeric" placeholder="9" required></label><button class="btn sunk">Add the bill</button></form>
<details class="more remove"><summary class="quietlink small">Stop a bill</summary><div class="chips">{"".join(
    f'<form method="post" action="/do/bill/{b["id"]}/remove" class="act"><button class="chip" data-confirm="Stop reminding about {e(b["title"])}?">{e(b["title"])} ×</button></form>'
    for _, b in states)}</div></details></details></section>'''
    pots = "".join(pot_card(s, g, today) for g in s["goals"])
    waiting = "".join(f'''<form method="post" action="/do/goal/0/add" class="formrow pot"><input type="hidden" name="dream" value="{x["id"]}">
<input type="hidden" name="title" value="{e(d.fund_name(x["title"]))}"><input type="hidden" name="by" value="{x["on_date"] or ""}">
<label class="field"><span>{e(x["title"])}: how much will it cost?</span><input name="target" inputmode="decimal" placeholder="2400"></label>
<button class="btn sunk">Make a pot</button></form>''' for x in s["dreams"] if x["saving"] and not x["lived_on"] and not d.goal_of(s, x))
    put_away = sum(float(g["saved"]) for g in s["goals"])
    pots_html = f'''<section class="block card" id="pots"><div class="top"><h2>Pots</h2><span class="note">{d.eur(put_away)} put away</span></div>
{problem("Write the amount as a number, like 2400.") if one("problem") in ("goal", "save") else ""}
{pots or '<div class="empty">A pot is money kept for one thing: a dream, the car service, a rainy day.</div>'}{waiting}
<details class="more"><summary class="quietlink small">A new pot</summary><form method="post" action="/do/goal/0/add" class="formrow">
<label class="field"><span>For</span><input name="title" maxlength="60" placeholder="Car service" required></label>
<label class="field short"><span>Euros</span><input name="target" inputmode="decimal" placeholder="400" required></label>
<label class="field"><span>By <i>(if there is a date)</i></span><input type="date" name="by"></label>
<label class="field"><span>Where the money is</span><input name="place" list="places" maxlength="60" placeholder="Cash box at home"></label>
<button class="btn sunk">Make the pot</button></form></details>{places()}</section>'''
    spent = c.execute("(select 's' k, id, amount, note, spent_on d from family.spending where spent_on >= %s and spent_on < %s)"
                      " union all (select 'i', id, amount, note, on_date from family.income where on_date >= %s and on_date < %s)"
                      " order by d desc, id desc", (m0, m1, m0, m1)).fetchall()
    row = lambda x: (f'<div class="line {"in" if x["k"] == "i" else ""}" data-key="{x["k"]}{x["id"]}"><b>{x["d"].day} {x["d"]:%b}</b>'
                     f'<span>{e(x["note"] or "Spending")}</span><i class="dots"></i><b>{"+" if x["k"] == "i" else ""}{d.eur(x["amount"])}</b>'
                     f'{d.post(f"/do/{"income" if x["k"] == "i" else "spend"}/{x["id"]}/remove", "×", "x")}</div>')
    more = (f'<details class="more"><summary class="quietlink small">All {len(spent)}</summary><div class="lines">{"".join(row(x) for x in spent[8:])}</div></details>'
            if len(spent) > 8 else "")
    lines = f'''<section class="block card" id="lines"><div class="top"><h2>This month</h2><span class="note">newest first</span></div>
<div class="lines">{"".join(row(x) for x in spent[:8]) or '<div class="empty">Nothing written in this month yet.</div>'}</div>{more}</section>'''
    body = (f'<div class="roomhead mhero">{hero}</div>{line}{bank_review(s, review, one)}'
            f'<div class="mcols">{bills}{pots_html}{lines}{statement_card(s, one)}</div>'
            f'{kakeibo_card(c, s, one)}{money_settings(s, m)}')
    return room(s, body, "moneyroom")


def places():
    return '<datalist id="places">' + "".join(f'<option value="{p}">' for p in money.PLACES) + "</datalist>"


def pot_card(s, g, today, dream_link=True):
    each = money.per_month(g["target"], g["saved"], g["by_date"], today)
    frac = float(g["saved"]) / float(g["target"]) if g["target"] else 0
    dream = next((x for x in s["dreams"] if x["id"] == g["dream_id"]), None)
    plan = (f'{d.eur(each)} a month gets you there by {g["by_date"]:%B}' if each else
            "All saved" if g["saved"] >= g["target"] else f'by {g["by_date"]:%B %Y}' if g["by_date"] else "")
    link = f'<a class="note" href="/dreams/{dream["id"]}">{e(dream["title"])}</a>' if dream and dream_link else ""
    quick = [x for x in dict.fromkeys([each, 20]) if x] if g["saved"] < g["target"] else []
    puts = "".join(f'<form method="post" action="/do/goal/{g["id"]}/save" class="act"><input type="hidden" name="amount" value="{x}">'
                   f'<button class="btn sunk small">+ {d.eur(x)}</button></form>' for x in quick)
    return f'''<div class="pot" data-key="g{g["id"]}"><div class="top"><b>{e(g["title"])}</b>{link}</div>
<div class="pbar" aria-hidden="true"><i style="width:{min(100, frac * 100):.1f}%"></i></div>
<div class="pline"><span><b>{d.eur(g["saved"])}</b> of {d.eur(g["target"])}</span><span class="place">{e(g["place"] or "Where is it kept?")}</span></div>
<div class="pacts">{puts}<details class="more"><summary class="quietlink small">More</summary>
<form method="post" action="/do/goal/{g["id"]}/out" class="formrow"><label class="field short"><span>Take out</span><input name="amount" inputmode="decimal" placeholder="50"></label><button class="btn sunk">Take it out</button></form>
<form method="post" action="/do/goal/{g["id"]}/place" class="formrow"><label class="field"><span>Where the money is</span><input name="place" list="places" value="{e(g["place"] or "")}" maxlength="60" placeholder="Cash box at home"></label><button class="btn sunk">Save</button></form>
<form method="post" action="/do/goal/{g["id"]}/remove" class="act"><button class="quietlink small" data-confirm="Remove the {e(g["title"])}? The dream stays.">Remove this pot</button></form>
</details></div>{f'<div class="note">{plan}</div>' if plan else ""}</div>'''


def bank_review(s, review, one):
    """Lines from a bank statement, each with what it most likely is; one tap accepts them all."""
    if not review:
        if one("read") == "0":
            return (f'<section class="block card review" id="review"><p class="lede">Nothing new in that file: '
                    f'{e(one("seen"))} lines were already on your table.</p></section>')
        return ""
    bills = s["bills"]
    opts = lambda ln: (
        [("spend", "Living")] + [(f"bill:{b['id']}", f"Bill: {b['title']}") for b in bills]
        + [(f"pot_in:{g['id']}", f"Into {g['title']}") for g in s["goals"]] + [(f"pot_out:{g['id']}", f"Out of {g['title']}") for g in s["goals"]]
        if ln["amount"] < 0 else [("income", "Came in")] + [(f"pot_out:{g['id']}", f"Out of {g['title']}") for g in s["goals"]]
        + [("spend", "Living")]) + [("skip", "Leave out")]
    def chosen(ln):
        p = ln["proposal"] or {}
        return f'{p.get("do")}:{p["id"]}' if p.get("id") else p.get("do", "skip")
    rows = "".join(
        f'<div class="bline" data-key="bl{ln["id"]}"><b>{ln["on_date"].day} {ln["on_date"]:%b}</b><span class="btext">{e(ln["text"])}'
        f'{f"<i>{e(why)}</i>" if (why := (ln["proposal"] or {}).get("why")) else ""}'
        f'</span><b class="bamt">{"−" if ln["amount"] < 0 else "+"}{d.eur(abs(ln["amount"]))}</b>'
        f'<select name="a{ln["id"]}" class="sel" aria-label="What it is">'
        + "".join(f'<option value="{v}"{" selected" if v == chosen(ln) else ""}>{e(w)}</option>' for v, w in opts(ln))
        + "</select></div>" for ln in review)
    return f'''<section class="block card review" id="review"><div class="top"><h2>From your bank</h2><span class="note">{len(review)} new line{"s" if len(review) != 1 else ""}</span></div>
<p class="lede">Matched to your bills and pots. Change any that is wrong, then accept them.</p>
<form method="post" action="/do/statement/0/accept"><div class="blines">{rows}</div>
<div class="row"><button class="btn raised">Accept {"all " + str(len(review)) if len(review) > 1 else "it"}</button>
<button class="quietlink small" name="skip" value="1" data-confirm="Leave all {len(review)} out?">Leave them all out</button></div></form></section>'''


def statement_card(s, one):
    agent = e(s["fam"]["agent_name"])
    bad = problem("I couldn't read that file. Download the statement as CSV from your internet bank (LHV: Statement, then CSV; "
                  "Revolut: Statement, then Excel or CSV) and try again.") if one("problem") == "file" else ""
    return f'''<section class="block card" id="statement"><div class="top"><h2>From the bank</h2></div>{bad}
<form method="post" action="/do/statement/0/add" enctype="multipart/form-data" class="dropzone" data-autosend>
<label><b>Drop a statement file here</b><span>An LHV or Revolut statement (CSV). {agent} matches the lines to your bills and pots; you accept them with one tap.</span>
<input type="file" name="statement" accept=".csv,.xml,.txt,.json,text/csv,application/xml"></label><button class="btn sunk nojs">Read it</button></form>
<p class="note">Or send the file to {agent} on Telegram. Card numbers are never kept.</p></section>'''


def kakeibo_card(c, s, one):
    """The month's four questions (Kakeibo): from three days before the month ends until a week into the next."""
    today = s["today"]
    m0 = today.replace(day=1)
    ask = m0 if money.next_month(today) - today <= timedelta(days=3) else (m0 - timedelta(days=1)).replace(day=1) if today.day <= 7 else None
    rows = c.execute("select * from family.kakeibo where answer is not null order by month desc limit 1").fetchall()
    last = rows[0] if rows else None
    out = ""
    if ask and not (last and last["month"] == ask):
        out += f'''<section class="block card kakeibo" id="kakeibo"><div class="top"><h2>Four questions for {ask:%B}</h2></div>
<ol class="kq"><li>How much came in?</li><li>How much do we want to save?</li><li>How much did we spend?</li><li>How can we do better next month?</li></ol>
<form method="post" action="/do/kakeibo/0/answer" class="supportform"><input type="hidden" name="month" value="{ask.isoformat()}">
<label class="sr" for="kk">In your words</label><textarea id="kk" name="text" rows="3" maxlength="2000" placeholder="In your words, all four at once" required></textarea>
<button class="btn sunk">Keep it</button></form></section>'''
    if last:
        f = last["facts"] or {}
        nums = f'{d.eur(f.get("came", 0))} came in; {d.eur(f.get("bills", 0))} bills, {d.eur(f.get("living", 0))} living, {d.eur(f.get("dreams", 0))} into pots.' if f else ""
        out += (f'<section class="block card kakeibo said"><div class="label">{last["month"]:%B}, in your words</div>'
                f'<p class="lede">{e(last["answer"])}</p><p class="note">{nums}</p></section>')
    return out


def money_settings(s, m):
    fam = s["fam"]
    days = "".join(f'<option value="{n}"{" selected" if fam["payday"] == n else ""}>the {n}{money.suffix(n)}</option>' for n in range(1, 32))
    plan = ""
    if m["came"] == 0:
        plan = f'''<form method="post" action="/do/money/0/plan" class="formrow"><label class="field short"><span>Until something is written in: our plan</span>
<input name="amount" inputmode="decimal" value="{d.eur(m["plan"])[1:].replace(",", "") if m["plan"] is not None else ""}" placeholder="2600"></label><button class="btn sunk">Save</button></form>'''
    return f'''<section class="msettings" id="payday"><form method="post" action="/do/money/0/payday" class="formrow">
<label class="field short"><span>Payday</span><select name="day" class="sel"><option value="">Not set</option>{days}</select></label><button class="btn sunk">Save</button></form>
<p class="note">On payday {e(fam["agent_name"])} asks if you want to save first, with an amount for each pot. On the month's last evening: four short questions.</p>{plan}</section>'''


# ---------- dreams: the board, the add sheet, the dream page, the two celebrations ----------

def sharer_words(s, dr):
    ids = dr["sharers"] or []
    ppl = [p for p in s["people"] if p["id"] in ids]
    if not ppl:
        return "Nobody yet"
    humans = [p for p in s["people"] if p["role"] != "pet"]
    if len(ppl) == len(s["people"]):
        return "All of you"
    if set(p["id"] for p in humans) == set(ids):
        pets = [p["name"] for p in s["people"] if p["role"] == "pet"]
        return "Everyone" + (f" except {' and '.join(pets)}" if pets else "")
    names = [p["name"] for p in ppl]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def chairs_of(s, dr, size=30):
    return '<span class="stack">' + "".join(d.chair(p, size) for p in s["people"] if p["id"] in (dr["sharers"] or [])) + "</span>"


def tear(n, small=False):
    if n is None:
        return ""
    return f'<div class="tearoff{" small" if small else ""}"><b>{n}</b><span>{"day" if n == 1 else "days"}</span></div>'


def dreams(s, one):
    who = int(one("who")) if one("who").isdecimal() else None
    pick = [dr for dr in s["dreams"] if who is None or who in (dr["sharers"] or [])]
    chips = [f'<a href="/dreams" class="pchip ink{" on" if who is None else ""}"><i></i>Everyone</a>']
    for p in s["people"]:
        fg = "light" if p["colour"] in d.LIGHT_TEXT else "dark"
        chips.append(f'<a href="/dreams?who={p["id"]}" class="pchip{" on" if who == p["id"] else ""}">'
                     f'<i class="c-{p["colour"]} {fg}">{e(p["name"][0])}</i>{e(p["name"])}</a>')
    today = s["today"]
    dated = [dr for dr in pick if dr["on_date"] and not dr["lived_on"]]
    someday = [dr for dr in pick if not dr["on_date"] and not dr["lived_on"]]
    lived = [dr for dr in pick if dr["lived_on"]]
    cards = []
    for dr in dated:
        g = d.goal_of(s, dr)
        when = f'{dr["on_date"].day} {dr["on_date"]:%B %Y}' + (f' · {e(dr["note"])}' if dr["note"] else "")
        money = (f'<div class="dmoney">{d.settings(g)}<div class="note">{d.goal_words(g)} · {e(g["place"] or g["title"])}</div>'
                 f'{d.points_ring(s, dr, 30)}</div>' if g else f'<div class="dmoney">{d.points_ring(s, dr, 30)}</div>' if dr.get("points_for") else "")
        n = d.days_to(dr["on_date"], today)
        cards.append(f'''<a class="dcard" href="/dreams/{dr["id"]}" data-key="dc{dr["id"]}">{d.dream_cover(dr)}
<div class="dbody"><div class="dtop"><h3>{e(dr["title"])}</h3>{tear(n if n >= 0 else None)}</div><div class="note">{when}</div>
<div class="who">{chairs_of(s, dr, 26)}<span class="note">{e(sharer_words(s, dr))}</span></div>{money}</div></a>''')
    somes = "".join(f'''<a class="scard" href="/dreams/{dr["id"]}" data-key="dc{dr["id"]}">{d.dream_cover(dr)}
<h3>{e(dr["title"])}</h3><div class="who">{chairs_of(s, dr, 22)}<span class="note">{e(dr["note"] or sharer_words(s, dr))}</span></div>
{d.points_ring(s, dr, 28)}</a>''' for dr in someday)
    pins = "".join(f'''<a class="pinned" href="/dreams/{dr["id"]}" style="--rot:{(-3, 2, -1.5, 3)[i % 4]}deg" data-key="dl{dr["id"]}">
<i class="pinhead c-{d_colour(s, dr)}"></i>{d.dream_cover(dr)}<h3>{e(dr["title"])}</h3><div class="note">{dr["lived_on"]:%B %Y}</div></a>''' for i, dr in enumerate(lived))
    body = f'''<div class="roomhead dreamhead"><h1 class="display">Dreams</h1><nav class="pchips" aria-label="Whose dreams">{"".join(chips)}</nav>
<a class="btn raised" href="/dreams/new">Add a dream</a></div>
<section class="dsec"><h2 class="title">With a date <span class="note">Counting down</span></h2><div class="dated">{"".join(cards) or '<div class="empty">No dates yet.</div>'}</div></section>
<section class="dsec"><h2 class="title">Someday <span class="note">No date yet, and that's fine</span></h2><div class="somedays">{somes or '<div class="empty">Nothing here yet.</div>'}</div></section>
<section class="dsec lived"><h2 class="title">Dreams we lived <span class="note">Pinned up like photos on the fridge</span></h2><div class="pins">{pins or '<div class="empty">When a dream comes true, it is pinned up here.</div>'}</div></section>'''
    return room(s, body, "dreamroom")


def d_colour(s, dr):
    first = next((p for p in s["people"] if p["id"] in (dr["sharers"] or [])), None)
    return first["colour"] if first else "ink"


def dream_sheet(s, one, dr=None):
    """Add a dream (or change one). Pip can fill it from one sentence."""
    val = lambda k, default="": one(k, default) if one(k, "") != "" or not dr else default
    title = one("title") or (dr["title"] if dr else "")
    cover = int(one("cover")) if one("cover").isdecimal() and int(one("cover")) < d.DREAM_COVERS else (dr["cover"] if dr else 0)
    if one("sharers") or one("problem"):
        sharing = [int(x) for x in one("sharers").split(",") if x.isdecimal()]
    else:
        sharing = dr["sharers"] if dr else [p["id"] for p in s["people"] if p["role"] != "pet"]
    when_date = one("when") == "date" if one("when") else bool(dr and dr["on_date"])
    on = one("on_date") or (dr["on_date"].isoformat() if dr and dr["on_date"] else "")
    g = d.goal_of(s, dr) if dr else None
    saving = bool(one("saving")) if one("title") or one("problem") else (dr["saving"] if dr else True)
    target = one("target") or (d.eur(g["target"])[1:].replace(",", "") if g else "")
    note = one("note") or (dr["note"] or "" if dr else "")
    covers = "".join(f'<label class="cover cover-{i}"><input type="radio" name="cover" value="{i}"{" checked" if i == cover else ""}>'
                     f'<span class="sr">Picture {i + 1}</span></label>' for i in range(d.DREAM_COVERS))
    sharers = "".join(f'<label class="sharer" title="{e(p["name"])}"><input type="checkbox" name="sharer" value="{p["id"]}"'
                      f'{" checked" if p["id"] in sharing else ""}>{d.chair(p, 44)}</label>' for p in s["people"])
    if dr and dr["photo_id"]:  # the family's photo is the cover; it is changed on the dream's page
        pick = (f'{d.dream_cover(dr, "hero")}<input type="hidden" name="cover" value="{cover}">'
                f'<p class="note">Your photo is the cover. Change it on the dream\'s page.</p>')
    else:
        pick = (f'<div class="cover cover-{cover} hero" data-cover-preview></div>'
                f'<div class="covers" role="radiogroup" aria-label="Picture">{covers}</div>')
    agent = e(s["fam"]["agent_name"])
    fill = "" if dr else f'''<form method="post" action="/do/dream/0/fill" class="askform act-plain"><label class="sr" for="fill">Tell {agent} in one sentence</label>
<input id="fill" name="text" maxlength="200" placeholder="Or tell {agent}: We want to go to Lapland next winter" autocomplete="off"><button class="btn sunk">Fill it in</button></form>'''
    action = f"/do/dream/{dr['id']}/edit" if dr else "/do/dream/0/add"
    remove = (f'<form method="post" action="/do/dream/{dr["id"]}/remove" class="inline"><button class="quietlink small" '
              f'data-confirm="Remove this dream from the board?">Remove this dream</button></form>' if dr else "")
    body = f'''<div class="sheet"><div class="sheethead"><h1 class="display">{"Change the dream" if dr else "A new dream"}</h1>
<a class="quietlink" href="{f"/dreams/{dr['id']}" if dr else "/dreams"}">Cancel</a></div>{fill}
<form method="post" action="{action}" class="dreamform" id="dreamform">
{pick}
{problem(one("problem")) if one("problem") else ""}
<label class="field"><span>The dream</span><input name="title" value="{e(title)}" maxlength="80" placeholder="Lapland in winter" required></label>
<div class="fieldlabel">Who shares it?</div><div class="sharers">{sharers}</div>
<div class="fieldlabel">When?</div><div class="when"><label class="pill"><input type="radio" name="when" value="someday"{"" if when_date else " checked"}><span>Someday</span></label>
<label class="pill"><input type="radio" name="when" value="date"{" checked" if when_date else ""}><span>On a date</span></label>
<input type="date" name="on_date" value="{e(on)}" class="datefield" aria-label="The date" data-monthly-date></div>
<label class="field"><span>A few words <i>(optional)</i></span><input name="note" value="{e(note)}" maxlength="80" placeholder="5 nights"></label>
<div class="block card getthere"><label class="toggle"><span><b>What would get us there?</b><span>A saving goal in Money</span></span>
<input type="checkbox" name="saving" value="1"{" checked" if saving else ""}><i></i></label>
<div class="line"><label class="sr" for="target">Saving goal in euros</label><b>€</b><input id="target" name="target" value="{e(target)}" inputmode="decimal" placeholder="2400" data-monthly-amount>
<i class="dots"></i><span class="note" data-monthly></span></div></div>
<div class="actions"><button class="btn raised big">{"Save the dream" if dr else "Add our dream"}</button>{remove}</div></form></div>'''
    return room(s, body, "sheetroom")


def dream_page(c, s, dr, one):
    today = s["today"]
    g = d.goal_of(s, dr)
    n = d.days_to(dr["on_date"], today) if dr["on_date"] and dr["on_date"] >= today else None
    label = f'{dr["on_date"].day} {dr["on_date"]:%B %Y}' if dr["on_date"] else "Someday"
    count = len([p for p in s["people"] if p["id"] in (dr["sharers"] or [])])
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}.get(count, str(count))
    shared = (f"Shared by {words} of you" if count > 1 else f"{sharer_words(s, dr)}'s dream") if count else "Nobody shares it yet"
    if g:
        each = money.per_month(g["target"], g["saved"], g["by_date"], today)
        quick = "" if dr["lived_on"] or g["saved"] >= g["target"] else (
            f'<form method="post" action="/do/goal/{g["id"]}/save" class="act"><input type="hidden" name="amount" value="{each or 20}">'
            f'<button class="btn sunk small">+ {d.eur(each or 20)}</button></form>')
        plan = (f'{d.eur(each)} a month gets you there by {g["by_date"]:%B}' if each else
                "All saved" if g["saved"] >= g["target"] else "")
        fund = (f'<div class="block card fund"><div class="fring">{d.ring_svg(float(g["saved"]) / float(g["target"]), 64, "money")}</div>'
                f'<div class="fwords"><div><b>{d.eur(g["saved"])}</b> of {d.eur(g["target"])}</div>'
                f'<div class="note">{e(g["place"] or "Where is it kept? Say in Money")} · <a class="link" href="/money#pots">{e(g["title"])}</a></div>'
                f'{f"<div class=note>{plan}</div>" if plan else ""}</div>{quick}</div>')
    elif dr["saving"] and not dr["lived_on"]:
        fund = f'''<form method="post" action="/do/goal/0/add" class="block card fund formrow"><input type="hidden" name="dream" value="{dr["id"]}">
<input type="hidden" name="title" value="{e(d.fund_name(dr["title"]))}"><input type="hidden" name="by" value="{dr["on_date"] or ""}">
<label class="field"><span>How much will it cost, roughly?</span><input name="target" inputmode="decimal" placeholder="2400"></label>
<label class="field"><span>Where will the money be?</span><input name="place" list="places" maxlength="60" placeholder="Cash box at home"></label>
<button class="btn sunk">Make a pot</button>{places()}</form>'''
    else:
        fund = ""
    pts = d.dream_points(s, dr)
    kids = [p for p in s["people"] if p["role"] == "child"]
    if pts:
        p, have, want = pts
        fund += (f'<div class="block card fund points c-{p["colour"]}"><div class="fring">{d.ring_svg(have / want, 64, "pts")}</div>'
                 f'<div class="fwords"><div><b>{e(p["name"])}: {have}</b> of {want} points</div>'
                 f'<div class="note">Jobs and routine steps with points add up. {want - have} to go.</div></div></div>')
    elif kids and not dr["lived_on"]:
        opts = "".join(f'<option value="{p["id"]}"{" selected" if p["id"] in (dr["sharers"] or []) else ""}>{e(p["name"])}</option>' for p in kids)
        fund += f'''<details class="more earn"><summary class="quietlink small">A child earns this with jobs</summary>
<form method="post" action="/do/dream/{dr["id"]}/points" class="formrow"><label class="field"><span>Who</span><select name="person" class="sel">{opts}</select></label>
<label class="field short"><span>Points</span><input name="points" inputmode="numeric" placeholder="300" required></label><button class="btn sunk">Start counting</button></form></details>'''
    steps = "".join(
        f'<form method="post" action="/do/dream/{dr["id"]}/step-{i}" class="task big dstep{" done" if st.get("done") else ""}" data-key="ds{dr["id"]}-{i}">'
        f'<button class="tick c-{s["by_id"][st["person"]]["colour"] if st.get("person") in s["by_id"] else "ink"} '
        f'{"dark" if st.get("person") in s["by_id"] and s["by_id"][st["person"]]["colour"] not in d.LIGHT_TEXT else "light"}" '
        f'title="{"Not done yet" if st.get("done") else "Done"}: {e(st.get("t", ""))}"></button><span>{e(st.get("t", ""))}</span></form>'
        for i, st in enumerate(dr["steps"] or []))
    add_step = f'''<form method="post" action="/do/dream/{dr["id"]}/step-add" class="formrow act"><label class="field"><span class="sr">A step</span>
<input id="steptext" name="t" maxlength="80" placeholder="Book the cabin"></label>{who_select(s, everyone="Anyone")}<button class="btn sunk">Add a step</button></form>'''
    drawn = d.post(f"/do/dream/{dr['id']}/drawn", "Use a drawn cover", "quietlink small") if dr["photo_id"] else ""
    if dr["lived_on"]:
        acts = (f'<p class="lede">It came true in {dr["lived_on"]:%B %Y}.</p>'
                f'{d.post(f"/do/dream/{dr["id"]}/again", "Not quite yet after all", "quietlink small")}{drawn}')
    else:
        acts = (f'<form method="post" action="/do/dream/{dr["id"]}/lived" class="inline"><button class="btn sunk big">It came true</button></form>'
                f'<a class="quietlink" href="/dreams/{dr["id"]}/edit">Edit</a>{drawn}')
    cover_btn = photo_form(dr, "Change the cover photo" if dr["photo_id"] else "Add a cover photo", "coverbtn")
    body = f'''<article class="dpage" data-key="dp{dr["id"]}">{d.dream_cover(dr, "dhero")}<a class="back" href="/dreams" aria-label="Back to the dream board">‹</a>
{cover_btn}
<div class="dsheet"><div class="dtop"><div><div class="label">{label}</div><h1 class="display">{e(dr["title"])}</h1></div>{tear(n)}</div>
<div class="who">{chairs_of(s, dr, 40)}<span>{e(shared)}</span></div>{fund}
<h2 class="heading">What gets us there</h2><div class="tasklist">{steps or '<div class="empty">No steps yet.</div>'}</div>{"" if dr["lived_on"] else add_step}
{journal(c, s, dr, one)}<div class="actions">{acts}</div></div></article>'''
    if one("added"):
        body += added_celebration(s, dr, n, g)
    if one("lived") and dr["lived_on"]:
        body += lived_celebration(s, dr, count, words)
    return room(s, body, "droom")


def journal(c, s, dr, one):
    """A dream's small diary: short dated lines, maybe with a photo. Pip writes here too ("Note for Lapland: ...")."""
    rows = c.execute("select * from family.journal where dream_id = %s order by on_date desc, id desc", (dr["id"],)).fetchall()
    items = "".join(
        f'<li data-key="j{j["id"]}"><time datetime="{j["on_date"]}">{j["on_date"].day} {j["on_date"]:%b}</time><div class="jtext"><p>{e(j["text"])}</p>'
        + (f'<img src="/photo/{j["photo_id"]}" alt="" loading="lazy">' if j["photo_id"] else "")
        + f'</div><form method="post" action="/do/journal/{j["id"]}/remove" class="act"><button class="x" data-confirm="Remove this line?" '
          f'aria-label="Remove">×</button></form></li>' for j in rows)
    agent = e(s["fam"]["agent_name"])
    return f'''<h2 class="heading" id="journal">Journal</h2>{problem("Write a few words for the journal.") if one("problem") == "journal" else ""}
<form method="post" action="/do/journal/{dr["id"]}/add" enctype="multipart/form-data" class="jform" data-photo-optional>
<label class="sr" for="jtext">A line for the journal</label><input id="jtext" name="text" maxlength="280" required autocomplete="off"
placeholder="Found cheap flights for February"><label class="btn sunk filebtn jphoto" title="Add a photo">Photo<input type="file" name="photo" accept="image/*"></label>
<button class="btn sunk">Add</button></form>
<ol class="journal">{items or f'<li class="empty">Little things on the way: a price you found, a drawing, a photo. Or tell {agent}: "Note for {e(d.fund_name(dr["title"])[:-5])}: ..."</li>'}</ol>'''


def photo_form(dr, label="Add a photo", cls=""):
    """A photo for a dream, planned or lived: it becomes the dream's cover (and is kept in Photos)."""
    caption = f'{dr["title"]}, {dr["lived_on"]:%B %Y}' if dr["lived_on"] else dr["title"]
    return f'''<form method="post" action="/do/photo/0/add" enctype="multipart/form-data" class="photoform {cls}" data-photo>
<input type="hidden" name="dream" value="{dr["id"]}"><input type="hidden" name="caption" value="{e(caption)}">
<label class="btn sunk filebtn">{e(label)}<input type="file" name="photo" accept="image/*" required></label><button class="btn sunk nojs">Send</button></form>'''


def added_celebration(s, dr, n, g):
    """Dream added (small celebration, 1.2 s): the sharers' chairs pull up around the round picture; cut-outs in their colours."""
    ppl = [p for p in s["people"] if p["id"] in (dr["sharers"] or [])] or s["people"][:1]
    chairs = "".join(f'<span class="gather" style="--a:{-90 + i * 360 / len(ppl):.1f}deg;--i:{i}">{d.chair(p, 44)}</span>'
                     for i, p in enumerate(ppl))
    when = f"{n} days" if n else "someday" if not dr["on_date"] else "today"
    remind = (f' {e(s["fam"]["agent_name"])} will remind you about the saving goal on the first of each month.'
              if g and g["by_date"] else "")
    pulled = "Everyone pulled up a chair." if len(ppl) > 1 else f"{e(ppl[0]['name'])} pulled up a chair."
    return f'''<div class="celebrate small" role="dialog" aria-label="Dream added">{d.cutouts([p["colour"] for p in ppl], 15, "burst")}
<div class="ring">{d.dream_cover(dr, "round")}{chairs}</div>
<div class="label">On the board · {when}</div><h2 class="display">{e(dr["title"])}</h2><p class="lede">{pulled}{remind}</p>
<a class="btn raised" href="/dreams">See the board</a></div>'''


def lived_celebration(s, dr, count, words):
    """It came true (big celebration, 2.5 s): a Forest screen, the card becomes a pinned photo, 30 cut-outs fall."""
    cols = [p["colour"] for p in s["people"] if p["id"] in (dr["sharers"] or [])] or ["mustard"]
    who = f"all {words} of you" if count > 1 else sharer_words(s, dr)
    return f'''<div class="celebrate big" data-theme="night" role="dialog" aria-label="It came true">{d.cutouts(cols + ["mustard"], 30, "fall")}
<div class="pinned hero"><i class="pinhead c-mustard"></i>{d.dream_cover(dr)}<h3>{e(dr["title"])}</h3><div class="note">{dr["lived_on"]:%B %Y} · {e(who)}</div></div>
<h2 class="display">It came true.</h2><p class="lede">Moved to the dreams you lived. Add a photo from the trip and it becomes the picture on the wall.</p>
<div class="col">{photo_form(dr)}<a class="btn ghost" href="/dreams">Later</a></div></div>'''


# ---------- routines, photos, support ----------

def routines(s):
    rows = "".join(f'<section class="block card" data-key="rr{r["id"]}">{d.routine_html(s, r)}<div class="ractions">'
                   f'<form method="post" action="/do/routine/{r["id"]}/points" class="act rpoints"><label>Points for each step '
                   f'<input name="points" inputmode="numeric" value="{r["points"] or ""}" placeholder="0" maxlength="3" size="3"></label>'
                   f'<button class="quietlink small">Save</button></form>'
                   f'<form method="post" action="/do/routine/{r["id"]}/remove" class="act"><button class="quietlink small" '
                   f'data-confirm="Remove {e(d.routine_title(r))}?">Remove this routine</button></form></div></section>' for r in s["routines"])
    kids = sorted(s["people"], key=lambda p: p["role"] != "child")
    opts = "".join(f'<option value="{p["id"]}">{e(p["name"])}</option>' for p in kids if p["role"] != "pet")
    add = f'''<section class="block card" id="add"><h2>A new routine</h2>
<form method="post" action="/do/routine/0/add" class="formrow"><label class="field"><span>Whose</span><select name="person" class="sel">{opts}</select></label>
<div class="when"><label class="pill"><input type="radio" name="part" value="morning" checked><span>Morning</span></label>
<label class="pill"><input type="radio" name="part" value="evening"><span>Evening</span></label></div>
<label class="field wide"><span>The steps, with commas</span><input name="steps" maxlength="200" placeholder="Wake up, get dressed, breakfast, teeth, school bag" required></label>
<label class="field short"><span>Points for each step <i>(optional)</i></span><input name="points" inputmode="numeric" placeholder="2"></label>
<button class="btn raised">Make the routine</button></form>
<p class="note">Each step gets a picture from its words. A child taps a step when it's done and it gets a star; they start fresh each day.
With points, the stars add up toward the child's own dream.</p></section>'''
    return room(s, f'<div class="roomhead"><h1 class="display">Routines</h1></div><div class="cards">{rows}{add}</div>', "routineroom")


def photos(s, one):
    imgs = "".join(f'<figure class="ph" data-key="ph{p["id"]}"><img src="/photo/{p["id"]}" alt="{e(p["caption"])}" loading="lazy">'
                   f'<figcaption>{e(p["caption"])}</figcaption><form method="post" action="/do/photo/{p["id"]}/remove" class="act">'
                   f'<button class="x" data-confirm="Remove this photo?">×</button></form></figure>' for p in s["photos"])
    form = f'''<form method="post" action="/do/photo/0/add" enctype="multipart/form-data" class="block card photoform" data-photo>
<h2>Add a photo or a drawing</h2>{problem("That file is not a photo we can show. Try a JPEG or PNG picture.") if one("problem") else ""}
<label class="field"><span>A few words <i>(optional)</i></span><input name="caption" maxlength="80" placeholder="Saaremaa, last summer"></label>
<label class="btn raised filebtn">Choose a photo<input type="file" name="photo" accept="image/*" required></label><button class="btn sunk nojs">Send</button>
<p class="note">Photos stay on your box. They show on the Photos block and on the wall screen when nobody is near.</p></form>'''
    return room(s, f'<div class="roomhead"><h1 class="display">Photos</h1></div>{form}<div class="phgrid">{imgs}</div>', "photoroom")


def support(s, one):
    if one("thanks"):
        inner = ('<h1 class="display">Thank you.</h1><p class="lede">We read every one. If it is urgent, say so in your '
                 'words and we will look first.</p><a class="btn raised" href="/">Back to the table</a>')
    else:
        agent = e(s["fam"]["agent_name"])
        inner = f'''<h1 class="display">Something not right?</h1>
<p class="lede">Tell us in your own words what happened, and on which screen. It is kept on your box for the people who look after FamBox.</p>
<form method="post" action="/do/support/0/add" class="supportform"><input type="hidden" name="page" value="{e(one("from"))}">
<label class="sr" for="what">What happened</label><textarea id="what" name="text" rows="6" maxlength="2000" required placeholder="The calendar showed Lena's piano on the wrong day."></textarea>
<button class="btn raised big">Send</button></form><p class="note">You can also tell {agent}: "Problem: ..." on Telegram or here on the home screen.</p>'''
    return room(s, f'<div class="sheet narrow">{inner}</div>', "supportroom")


# ---------- Pip's thinking (phase E): FamBox AI, the family's own key, or the simple rules ----------

PROVIDER_NAMES = {"anthropic": "Anthropic (Claude)", "openai": "OpenAI (ChatGPT)", "openrouter": "OpenRouter",
                  "gemini": "Google Gemini", "mistral": "Mistral"}


def thinking(c, s, one):
    """How Pip understands what the family writes and photographs. Not in the design: drawn in the Table system
    (one card, three choices, sunk fields, one button). A key is checked with one tiny call and never shown again."""
    a = e(s["fam"]["agent_name"])
    h = c.execute("select ai_mode, ai_provider, ai_key, ai_state from family.household").fetchone()
    hosted = bool(os.environ.get("FAMBOX_AI_TOKEN"))
    mode = h["ai_mode"] if h["ai_mode"] != "fambox" or hosted else "rules"
    prov = PROVIDER_NAMES.get(one("provider")) or "your AI company"
    said = {"saved": "Saved.", "removed": "Your key is removed from the box."}
    msg = next((f'<p class="ok" role="status">{said[k]}</p>' for k in said if one(k)), "")
    msg += problem({"refused": "That key wasn't accepted. Copy it again from your AI company's page and paste the whole key.",
                    "offline": f"We couldn't reach {prov} just now. Wait a minute and try again.",
                    "nokey": "Paste your key first.", "provider": "Choose your AI company."}[one("problem")]) \
        if one("problem") in ("refused", "offline", "nokey", "provider") else ""
    if mode == "fambox" and h["ai_state"] == "cap_reached":
        msg += f"<p class=\"note\">This month's FamBox AI is used up. It comes back on the 1st; until then {a} uses the simple rules.</p>"
    elif mode == "fambox" and h["ai_state"] == "ai_not_set_up":
        msg += f'<p class="note">FamBox AI is being switched on. Until then {a} uses the simple rules.</p>'
    card = lambda k, title, words, off=False: (
        f'<label class="plan{" soon" if off else ""}"><input type="radio" name="mode" value="{k}"{" checked" if k == mode else ""}'
        f'{" disabled" if off else ""}><b>{title}</b><span>{words}</span></label>')
    cards = (card("fambox", "FamBox AI", (f"Included with your table. " if hosted else "An add-on for EUR 5 a month, coming with your plan. ")
                  + f"{a} understands anything you write and reads photos of letters.", off=not hosted)
             + card("own", "Use our own key", "You pay your AI company yourself. "
                    f"{a} talks to it straight from your box: Anthropic, OpenAI, OpenRouter, Google Gemini or Mistral.")
             + card("rules", "Simple rules only", f"No AI. {a} adds dates, jobs and money lines you write in plain words."))
    if h["ai_key"]:
        key = (f'<p class="keyline">{e(PROVIDER_NAMES.get(h["ai_provider"], "Your"))} key ending '
               f'<b>••••{e(h["ai_key"][-4:])}</b></p>')
    else:
        opts = "".join(f'<option value="{k}"{" selected" if k == one("provider") else ""}>{v}</option>' for k, v in PROVIDER_NAMES.items())
        key = (f'<div class="formrow"><label class="field wide"><span>Your AI company</span><select name="provider" class="sel">{opts}</select></label>'
               f'<label class="field wide"><span>Your key</span><input name="key" type="password" autocomplete="off" spellcheck="false"'
               f' placeholder="Paste it here"></label></div>'
               f'<p class="note">We check it with one tiny question, keep it only on your box and never show it again.</p>')
    forget = (f'<form method="post" action="/do/ai/0/forget" class="act"><button class="quietlink small" data-confirm="Remove the key '
              f'from your box?">Remove the key</button></form>') if h["ai_key"] else ""
    return room(s, f'''<div class="sheet narrow aisheet"><h1 class="display">{a}'s thinking</h1>
<p class="lede">How {a} understands what you write and photograph. The simple rules always work, with or without AI.</p>
<form method="post" action="/do/ai/0/save" class="aiform"><div class="plans">{cards}</div>
<div class="ownkey">{key}</div>{msg}<button class="btn raised big">Save</button></form>{forget}</div>''', "airoom")
