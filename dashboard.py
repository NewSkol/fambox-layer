#!/usr/bin/env python3
"""FamBox family dashboard: the home screen of a family's box.

Python standard library plus psycopg (PostgreSQL). Pages are built here as HTML from the Table
design; static/app.js only refreshes them live. Data lives in the "family" schema (schema.sql).

  python3 dashboard.py --init      create or upgrade the family schema
  python3 dashboard.py --sample    replace the data with the Kask sample family (dev and tests only)
  python3 dashboard.py --new-family NAME SLUG   a fresh box: wipes the family, moving in starts again
  python3 dashboard.py --link      print a one-time sign-in link (the box's welcome message)
  python3 dashboard.py             serve on 127.0.0.1:8090, with Pip on Telegram once a bot is connected

Environment: FAMBOX_DB (default "dbname=builder", the box user's own database), PORT, HOST,
FAMBOX_ADDRESS (the box's address, for links), FAMBOX_DEMO=1 (a demo box: "Start again" on the home screen),
FAMBOX_TELEGRAM=dry-run (see pip.py),
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (two-way Google calendars, calsync.py). Every page except /healthz needs a signed-in browser (welcome.py).
"""
import html
import json
import math
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

import ai
import calsync
import money
import pages
import pip
import welcome

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
DSN = os.environ.get("FAMBOX_DB", "dbname=builder")
e = html.escape

LIGHT_TEXT = {"teal", "plum", "clay", "olive"}  # chair colours that carry white initials (tokens.css)
DAY_START, DAY_END = 6, 22  # the day ribbon runs 06:00 to night
DREAM_COVERS = 10  # drawn covers standing in for photos (style.css .cover-0 to .cover-9; moving in offers the first 4)
WEATHER_WORDS = {"clearsky": "Clear", "fair": "Fair", "partlycloudy": "Partly cloudy", "cloudy": "Cloudy",
                 "fog": "Fog", "lightrain": "Light rain", "rain": "Rain", "heavyrain": "Heavy rain",
                 "rainshowers": "Showers", "lightrainshowers": "Light showers", "sleet": "Sleet",
                 "lightsnow": "Light snow", "snow": "Snow", "snowshowers": "Snow showers"}


def db():
    return psycopg.connect(DSN, autocommit=True, row_factory=dict_row)


# ---------- reading the family's day ----------

def load(c, now):
    """Everything the home screen shows, for the family's local day `now`."""
    fam = c.execute("select * from family.household").fetchone()
    people = c.execute("select * from family.people order by sort, id").fetchall()
    today = now.date()
    week0 = today - timedelta(days=today.weekday())
    tz = now.tzinfo
    start = datetime.combine(week0, datetime.min.time(), tz)
    events = c.execute("select * from family.events where starts_at < %s and coalesce(ends_at, starts_at) >= %s"
                       " order by all_day desc, starts_at", (start + timedelta(days=14), start)).fetchall()
    for ev in events:
        ev["start"] = ev["starts_at"].astimezone(tz)
        ev["end"] = (ev["ends_at"] or ev["starts_at"]).astimezone(tz)
    tasks = c.execute("select * from family.tasks where done_at is null or done_at > now() - interval '7 days'"
                      " order by created_at, id").fetchall()
    sugg = c.execute("select * from family.suggestions where state = 'pending'"
                     " or (state = 'accepted' and decided_at > now() - interval '1 day')"
                     " order by state = 'pending' desc, items->0 ? 'payday' desc, id").fetchall()  # payday's question first
    status = c.execute("select * from family.status where done_at is null order by id limit 1").fetchone()
    talk = c.execute("select * from family.messages where from_pip and at > now() - interval '10 minutes'"
                     " and at > %s order by id desc limit 1",  # Pip's latest answer stands in for its line of the day
                     (fam["moved_in_at"] or now,)).fetchall()  # (not what was said while moving in)
    m0, m1 = month_bounds(today)
    t0 = datetime.combine(today, datetime.min.time(), tz)
    routines = c.execute("select r.*, p.name, p.colour from family.routines r left join family.people p on p.id = r.person_id"
                         " order by r.sort, r.id").fetchall()
    done = {}
    for x in c.execute("select routine_id, step, at from family.routine_done where on_day = %s", (today,)).fetchall():
        done.setdefault(x["routine_id"], {})[x["step"]] = x["at"]
    sun = sun_times(float(fam["lat"]), float(fam["lon"]), today, tz) if fam["lat"] is not None and fam["lon"] is not None else None
    return dict(fam=fam, people=people, events=events, tasks=tasks, sugg=sugg, status=status, talk=talk,
                now=now, today=today, week0=week0, by_id={p["id"]: p for p in people}, sun=sun,
                dreams=c.execute("select * from family.dreams order by lived_on desc nulls first, on_date nulls last, id").fetchall(),
                goals=c.execute("select * from family.goals order by id").fetchall(),
                spent=c.execute("select coalesce(sum(amount), 0) s from family.spending where spent_on >= %s and spent_on < %s",
                                (m0, m1)).fetchone()["s"],
                bills=c.execute("select * from family.bills order by due_day, id").fetchall(),
                cd_events=c.execute("select * from family.events where countdown and starts_at >= %s order by starts_at limit 6",
                                    (t0,)).fetchall(),
                routines=routines, routine_done=done,
                meals=c.execute("select * from family.meals where on_date >= %s and on_date < %s order by on_date, id",
                                (today, today + timedelta(days=7))).fetchall(),
                shopping=c.execute("select * from family.shopping where done_at is null or done_at > now() - interval '1 day'"
                                   " order by done_at nulls first, id").fetchall(),
                notes=c.execute("select * from family.notes order by id desc limit 8").fetchall(),
                photos=c.execute("select id, caption from family.photos order by id desc limit 50").fetchall(),
                layout=layout(c), month=money.month(c, today, fam), points=points_of(c), moment=moment(now),
                arranged=bool(c.execute("select 1 from family.blocks limit 1").fetchone()),
                bank_new=c.execute("select count(*) n from family.bank_lines where state = 'new'").fetchone()["n"],
                cal=c.execute("select kind, calendar_name, problem, problem_since from family.cal_accounts"
                              " where problem is not null").fetchall(),
                ai_on=bool(ai.setting(c)) and fam["ai_state"] not in ("ai_not_set_up", "cap_reached"),  # letters need a working AI
                group=bool(c.execute("select 1 from family.chats where kind = 'group'").fetchone())
                or not c.execute("select 1 from family.members where consent_at is not null").fetchone())  # nobody could add Pip yet


def points_of(c):
    """Each person's points: what their chores earned, less the points of their dreams that came true."""
    return {r["id"]: r["n"] for r in c.execute(
        "select p.id, coalesce((select sum(amount) from family.points x where x.person_id = p.id), 0)"
        " - coalesce((select sum(points_target) from family.dreams d where d.points_for = p.id and d.lived_on is not null), 0) n"
        " from family.people p").fetchall()}


def on_day(events, d):
    def last(ev):  # a timed event ending at 00:00 belongs to the day before
        end = ev["end"] if ev["all_day"] or ev["end"] == ev["start"] else ev["end"] - timedelta(microseconds=1)
        return end.date()
    return [ev for ev in events if ev["start"].date() <= d <= last(ev)]


def free_together(events, now, need=timedelta(hours=2)):
    """First weekend gap of at least two hours between 10:00 and 18:00 when nothing is planned."""
    for i in range(8):
        d = now.date() + timedelta(days=i)
        if d.weekday() < 5:
            continue
        lo = max(datetime.combine(d, datetime.min.time(), now.tzinfo).replace(hour=10), now)
        hi = lo.replace(hour=18, minute=0)
        busy = sorted((ev["start"], ev["ends_at"] and ev["end"] or ev["start"] + timedelta(hours=1))
                      for ev in on_day(events, d) if not ev["all_day"])
        t = lo
        for b0, b1 in busy + [(hi, hi)]:
            if b0 - t >= need:
                return d, t, min(b0, hi)
            t = max(t, b1)
    return None


_weather = {"at": 0, "ok_at": 0, "data": None}


def _fetch_weather(lat, lon):
    url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat:.3f}&lon={lon:.3f}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FamBox/0.1 https://fam.box"})
        _weather["data"] = json.load(urllib.request.urlopen(req, timeout=8))["properties"]["timeseries"]
        _weather["ok_at"] = time.time()
    except Exception as ex:  # no weather is fine; the clock still shows
        print(f"weather unavailable: {type(ex).__name__}", file=sys.stderr)


def weather(fam, now):
    """Temperature and a few words from MET Norway (free, credit required). None when unknown."""
    if fam["lat"] is None or fam["lon"] is None or os.environ.get("FAMBOX_NO_WEATHER"):
        return None
    if time.time() - _weather["at"] > 1800:  # refresh in the background; this page uses what is there
        _weather["at"] = time.time()
        threading.Thread(target=_fetch_weather, args=(float(fam["lat"]), float(fam["lon"])), daemon=True).start()
    series = _weather["data"]
    if not series or time.time() - _weather["ok_at"] > 3 * 3600:
        return None
    cur = series[0]["data"]
    code = cur.get("next_1_hours", {}).get("summary", {}).get("symbol_code", "")
    words = WEATHER_WORDS.get(code.split("_")[0], "")
    rain = None
    for s in ([] if "rain" in code or "sleet" in code else series):  # already raining: no "rain after"
        t = datetime.fromisoformat(s["time"].replace("Z", "+00:00")).astimezone(now.tzinfo)
        if t.date() != now.date():
            break
        if t > now and s["data"].get("next_1_hours", {}).get("details", {}).get("precipitation_amount", 0) >= 0.2:
            rain = t
            break
    hours = []
    for x in series[1:13:2]:  # every second hour, for the large weather block
        t = datetime.fromisoformat(x["time"].replace("Z", "+00:00")).astimezone(now.tzinfo)
        if t > now:
            hours.append((t, round(x["data"]["instant"]["details"]["air_temperature"])))
    return dict(temp=round(cur["instant"]["details"]["air_temperature"]), words=words, rain=rain, hours=hours[:5])


# ---------- drawing ----------

def ring(width, r, d, colours):
    """The family symbol: a dot (the table) with one chair per person around it."""
    out = [f'<span class="sym" style="width:{width}px;height:{width}px">'
           f'<i style="left:{width/2 - d*0.55:.1f}px;top:{width/2 - d*0.55:.1f}px;width:{d*1.1:.1f}px;height:{d*1.1:.1f}px" class="table"></i>']
    for i, col in enumerate(colours):
        a = -math.pi / 2 + i * 2 * math.pi / len(colours)
        out.append(f'<i class="c-{col}" style="left:{width/2 + d*math.cos(a) - r:.1f}px;top:{width/2 + d*math.sin(a) - r:.1f}px;'
                   f'width:{2*r:.1f}px;height:{2*r:.1f}px"></i>')
    return "".join(out) + "</span>"


def pip_face(size, character="Pip", mood=None):
    return f'<span class="pip" style="width:{size}px;height:{size}px">{welcome.face(character, size, mood)}</span>'


def chair(p, size=36, link=None, on=False):
    fg = "light" if p["colour"] in LIGHT_TEXT else "dark"
    tag = f'<a href="{e(link)}"' if link else "<span"
    end = "</a>" if link else "</span>"
    return (f'{tag} class="chair c-{p["colour"]} {fg}{" on" if on else ""}" style="--s:{size}px" '
            f'title="{e(p["name"])}" data-key="p{p["id"]}">{e(p["name"][0])}{end}')


def colour_of(ev, by_id):
    p = by_id.get(ev["person_id"])
    return f'c-{p["colour"]}' if p else "c-ink"


def hm(t):
    return t.strftime("%H:%M")


def day_name(d, today):
    return "Today" if d == today else "Tomorrow" if d == today + timedelta(1) else d.strftime("%A")


def post(action, text, cls="link", label=None):
    """A button that works without JavaScript: a tiny form posting to `action`."""
    hidden = f'<input type="hidden" name="label" value="{e(label)}">' if label is not None else ""
    return (f'<form method="post" action="{e(action)}" class="act">{hidden}'
            f'<button class="{cls}">{e(text)}</button></form>')


BLOCKS = {  # kind: (name in edit mode and on the shelf, size when it is put on the table, shelf note)
    "needs": ("What needs us", "L", "Pip's questions and what is due"),
    "day": ("Day", "L", "Today, tonight, the week ahead"),
    "clock": ("Clock and weather", "L", "The time and the day"),
    "pip": ("Agent", "L", "Ideas and a place to ask"),
    "today": ("Calendar", "L", "Today and the week"),
    "tasks": ("Who does what", "M", "Jobs with chairs"),
    "dreams": ("Dreams", "M", "From the dream board"),
    "countdowns": ("Countdowns", "M", "Birthdays, trips, holidays"),
    "money": ("Money", "M", "The plan and the bills"),
    "routines": ("Routines", "M", "Picture steps for children"),
    "weather": ("Weather", "S", "S, M, L · far"),
    "photos": ("Photos", "M", "Your own, or drawings"),
    "meals": ("Meals", "M", "Tonight, the week, shopping"),
    "notes": ("Notes", "M", "A small pinboard"),
}
# A family that never arranged its table sees one of these. The first home (the October design) stays for families who
# had it until they choose the new one ("Use the new home"); new families start on the new one (household.home).
OLD_HOME = ["clock", "pip", "today", "tasks", "dreams", "countdowns", "money", "routines"]
NEW_HOME = ["needs", "day", "money", "dreams", "routines", "tasks", "countdowns"]
ICONS = {  # routine pictograms from the design (FamBox Table Dashboard.dc.html, logic R)
    "wake": "M12 8a4 4 0 1 1 0 8a4 4 0 1 1 0-8zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.6 17.6L19 19M5 19l1.4-1.4M17.6 6.4L19 5",
    "dress": "M8 4L3 7l2 4 2-1v10h10V10l2 1 2-4-5-3q-1 2.2-4 2.2T8 4z",
    "eat": "M3 11h18a9 7 0 0 1-18 0zM9 3.5q1.2 2 0 4M14 3.5q1.2 2 0 4",
    "teeth": "M5 19L15 9M13.5 4.5h6v4h-6zM14.8 6.5h3.4",
    "bag": "M6.5 9a5.5 5.5 0 0 1 11 0v11h-11zM9.5 8.5V6.5a2.5 2.5 0 0 1 5 0v2M9 14h6",
    "bath": "M3 12h18v2a6 6 0 0 1-6 6H9a6 6 0 0 1-6-6zM6 12V6a2 2 0 0 1 4 0",
    "book": "M4 5h6a2 2 0 0 1 2 2v12a2 2 0 0 0-2-2H4zM20 5h-6a2 2 0 0 0-2 2v12a2 2 0 0 1 2-2h6z",
    "moon": "M19 14.5A7.5 7.5 0 1 1 9.5 5a6 6 0 0 0 9.5 9.5z",
    "dot": "M12 6.5a5.5 5.5 0 1 1 0 11a5.5 5.5 0 1 1 0-11z",
}
STAR = "M12 2.6l2.8 5.9 6.4.8-4.7 4.4 1.2 6.4L12 17l-5.7 3.1 1.2-6.4-4.7-4.4 6.4-.8z"


def layout(c):
    """The family's blocks in their order, [{kind, size, sort, shown}]. A family that arranged its table keeps exactly
    that (blocks added since wait on the shelf); one that never did sees its home's own arrangement."""
    rows = {r["kind"]: r for r in c.execute("select * from family.blocks").fetchall() if r["kind"] in BLOCKS}
    fam = c.execute("select home from family.household").fetchone()
    plan = NEW_HOME if fam and fam["home"] in ("new", "tried") else OLD_HOME
    order = list(BLOCKS)
    out = []
    for k, (_, size, _) in BLOCKS.items():
        if k in rows:
            out.append(rows[k])
        elif not rows and k in plan:
            out.append(dict(kind=k, size=size, sort=plan.index(k), shown=True))
        else:
            out.append(dict(kind=k, size=size, sort=100 + order.index(k), shown=False))
    return sorted(out, key=lambda b: (b["sort"], order.index(b["kind"])))


def moment(now):
    """The part of the family's day the home screen is drawn for: morning, day, evening, night, or "week"
    (Sunday evening: the week ahead)."""
    h = now.hour
    if now.weekday() == 6 and 17 <= h < 24:
        return "week"
    return "morning" if 5 <= h < 12 else "day" if 12 <= h < 17 else "evening" if 17 <= h < 21 else "night"


def arrange(c, kind, size=None, shown=None, to=None):
    """Changes one block (size, shown, new place `to` among the shown blocks) and saves the whole arrangement."""
    if kind not in BLOCKS or (size and size not in ("S", "M", "L")):
        return False
    with c.transaction():  # Pip calls this outside a transaction; the lock needs one
        c.execute("lock table family.blocks in share row exclusive mode")  # two screens arranging at once take turns
        bl = layout(c)
        b = next(x for x in bl if x["kind"] == kind)
        b["size"] = size or b["size"]
        if shown is not None:
            if shown and not b["shown"]:
                to = len([x for x in bl if x["shown"]]) if to is None else to
            b["shown"] = shown
        if to is not None:
            bl.remove(b)
            shown_list = [x for x in bl if x["shown"]]
            anchor = shown_list[to] if 0 <= to < len(shown_list) else None
            bl.insert(bl.index(anchor) if anchor else len(bl), b)
        c.execute("delete from family.blocks")
        for i, x in enumerate(bl):
            c.execute("insert into family.blocks (kind, size, sort, shown) values (%s, %s, %s, %s)",
                      (x["kind"], x["size"], i, x["shown"]))
    return True


def month_bounds(d):
    m0 = d.replace(day=1)
    return m0, (m0 + timedelta(days=32)).replace(day=1)


def eur(x):
    x = float(x)
    return f"€{x:,.0f}" if x == int(x) else f"€{x:,.2f}"


def settings(goal, size=""):
    """Ten place settings: the saving goal at a glance."""
    n = min(10, int(goal["saved"] * 10 // goal["target"])) if goal else 0
    return f'<span class="settings {size}" aria-hidden="true">{"".join(f"<i class=on></i>" if i < n else "<i></i>" for i in range(10))}</span>'


def goal_words(g):
    return f'<b>{eur(g["saved"])}</b> of {eur(g["target"])}'


def days_to(d, today):
    return (d - today).days


def countdown_list(s):
    """Countdowns: events marked to count down and dreams with a date, soonest first."""
    items = [(ev["starts_at"].astimezone(s["now"].tzinfo).date(), ev["title"], f"e{ev['id']}") for ev in s["cd_events"]]
    items += [(dr["on_date"], dr["title"], f"d{dr['id']}") for dr in s["dreams"]
              if dr["on_date"] and not dr["lived_on"] and dr["on_date"] >= s["today"]]
    return sorted(items)


def page_card(d, what, key, today, wall=False):
    n = days_to(d, today)
    num = "Today" if n == 0 else str(n)
    sub = "" if n == 0 else ("day to" if n == 1 else "days to")
    return (f'<div class="tear{" today" if n == 0 else ""}" data-key="cd{key}" data-n="{n}"><b>{num}</b>'
            f'<span>{sub}</span><strong>{e(what)}</strong></div>') if not wall else (
            f'<div class="tear" data-key="wcd{key}" data-n="{n}"><b>{num}</b><strong>{e(what)}</strong></div>')


def next_event(s):
    now = s["now"]
    for d in (s["today"], s["today"] + timedelta(1)):
        for ev in on_day(s["events"], d):
            if not ev["all_day"] and ev["start"] >= now - timedelta(minutes=5):
                return ev
    return None


def routine_part(now):
    return "morning" if now.hour < 13 else "evening"


def routine_title(r):
    return f'{r["name"]}\'s {r["part"]}' if r["name"] else f'Our {r["part"]}'


def routine_html(s, r, big=True):
    done = s["routine_done"].get(r["id"], {})
    steps = r["steps"] or []
    n = sum(1 for i in range(len(steps)) if i in done)
    cells = "".join(
        f'<form method="post" action="/do/routine/{r["id"]}/step-{i}" class="act rstep{" done" if i in done else ""}">'
        f'<button title="{e(st.get("name", ""))}{": done" if i in done else ""}"><span class="ricon">'
        f'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="{ICONS.get(st.get("icon"), ICONS["dot"])}"/></svg>'
        f'<svg class="star" viewBox="0 0 24 24" aria-hidden="true"><path d="{STAR}"/></svg></span>'
        f'<span class="rname">{e(st.get("name", ""))}</span></button></form>' for i, st in enumerate(steps))
    last = max(done.values()) if done else None
    party = ""
    if steps and n == len(steps) and last and s["now"] - last < timedelta(seconds=20):  # just finished: the small celebration
        party = cutouts([r["colour"] or "ink"] * 3 + ["mustard"], 14, "burst")
    return (f'<div class="routine" data-key="r{r["id"]}-{n}"><div class="top"><h2>{e(routine_title(r))}</h2>'
            f'<span class="note">{n} of {len(steps)}{f" · {r["points"]} points a step" if r.get("points") else ""}</span></div>'
            f'<div class="rsteps">{cells}</div>{party}</div>')


def cutouts(colours, n, kind):
    """Paper cut-outs in the people's colours (the small and big celebrations). Hidden when motion is reduced."""
    out = []
    for i in range(n):
        col = colours[i % len(colours)]
        x, y, rot = (i * 37) % 96, (i * 53) % 90, (i * 71) % 180
        out.append(f'<i class="cut c-{col}{" sq" if i % 3 == 0 else ""}" style="--x:{x}%;--y:{y}%;--r:{rot}deg;--d:{(i % 7) * 60}ms"></i>')
    return f'<div class="cutouts {kind}" aria-hidden="true">{"".join(out)}</div>'


def block_clock(s, w, size="L"):
    now = s["now"]
    pos = lambda t: max(0, min(100, (t.hour + t.minute / 60 - DAY_START) / (DAY_END - DAY_START) * 100))
    wline = ""
    rain_label = ""
    if w:
        rain = f', rain after {hm(w["rain"])}' if w["rain"] else ""
        wline = (f'<div class="weather"><span class="temp">{w["temp"]}°</span>'
                 f'<span class="wwords">{e((w["words"] + rain).lstrip(", ")).capitalize()}</span></div>')
        if w["rain"]:
            rain_label = f'<span style="left:{pos(w["rain"]):.1f}%">rain {hm(w["rain"])}</span>'
    if size == "S":
        return (f'<section class="block S clock" data-key="clock" data-kind="clock"><div class="label">{e(now.strftime("%a %-d %b"))}</div>'
                f'<div class="time" data-clock>{hm(now)}</div>{wline}</section>')
    if size == "M":
        nx = next_event(s)
        nline = (f'<div class="nextline"><i class="pdot {colour_of(nx, s["by_id"])}"></i>{e(nx["title"])} '
                 f'{day_name(nx["start"].date(), s["today"]).replace("Today", "").lower()} {hm(nx["start"])}</div>' if nx else "")
        return (f'<section class="block M clock" data-key="clock" data-kind="clock"><div class="label">{e(now.strftime("%A %-d %B"))}</div>'
                f'<div class="mrow"><div class="time" data-clock>{hm(now)}</div><div>{wline}{nline}</div></div></section>')
    ticks = "".join(f'<i class="tick {colour_of(ev, s["by_id"])}" style="left:{pos(ev["start"]):.1f}%"></i>'
                    for ev in on_day(s["events"], s["today"]) if not ev["all_day"])
    return f'''<section class="block L clock" data-key="clock" data-kind="clock">
<div class="label" data-date>{e(now.strftime("%A %-d %B"))}</div>
<div class="time" data-clock>{hm(now)}</div>{wline}
<div class="ribbon" data-ribbon="{DAY_START},{DAY_END}"><i class="bar"></i>{ticks}
<i class="sun" style="left:{pos(now):.1f}%"></i><i class="moon"></i>
<div class="marks"><span>{DAY_START:02d}:00</span>{rain_label}<span class="r">night</span></div></div>
</section>'''


def suggestion(sg, big, look=False):
    items = sg["items"]
    if sg["state"] == "accepted":
        return (f'<div class="added" data-key="s{sg["id"]}"><span>Added</span> · '
                f'{post(f"/do/suggestion/{sg["id"]}/undo", "Undo")}</div>')
    if sg["quiet"]:
        return (f'<div class="quiet" data-key="s{sg["id"]}"><i class="dot"></i>{e(sg["text"])}'
                f'{post(f"/do/suggestion/{sg["id"]}/accept", sg["accept_label"], "link strong")}'
                f'{post(f"/do/suggestion/{sg["id"]}/dismiss", "Not now", "link")}</div>')
    n = len(items)
    label = sg["accept_label"].replace("{n}", str(n))
    if look:  # "Look first": every item on its own line, each can be taken out before adding
        chips = "".join(f'<div class="lookline"><span>{e(it.get("label", ""))}</span>'
                        f'{post(f"/do/suggestion/{sg["id"]}/drop-{i}", "Leave out", "link", label=it.get("label", ""))}</div>'
                        for i, it in enumerate(items))
        second = post(f"/do/suggestion/{sg["id"]}/dismiss", "None of these", "btn sunk")
    else:
        chips = "".join(f'<span class="chip">{e(it.get("label", ""))}</span>' for it in items) if big else ""
        second = f'<a class="btn sunk" href="?look={sg["id"]}#pip">Look first</a>'
    return f'''<div class="suggest" data-key="s{sg["id"]}"><div>{e(sg["text"])}</div>
<div class="chips{" look" if look else ""}">{chips}</div>
<div class="row">{post(f"/do/suggestion/{sg["id"]}/accept", label, "btn raised")}{second}</div></div>'''


def pip_parts(s):
    """(Telegram note or "", the ask bar, the quiet extras) for Pip's block and the needs strip."""
    fam = s["fam"]
    a = e(fam["agent_name"])
    if pip.DRY:
        tg = ""
    elif not fam["bot_username"]:
        tg = f'<div class="tgnote">Not on Telegram yet · <a class="link" href="/welcome/5">Connect</a></div>'
    elif pip.seen["problem"] == "unknown":
        tg = f'<div class="tgnote warn">Telegram no longer knows {a} · <a class="link" href="/welcome/5?change=1">Connect again</a></div>'
    elif pip.offline():
        tg = f'<div class="tgnote warn">Can\'t reach Telegram right now; messages wait there</div>'
    else:
        tg = ""
    ask = (f'<form method="post" action="/ask" class="askform act"><label class="sr" for="ask">Ask {a}</label>'
           f'<input id="ask" name="text" maxlength="500" placeholder="Ask {a}, or tell {a} a date" autocomplete="off">'
           f'<button class="btn raised">Send</button></form>')
    extra = ""
    if s.get("ai_on"):  # a photo of a letter: Pip finds the dates (phase E); the photo is not kept
        extra += (f'<form method="post" action="/do/letter/0/add" enctype="multipart/form-data" class="letterform" data-photo'
                  f' data-fail="/#pip"><label class="quietlink small filebtn">Read a letter from a photo<input type="file" name="photo"'
                  f' accept="image/*" required></label><button class="btn sunk nojs">Send</button></form>')
    if not pip.DRY and fam["bot_username"] and not s.get("group") and not tg:
        extra += f'<a class="quietlink small" href="/telegram/group">Add {a} to your family group</a>'
    return tg, ask, (f'<div class="pipmore">{extra}</div>' if extra else "")


def greeting(now):
    h = now.hour
    return "good morning" if h < 12 else "good afternoon" if h < 18 else "good evening"


def block_pip(s, look=None, size="L"):
    fam = s["fam"]
    part = greeting(s["now"])
    strip = s.get("needs_shown")  # the needs strip already shows Pip's questions and the Telegram note
    loud = [] if strip else [g for g in s["sugg"] if not g["quiet"]][:1]
    quiet = [] if strip else [g for g in s["sugg"] if g["quiet"]][:1]
    a = e(fam["agent_name"])
    line = f'<div class="pipline">{e(fam["agent_line"])}</div>' if fam["agent_line"] and not s["talk"] else ""
    talk = "".join(f'<div class="bub them">{e(m["text"])}</div>' for m in s["talk"])
    tg, ask, extra = pip_parts(s)
    tg = "" if strip else tg
    pid = "" if strip else ' id="pip"'
    if size == "S":
        first = (loud + quiet)[:1]
        words = e(first[0]["text"]) if first else (e(fam["agent_line"] or "") or f"Ask {a} anything")
        return (f'<section class="block S pipblock"{pid} data-key="pip" data-kind="pip"><div class="head">'
                f'{pip_face(36, fam["agent_character"])}<div class="label">{a}</div></div><div class="sline">{words}</div></section>')
    if size == "M":
        g = (loud + quiet)[:1]
        if g and g[0]["state"] == "pending":
            g = g[0]
            said = (f'<div class="quiet" data-key="s{g["id"]}"><i class="dot"></i>{e(g["text"])}'
                    f'{post(f"/do/suggestion/{g["id"]}/accept", g["accept_label"].replace("{n}", str(len(g["items"]))), "link strong")}</div>')
        elif g:
            said = suggestion(g[0], False)
        else:
            said = line or (f'<div class="pipline">{e(s["talk"][-1]["text"])}</div>' if s["talk"] else "")
        return (f'<section class="block M pipblock"{pid} data-key="pip" data-kind="pip"><div class="head">'
                f'{pip_face(36, fam["agent_character"])}<div class="label">{a} · {part}</div>{tg}</div>{said}{ask}</section>')
    return f'''<section class="block L pipblock"{pid} data-key="pip" data-kind="pip">
<div class="head">{pip_face(44, fam["agent_character"])}<div class="label">{a} · {part}</div>{tg}</div>
{line}{"".join(suggestion(g, True, look == g["id"]) for g in loud)}{"".join(suggestion(g, True) for g in quiet)}
<div class="talk" data-key="talk{s["talk"][-1]["id"] if s["talk"] else 0}">{talk}</div>
{tour_box(2, s, ask, "Talk to " + fam["agent_name"] + " any time, here or on Telegram.")}{extra}
</section>'''


def leader(t, what, key, cls="", dot="", faded=False):
    d = f'<i class="pdot {dot}"></i>' if dot else ""
    return (f'<div class="line{" past" if faded else ""} {cls}" data-key="{key}"><b>{e(t)}</b><i class="dots"></i>'
            f'<span>{e(what)}</span>{d}</div>')


def today_lines(s, person=None, limit=None):
    now, rows = s["now"], []
    for ev in on_day(s["events"], s["today"]):
        if person and ev["person_id"] not in (None, person["id"]):
            continue
        t = "All day" if ev["all_day"] else hm(ev["start"])
        past = not ev["all_day"] and ev["start"] < now - timedelta(minutes=30)
        rows.append((past, leader(t, ev["title"], f"e{ev['id']}", dot="" if person else colour_of(ev, s["by_id"]), faded=past)))
    if limit and len(rows) > limit:  # in a small block, what is still to come matters most
        rows = [r for r in rows if not r[0]][:limit] or rows[-limit:]
    return "".join(h for _, h in rows) or '<div class="empty">Nothing planned today.</div>'


def block_today(s, size="L"):
    if size == "S":
        nx = next_event(s)
        what = (f'<div class="stitle">{e(nx["title"])}</div><div class="note">{day_name(nx["start"].date(), s["today"])} '
                f'{hm(nx["start"])}</div>' if nx else '<div class="note">Nothing more today.</div>')
        return (f'<section class="block S today" id="today" data-key="today" data-kind="today"><div class="top">'
                f'<div class="label">Next</div><a class="note" href="/calendar">Calendar</a></div>{what}</section>')
    if size == "M":
        return (f'<section class="block M today" id="today" data-key="today" data-kind="today"><div class="top"><h2>Today</h2>'
                f'<a class="note" href="/calendar">Calendar</a></div><div class="lines">{today_lines(s, limit=3)}</div></section>')
    week = []
    for i in range(7):
        d = s["week0"] + timedelta(days=i)
        cols = []
        for ev in on_day(s["events"], d):
            c = colour_of(ev, s["by_id"])
            if c not in cols:
                cols.append(c)
        dots = "".join(f'<i class="{c}"></i>' for c in cols[:3])
        week.append(f'<a href="/calendar?d={d.isoformat()}" class="wd{" now" if d == s["today"] else ""}"><span>{d.strftime("%a")}</span>'
                    f'<b>{d.day}</b><div class="wdots">{dots}</div></a>')
    free = free_together(s["events"], s["now"])
    ft = ""
    if free:
        d, a, b = free
        ft = (f'<div class="free">{ring(28, 2.6, 10.5, [p["colour"] for p in s["people"]])}'
              f'<span><b>Free together</b> {day_name(d, s["today"])} {hm(a)} to {hm(b)}</span>'
              f'<a class="link" href="/calendar?add={d.isoformat()}&amp;at={hm(a)}#add">Plan</a></div>')
    return f'''<section class="block L today" id="today" data-key="today" data-kind="today">
<div class="top"><h2>Today</h2><a class="note" href="/calendar">Calendar</a></div>
<div class="lines">{today_lines(s)}</div>
<div class="week">{"".join(week)}</div>{ft}
</section>'''


def in_words(t, now):
    """"in 40 minutes", "in 2 hours", "tomorrow", "now" for the next thing."""
    mins = int((t - now).total_seconds() // 60)
    if t.date() != now.date():
        return day_name(t.date(), now.date()).lower()
    if mins < 5:
        return "now"
    if mins < 60:
        return f"in {mins} minutes"
    return f"in {round(mins / 60)} hour{'s' if round(mins / 60) != 1 else ''}"


def ev_bit(ev):
    return e(ev["title"]) + ("" if ev["all_day"] else f' <b>{hm(ev["start"])}</b>')


def who_where(s, d, since=None):
    """Who is where on day `d` (from `since` on): one line per person with something on, then the whole family's,
    then everyone who is free, by chair."""
    evs = [ev for ev in on_day(s["events"], d)
           if since is None or ev["all_day"] or max(ev["end"], ev["start"] + timedelta(minutes=30)) > since]
    rows, free = [], []
    for p in s["people"]:
        mine = [ev for ev in evs if ev["person_id"] == p["id"]]
        if mine:
            rows.append(f'<div class="whorow" data-key="ww{p["id"]}-{d.isoformat()}">{chair(p, 30)}'
                        f'<span>{" · ".join(ev_bit(ev) for ev in mine[:3])}</span></div>')
        else:
            free.append(p)
    ours = [ev for ev in evs if ev["person_id"] not in s["by_id"]]
    if ours:
        rows.append(f'<div class="whorow all" data-key="wwall-{d.isoformat()}">{ring(30, 3, 11, [p["colour"] for p in s["people"]])}'
                    f'<span>{" · ".join(ev_bit(ev) for ev in ours[:3])}</span></div>')
    if free and rows:
        rows.append(f'<div class="whorow free"><span class="chairs">{"".join(chair(p, 22) for p in free)}</span>'
                    f'<span class="note">Nothing planned</span></div>')
    return "".join(rows)


def week_lines(s, first, n, per=2):
    """The coming days, one line each: who has what (days with nothing are left out)."""
    out = []
    for i in range(n):
        d = first + timedelta(days=i)
        evs = on_day(s["events"], d)
        if not evs:
            continue
        bits = "".join(f'<span class="wbit"><i class="pdot {colour_of(ev, s["by_id"])}"></i>{ev_bit(ev)}</span>' for ev in evs[:per])
        more = f'<span class="note">+{len(evs) - per}</span>' if len(evs) > per else ""
        out.append(f'<a class="wline" href="/calendar?d={d.isoformat()}" data-key="wl{d.isoformat()}">'
                   f'<b>{day_name(d, s["today"])}</b><span class="wbits">{bits}{more}</span></a>')
    return "".join(out)


def block_day(s, w, size="L"):
    """Our day, for the time it is: today in the morning and the day; tonight and the coming days in the evening;
    tomorrow at night; the week ahead on Sunday evening. The next thing is big; then who is where, by chair."""
    now, today, m = s["now"], s["today"], s["moment"]
    weather_words = f' · {w["temp"]}°, {e(w["words"].lower())}' if w else ""
    if m == "night":
        d = today if now.hour < 5 else today + timedelta(1)
        title, who, since, rest = ("Today" if d == today else "Tomorrow"), d, None, d + timedelta(1)
    elif m == "week":
        title, who, since, rest = "The week ahead", None, None, today + timedelta(1)
    else:
        title, who, since, rest = ("Tonight" if m == "evening" else "Today"), today, now, today + timedelta(1)
    nx = next_event(s)
    big = ""
    if nx:
        p = s["by_id"].get(nx["person_id"])
        big = (f'<div class="nextbig" data-key="nx{nx["id"]}"><i class="pdot {colour_of(nx, s["by_id"])}"></i>'
               f'<div><div class="nbt"><b>{hm(nx["start"])}</b> {e(nx["title"])}</div>'
               f'<div class="note">{e(p["name"]) + " · " if p else ""}{in_words(nx["start"], now)}</div></div></div>')
    head = (f'<div class="top"><h2>{title}</h2><a class="note" href="/calendar">Calendar</a></div>'
            f'<div class="label dayline">{e(now.strftime("%A %-d %B"))}{weather_words}</div>')
    if size == "S":
        return (f'<section class="block S day" id="day" data-key="day" data-kind="day">{head}'
                f'{big or "<div class=sline>Nothing more today.</div>"}</section>')
    whos = who_where(s, who, since) if who else ""
    if who and not whos:
        whos = f'<div class="empty">{"A quiet evening." if m == "evening" else "Nothing planned."}</div>'
    weekly = ""
    if size == "L":
        lines = week_lines(s, rest, 7 if m == "week" else 6, 3 if m == "week" else 2)
        if lines:
            weekly = (f'<div class="weekly">{"" if m == "week" else "<div class=label>Coming days</div>"}{lines}</div>')
        elif m == "week":
            weekly = '<div class="empty">A free week. Plan something together?</div>'
    return (f'<section class="block {size} day" id="day" data-key="day" data-kind="day" data-moment="{m}">{head}'
            f'{big}<div class="whos">{whos}</div>{weekly}</section>')


def needs_items(s, look=None):
    """What needs the family now, most pressing first: [(rank, key, html)], each with one tap.
    Ranks: 0 a late bill, 1 the status line (a slip to sign), 2 a bill due today, 3 payday's "save first",
    4 Pip's other questions (dates from a letter), 5 Telegram or a two-way calendar needs a hand, 6 a bill due in the next 3 days,
    7 Pip's small ideas, 8 bank lines waiting, 9 what was just added (Undo)."""
    out = []
    for b in s["bills"]:
        st, words, due = money.bill_state(b, s["today"])
        if st in ("late", "today", "soon"):
            out.append(({"late": 0, "today": 2, "soon": 6}[st], f"b{b['id']}",
                        f'<div class="need" data-key="nb{b["id"]}-{st}"><i class="dot m"></i><span class="nt">'
                        f'{e(b["title"])} <b>{eur(b["amount"])}</b> · {e(words.lower())}</span>'
                        f'{post(f"/do/bill/{b["id"]}/paid", "Paid", "link strong")}</div>'))
    st = s["status"]
    if st:
        detail = f'<span class="note"> · {e(st["detail"])}</span>' if st["detail"] else ""
        out.append((1, f"st{st['id']}", f'<div class="need" data-key="st{st["id"]}"><i class="dot"></i><span class="nt">'
                    f'{e(st["sentence"])}{detail}</span>{post(f"/do/status/{st["id"]}/done", st["action_label"], "link tomato")}</div>'))
    for sg in s["sugg"]:
        first = sg["items"][0] if sg["items"] and isinstance(sg["items"][0], dict) else {}
        rank = 9 if sg["state"] == "accepted" else 7 if sg["quiet"] else 3 if first.get("payday") else 4
        out.append((rank, f"s{sg['id']}", suggestion(sg, True, look == sg["id"])))
    tg = pip_parts(s)[0]
    if tg:
        out.append((5, "tg", f'<div class="need tg" data-key="tg">{tg}</div>'))
    for a in s.get("cal", []):
        line = calsync.problem_line(a)
        if line:
            out.append((5, f"cal{a['kind']}", f'<div class="need" data-key="cal{a["kind"]}-{a["problem"]}"><i class="dot"></i>'
                        f'<span class="nt">{e(line)}</span><a class="link strong" href="/calendar#both">Fix</a></div>'))
    if s.get("bank_new"):
        n = s["bank_new"]
        out.append((8, "bank", f'<div class="quiet" data-key="bank{n}"><i class="dot"></i>{n} new line{"s" if n != 1 else ""} from '
                    f'your bank to look at<a class="link strong" href="/money#review">Look</a></div>'))
    return sorted(out, key=lambda x: x[0])  # stable: within a rank, the order they came in


def block_needs(s, look=None, size="L"):
    """The top of the new home: what needs us now (Pip's questions, bills, a slip to sign), one tap each, and the
    place to ask Pip. Calm when nothing does."""
    fam = s["fam"]
    a = e(fam["agent_name"])
    items = needs_items(s, look)
    keep = {"S": 1, "M": 3, "L": 4}[size]
    shown = "".join(h for _, _, h in items[:keep])
    if len(items) > keep:
        rest = len(items) - keep
        shown += (f'<details class="nmore"><summary>{rest} more</summary>{"".join(h for _, _, h in items[keep:])}</details>')
    open_ = [x for x in items if x[0] < 9]
    title = ("Nothing needs you right now" if not open_ else
             "One thing needs you" if len(open_) == 1 else "A few things need you")
    both = s.get("pip_shown")  # Pip's own block is on the table too: its line, its talk and the ask bar stay there
    line = e(fam["agent_line"]) if fam["agent_line"] and not s["talk"] and not both else ""  # Pip's line of the day
    talk = "" if both else "".join(f'<div class="bub them">{e(m["text"])}</div>' for m in s["talk"])
    _, ask, extra = pip_parts(s)
    ask = "" if both else tour_box(2, s, ask, "Talk to " + fam["agent_name"] + " any time, here or on Telegram.") + extra
    talk = "" if both else f'<div class="talk" data-key="talk{s["talk"][-1]["id"] if s["talk"] else 0}">{talk}</div>'
    return f'''<section class="block {size} needsb" id="pip" data-key="needs" data-kind="needs">
<div class="nhead">{pip_face(40, fam["agent_character"])}<div><div class="label">{a} · {greeting(s["now"])}</div>
<h2>{title}</h2></div></div>{f'<p class="pipsays">{line}</p>' if line else ""}
<div class="nlist">{shown}</div>{talk}{ask}
</section>'''


def task_row(t, s, big=False):
    p = s["by_id"].get(t["person_id"])
    col = p["colour"] if p else "ink"
    fg = "light" if col in LIGHT_TEXT or col == "ink" else "dark"
    mark = (f'<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M2 5.2l2 2L8 3"/></svg>' if big and t["done_at"]
            else e(p["name"][0]) if p and not big else "")
    state = "done" if t["done_at"] else ""
    verb = "Not done yet" if t["done_at"] else "Done"
    pts = f'<i class="pts">+{t["points"]}</i>' if t.get("points") else ""
    return (f'<form method="post" action="/do/task/{t["id"]}/toggle" class="task {state}{" big" if big else ""}" data-key="t{t["id"]}">'
            f'<button class="tick c-{col} {fg}" title="{verb}: {e(t["title"])}">{mark}</button>'
            f'<span>{e(t["title"])}</span>{pts}</form>')


def block_tasks(s, size="M"):
    tasks = s["tasks"]
    today0 = datetime.combine(s["today"], datetime.min.time(), s["now"].tzinfo)
    done = sum(1 for t in tasks if t["done_at"])
    open_ = [t for t in tasks if not t["done_at"]]
    if size == "S":
        first = f'<div class="sline">{e(open_[0]["title"])}</div>' if open_ else '<div class="sline">Nothing to do. Lovely.</div>'
        return (f'<section class="block S tasks" data-key="tasks" data-kind="tasks"><div class="label">Who does what</div>'
                f'{first}<div class="note bottom">{done} of {len(tasks)} done</div></section>')
    shown = tasks if size == "L" else [t for t in tasks if not t["done_at"] or t["done_at"] >= today0]
    rows = "".join(task_row(t, s) for t in shown) or '<div class="empty">Nothing to do. Lovely.</div>'
    note = f"{done} of {len(tasks)} done" + (" this week" if size == "L" else "")
    return f'''<section class="block {size} tasks" data-key="tasks" data-kind="tasks">
<div class="top"><h2>Who does what</h2><span class="note">{note}</span></div>
<div class="tasklist">{rows}</div></section>'''


def ring_svg(frac, size, cls=""):
    """A progress ring: a quiet track and the part done."""
    r = size / 2 - 4
    c = 2 * math.pi * r
    return (f'<svg class="pring {cls}" width="{size}" height="{size}" viewBox="0 0 {size} {size}" aria-hidden="true">'
            f'<circle class="trk" cx="{size / 2}" cy="{size / 2}" r="{r:.1f}"/><circle class="val" cx="{size / 2}" cy="{size / 2}" '
            f'r="{r:.1f}" stroke-dasharray="{c * max(0, min(1, frac)):.1f} {c:.1f}" transform="rotate(-90 {size / 2} {size / 2})"/></svg>')


def dream_points(s, dr):
    """(the child, points so far, the target) for a dream a child earns with chores, or None."""
    p = s["by_id"].get(dr.get("points_for"))
    if not p or not dr.get("points_target"):
        return None
    have = dr["points_target"] if dr["lived_on"] else max(0, min(s["points"].get(p["id"], 0), dr["points_target"]))
    return p, have, dr["points_target"]


def points_ring(s, dr, size=40):
    got = dream_points(s, dr)
    if not got:
        return ""
    p, have, want = got
    return (f'<span class="ptsring c-{p["colour"]}">{ring_svg(have / want, size)}'
            f'<span><b>{e(p["name"])}</b>: {have} of {want} points</span></span>')


def goal_of(s, dr):
    return next((g for g in s["goals"] if g["dream_id"] == dr["id"]), None)


def dream_cover(dr, cls=""):
    """A dream's picture: the family's own photo when it has one (planned or lived), else its drawn cover."""
    if dr["photo_id"]:
        return f'<img class="cover {cls}" src="/photo/{dr["photo_id"]}" alt="" loading="lazy">'
    return f'<i class="cover cover-{dr["cover"]} {cls}"></i>'


def dream_when(dr, today):
    if dr["on_date"]:
        n = days_to(dr["on_date"], today)
        return f"{n} days" if n > 1 else "tomorrow" if n == 1 else "today" if n == 0 else dr["on_date"].strftime("%B %Y")
    return "someday"


def block_dreams(s, size="M"):
    open_ = [dr for dr in s["dreams"] if not dr["lived_on"]]
    if not open_:
        return (f'<section class="block {size} dream" data-key="dreams" data-kind="dreams"><div class="label">Dreams</div>'
                f'<div class="stitle">What would you love to do together?</div>'
                f'<a class="link bottom" href="/dreams/new">Add a dream</a></section>')
    if size == "S":
        dr = open_[0]
        g = goal_of(s, dr)
        money = f'<div class="srow">{settings(g, "small")}</div>' if g else ""
        return (f'<a class="block S dream" href="/dreams/{dr["id"]}" data-key="dreams" data-kind="dreams">'
                f'{dream_cover(dr)}<div class="label">Dream · {dream_when(dr, s["today"])}</div>'
                f'<div class="stitle">{e(dr["title"])}</div>{money}</a>')
    rows = []
    moving = sorted(open_, key=lambda dr: not (goal_of(s, dr) or dream_points(s, dr)))  # stable: dated ones stay first
    for dr in moving[:3 if size == "L" else 2]:
        g = goal_of(s, dr)
        right = (f'<div class="days"><b>{days_to(dr["on_date"], s["today"])}</b><span>days</span></div>'
                 if dr["on_date"] and dr["on_date"] >= s["today"] else f'<span class="note">{dream_when(dr, s["today"]).capitalize()}</span>')
        rows.append(f'<a class="drow" href="/dreams/{dr["id"]}" data-key="dr{dr["id"]}"><div><div class="dtitle">{e(dr["title"])}</div>'
                    f'{settings(g) if g else points_ring(s, dr, 28)}</div>{right}</a>')
    return (f'<section class="block {size} dream" data-key="dreams" data-kind="dreams"><div class="top"><h2>Dreams</h2>'
            f'<a class="note" href="/dreams">Dream board</a></div><div class="drows">{"".join(rows)}</div></section>')


def block_countdowns(s, size="M"):
    items = countdown_list(s)[:{"S": 1, "M": 3, "L": 6}[size]]
    if not items:
        return (f'<section class="block {size} countdowns empty" data-key="countdowns" data-kind="countdowns">'
                f'<div class="label">Countdowns</div><div class="sline">Tell {e(s["fam"]["agent_name"])} a birthday or a trip: '
                f'"Count down to Oskar\'s birthday on 15 October".</div></section>')
    pages = "".join(page_card(d, what, key, s["today"]) for d, what, key in items)
    return f'<section class="block {size} countdowns" data-key="countdowns" data-kind="countdowns">{pages}</section>'


def month_bar(m, label=False):
    """One bar for the month: Bills, Living and Dreams, then what is left, as shares of what came in (or the plan)."""
    parts = [("b", "Bills", m["bills"]), ("l", "Living", m["living"]), ("d", "Dreams", m["dreams"]),
             ("r", "Left", max(0.0, m["left"] or 0))]
    total = max(m["base"] or 0, m["bills"] + m["living"] + m["dreams"]) or 1
    bar = ('<div class="mbar3" aria-hidden="true">' + "".join(f'<i class="{k}" style="flex-grow:{v / total:.4f}"></i>'
                                                             for k, _, v in parts if v > 0) + "</div>")
    if not label:
        return bar
    key = "".join(f'<span><i class="{k}"></i>{w} <b>{eur(v)}</b></span>' for k, w, v in parts[:3])
    of = f'of the <b>{eur(m["plan"])}</b> plan' if m["from_plan"] else f'of <b>{eur(m["came"])}</b> that came in'
    return bar + f'<div class="mkey">{key}<span class="of">{of}</span></div>'


def left_words(m):
    if m["left"] >= 0:
        return "left to live on"
    return "over the plan" if m["from_plan"] else "more than came in"


def block_money(s, size="M"):
    m = s["month"]
    head = f'<div class="top"><h2>{m["name"]}</h2><a class="note" href="/money">Money</a></div>'
    if m["base"] is None:
        return (f'<section class="block {size} money" data-key="money" data-kind="money">{head}'
                f'<div class="sline">Write down what comes in, like "salary 3200", and you\'ll always see what is left to live on.</div>'
                f'<a class="link bottom" href="/money">Open Money</a></section>')
    amount = eur(round(abs(m["left"])))
    if size == "S":
        return (f'<a class="block S money" href="/money" data-key="money" data-kind="money"><div class="label">{m["name"]}</div>'
                f'<div class="big">{amount}</div><div class="note">{left_words(m)}</div></a>')
    due = sorted(((money.bill_state(b, s["today"]), b) for b in s["bills"]), key=lambda x: (x[0][2], x[1]["id"]))
    open_ = [(st, b) for st, b in due if st[0] != "paid"]
    bills = " ".join(f'<span class="{st[0]}"><b>{st[1]}</b> {e(b["title"])} {eur(b["amount"])}</span>' for st, b in open_[:2]) \
        or ('<span>All bills paid this month</span>' if due else "")
    pot = ""
    if size == "L" and s["goals"]:
        g = max(s["goals"], key=lambda g: (g["saved"] < g["target"], g["dream_id"] is not None, -g["id"]))
        where = f' · {e(g["place"])}' if g["place"] else ""
        pot = (f'<div class="mgoal"><div class="top"><b>{e(g["title"])}</b><span class="note">{goal_words(g)}{where}</span></div>'
               f'{settings(g)}</div>')
    return (f'<section class="block {size} money" data-key="money" data-kind="money">{head}'
            f'<div class="mline"><span class="big">{amount}</span><span>{left_words(m)}, {m["to_go"]} days to go</span></div>'
            f'{month_bar(m)}<div class="bills">{bills}</div>{pot}</section>')


def block_routines(s, size="M"):
    part = routine_part(s["now"])
    rs = sorted(s["routines"], key=lambda r: r["part"] != part)
    if not rs:
        return (f'<section class="block {size} routines empty" data-key="routines" data-kind="routines"><div class="label">Routines</div>'
                f'<div class="sline">Picture steps a child ticks, like "Oskar\'s morning: wake up, get dressed, breakfast, teeth, '
                f'school bag". Tell {e(s["fam"]["agent_name"])}, or</div><a class="link" href="/routines">make one here</a></section>')
    if size == "S":
        r = rs[0]
        done = s["routine_done"].get(r["id"], {})
        dots = "".join(f'<i class="{"on" if i in done else ""}"></i>' for i in range(len(r["steps"])))
        return (f'<a class="block S routines" href="/routines" data-key="routines" data-kind="routines"><div class="label">Routine</div>'
                f'<div class="stitle">{e(routine_title(r))}</div><div class="rdots">{dots}</div>'
                f'<div class="note bottom">{len(done)} of {len(r["steps"])}</div></a>')
    body = "".join(routine_html(s, r) for r in rs[:2 if size == "L" else 1])
    return f'<section class="block {size} routines" data-key="routines" data-kind="routines">{body}</section>'


def block_weather(s, w, size="S"):
    if not w:
        return (f'<section class="block {size} weatherb" data-key="weather" data-kind="weather"><div class="label">Weather</div>'
                f'<div class="sline">No weather yet. It needs the family\'s town, set when the box is made.</div></section>')
    more = ""
    if size != "S":
        rain = f'Rain after {hm(w["rain"])}' if w["rain"] else "No rain today"
        more = f'<div class="sline">{e(w["words"])}. {rain}.</div>'
    if size == "L" and w.get("hours"):
        more += '<div class="hours">' + "".join(f'<span><b>{hm(t)}</b>{temp}°</span>' for t, temp in w["hours"]) + "</div>"
    return (f'<section class="block {size} weatherb" data-key="weather" data-kind="weather"><div class="label">Weather</div>'
            f'<div class="big">{w["temp"]}°</div>{more}<p class="credit">MET Norway</p></section>')


def block_photos(s, size="M"):
    ph = s["photos"][:{"S": 1, "M": 2, "L": 8}[size]]
    if not ph:
        return (f'<section class="block {size} photos empty" data-key="photos" data-kind="photos"><div class="label">Photos</div>'
                f'<div class="sline">Your own photos and drawings, on the table and on the wall.</div>'
                f'<a class="link bottom" href="/photos">Add photos</a></section>')
    imgs = "".join(f'<img src="/photo/{p["id"]}" alt="{e(p["caption"])}" loading="lazy"{" class=on" if i == 0 else ""}>'
                   for i, p in enumerate(ph))
    return (f'<a class="block {size} photos{" slides" if size == "L" else ""}" href="/photos" data-key="photos{ph[0]["id"]}" '
            f'data-kind="photos">{imgs}</a>')


def block_meals(s, size="M"):
    meals = s["meals"]
    tonight = next((m for m in meals if m["on_date"] == s["today"]), None)
    if size == "S":
        return (f'<section class="block S meals" data-key="meals" data-kind="meals"><div class="label">Tonight</div>'
                f'<div class="stitle">{e(tonight["title"]) if tonight else "Not planned yet"}</div></section>')
    rows = "".join(leader(day_name(m["on_date"], s["today"]), m["title"], f"m{m['id']}") for m in meals[:5])
    rows = rows or (f'<div class="empty">Tell {e(s["fam"]["agent_name"])}: "Dinner on Friday: pizza".</div>')
    shop = ""
    if size == "L":
        items = "".join(f'<form method="post" action="/do/shopping/{x["id"]}/toggle" class="task{" done" if x["done_at"] else ""}" '
                        f'data-key="sh{x["id"]}"><button class="tick c-ink light" title="Got it: {e(x["title"])}"></button>'
                        f'<span>{e(x["title"])}</span></form>' for x in s["shopping"][:8])
        shop = (f'<h2 class="sub">Shopping</h2><div class="tasklist">{items or "<div class=empty>Nothing to buy.</div>"}</div>')
    return (f'<section class="block {size} meals" data-key="meals" data-kind="meals"><div class="top"><h2>Meals</h2>'
            f'<span class="note">This week</span></div><div class="lines">{rows}</div>{shop}</section>')


def block_notes(s, size="M"):
    notes = s["notes"][:{"S": 1, "M": 3, "L": 6}[size]]
    pins = "".join(f'<div class="pin" data-key="n{n["id"]}"><span>{e(n["text"])}</span>'
                   f'{post(f"/do/note/{n["id"]}/remove", "×", "x", None)}</div>' for n in notes)
    add = ("" if size == "S" else
           '<form method="post" action="/do/note/0/add" class="act pinadd"><label class="sr" for="pinadd">Pin a note</label>'
           '<input id="pinadd" name="text" maxlength="200" placeholder="Pin a note" autocomplete="off"></form>')
    return (f'<section class="block {size} notes" data-key="notes" data-kind="notes"><div class="label">Notes</div>'
            f'<div class="pins">{pins or "<div class=empty>Nothing pinned.</div>"}</div>{add}</section>')


def tour_box(n, s, inner, text):
    """The two-tap tour after moving in: the rest of the screen dims, `inner` gets a ring and a card."""
    if s.get("tour") != n:
        return inner
    nxt = ('<a class="btn raised" href="/?tour=2">Next</a><form method="post" action="/welcome/8" class="inline">'
           '<button class="quietlink">Skip</button></form>' if n == 1 else
           '<form method="post" action="/welcome/8" class="inline"><button class="btn raised">Done</button></form>')
    return (f'<div class="tourwrap tour{n}">{inner}<div class="tourcard" role="dialog" aria-label="Tour">'
            f'<div class="label">{n} of 2</div><p class="tourtitle">{e(text)}</p><div class="row">{nxt}</div></div></div>')


def status_strip(s, short=False):
    st = s["status"]
    if not st:
        return '<div class="status empty" data-key="status"></div>'
    detail = f'<span class="detail"> · {e(st["detail"])}</span>' if st["detail"] and not short else ""
    return (f'<div class="status" data-key="st{st["id"]}"><i class="dot"></i><span>{e(st["sentence"])}</span>{detail}'
            f'{post(f"/do/status/{st["id"]}/done", st["action_label"], "link tomato")}</div>')


def person_view(s, p):
    lines = today_lines(s, p)
    n = len([ev for ev in on_day(s["events"], s["today"]) if ev["person_id"] in (None, p["id"])])
    jobs = [t for t in s["tasks"] if t["person_id"] == p["id"]]
    st = s["status"]
    st_line = (f'<div class="needs"><i class="dot"></i>{e(st["sentence"])}: a grown-up signs it</div>'
               if st and st["person_id"] == p["id"] else "")
    jobs_html = "".join(task_row(t, s, big=True) for t in jobs) or '<div class="empty">No jobs today.</div>'
    things = "Nothing planned" if not n else f"{n} thing{'s' if n != 1 else ''}"
    pts = s["points"].get(p["id"], 0)
    things += " today"
    if pts or any(dr.get("points_for") == p["id"] for dr in s["dreams"]):
        things += f" · {pts} point{'s' if pts != 1 else ''}"
    chore = (f'<form method="post" action="/do/task/0/add" class="act choreform"><input type="hidden" name="person" value="{p["id"]}">'
             f'<label class="sr" for="chore{p["id"]}">A job for {e(p["name"])}</label><input id="chore{p["id"]}" name="text" maxlength="120" '
             f'placeholder="Tidy the shoes for 10 points" autocomplete="off"><button class="btn sunk">Add</button></form>')
    routines = "".join(f'<section class="block routines" data-key="pr{r["id"]}">{routine_html(s, r)}</section>'
                       for r in s["routines"] if r["person_id"] == p["id"])
    shared = [dr for dr in s["dreams"] if p["id"] in (dr["sharers"] or []) and not dr["lived_on"]]
    dreams = ""
    if shared:
        words = "one dream" if len(shared) == 1 else f"{len(shared)} dreams"
        dreams = (f'<section class="block" data-key="pdreams"><h2>{e(p["name"])} shares {words}</h2>'
                  + "".join(f'<a class="line" href="/dreams/{dr["id"]}"><span>{e(dr["title"])}</span><i class="dots"></i>'
                            f'<span class="note">{dream_when(dr, s["today"])}</span></a>{points_ring(s, dr, 32)}' for dr in shared) + "</section>")
    return f'''<div class="person">
<section class="block who" data-key="who{p["id"]}">{chair(p, 56)}<div><h2 class="title">{e(p["name"])}'s {s["now"].strftime("%A")}</h2>
<div class="note">{things}</div></div></section>
<section class="block" data-key="pday">{lines}</section>
<section class="block" data-key="pjobs"><h2>{e(p["name"])}'s jobs</h2><div class="tasklist">{jobs_html}</div>{st_line}{chore}</section>
{routines}{dreams}
</div>'''


def render_block(s, b, w, look):
    k, size = b["kind"], b["size"]
    return {
        "clock": lambda: block_clock(s, w, size), "pip": lambda: block_pip(s, look, size), "today": lambda: block_today(s, size),
        "tasks": lambda: block_tasks(s, size), "dreams": lambda: block_dreams(s, size),
        "countdowns": lambda: block_countdowns(s, size), "money": lambda: block_money(s, size),
        "routines": lambda: block_routines(s, size), "weather": lambda: block_weather(s, w, size),
        "photos": lambda: block_photos(s, size), "meals": lambda: block_meals(s, size), "notes": lambda: block_notes(s, size),
        "needs": lambda: block_needs(s, look, size), "day": lambda: block_day(s, w, size),
    }[k]()


def edit_slot(s, b, html_):
    name = s["fam"]["agent_name"] if b["kind"] == "pip" else BLOCKS[b["kind"]][0]
    k = b["kind"]
    sizes = "".join(f'<form method="post" action="/do/block/0/size" class="act"><input type="hidden" name="kind" value="{k}">'
                    f'<input type="hidden" name="size" value="{z}"><button class="{"on" if z == b["size"] else ""}" '
                    f'aria-label="{e(name)}: size {z}">{z}</button></form>' for z in "SML")
    nxt = "SML"[("SML".index(b["size"]) + 1) % 3]
    return (f'<div class="eslot {b["size"]}" data-kind="{k}" data-key="slot-{k}-{b["size"]}">{html_}'
            f'<div class="ectl"><span class="elabel">{e(name)} · {b["size"]}</span>'
            f'<form method="post" action="/do/block/0/hide" class="act ex"><input type="hidden" name="kind" value="{k}">'
            f'<button aria-label="Take {e(name)} off the table">×</button></form>'
            f'<div class="sml">{sizes}</div>'
            f'<form method="post" action="/do/block/0/move" class="act emove"><input type="hidden" name="kind" value="{k}">'
            f'<button name="dir" value="-1" aria-label="Move {e(name)} earlier">‹</button>'
            f'<button name="dir" value="1" aria-label="Move {e(name)} later">›</button></form>'
            f'<form method="post" action="/do/block/0/size" class="act ecorner"><input type="hidden" name="kind" value="{k}">'
            f'<input type="hidden" name="size" value="{nxt}"><button aria-label="Make {e(name)} size {nxt}"></button></form>'
            f'</div></div>')


def shelf(s):
    agent = e(s["fam"]["agent_name"])
    ready = "".join(f'<form method="post" action="/do/block/0/show" class="act shelfitem" data-kind="{b["kind"]}">'
                    f'<input type="hidden" name="kind" value="{b["kind"]}"><button><b>{e(BLOCKS[b["kind"]][0])}</b>'
                    f'<span>{e(BLOCKS[b["kind"]][2])}</span></button></form>' for b in s["layout"] if not b["shown"])
    return f'''<aside class="shelf" aria-label="Block shelf"><h2 class="title">Block shelf</h2>
<div class="buildbox">{pip_face(28, s["fam"]["agent_character"])}<b>Ask {agent} to build a block</b>
<input disabled placeholder="Show the bus times to school" aria-label="Describe a block">
<div class="row"><span class="btn sunk off">Build it</span><span class="note">{agent} builds it on your box with your own AI account. Coming soon.</span></div></div>
<div class="label">Ready to add</div><div class="ready">{ready or '<div class="empty">Every block is on your table.</div>'}</div>
<div class="label">Built by your family</div><div class="empty">Blocks you ask {agent} to build will wait here.</div>
{post("/do/block/0/reset", "Put everything back as it was", "quietlink small")}{home_switch(s)}
</aside>'''


def home_switch(s):
    """In edit mode: try the new home (families who have the first one), or go back to the first one after trying."""
    home = s["fam"]["home"]
    if home in ("old", "kept") and not s["arranged"]:  # "Put everything back" first: the arrangement is theirs
        return post("/do/home/0/new", "Use the new home", "quietlink small")
    return post("/do/home/0/old", "Back to the first home", "quietlink small") if home == "tried" else ""


def bottom_bar(fam, here="home"):
    items = [("home", "/", "home", "Home"), ("calendar", "/calendar", "cal", "Calendar"), None,
             ("money", "/money", "money", "Money"), ("dreams", "/dreams", "dreams", "Dreams")]
    out = []
    for it in items:
        if it is None:
            out.append(f'<a href="/#pip" class="pipbtn" aria-label="{e(fam["agent_name"])}">{pip_face(52, fam["agent_character"])}</a>')
            continue
        k, href, icon, name = it
        out.append(f'<a href="{href}"{" class=on" if k == here else ""}><i class="{icon}"></i>{name}</a>')
    return f'<nav class="bottom phone-only" aria-label="Sections">{"".join(out)}</nav>'


def header(s, person=None, edit_button=True):
    fam, people = s["fam"], s["people"]
    addr = welcome.address(fam)
    who_link = lambda p: "/?who=" + urllib.parse.quote(p["name"].lower())
    chairs = "".join(chair(p, 36, link=who_link(p), on=person is p) for p in people)
    chips = [f'<a href="/" class="pchip ink{" on" if not person else ""}"><i></i>Everyone</a>']
    for p in people:
        fg = "light" if p["colour"] in LIGHT_TEXT else "dark"
        chips.append(f'<a href="{e(who_link(p))}" class="pchip{" on" if person is p else ""}">'
                     f'<i class="c-{p["colour"]} {fg}">{e(p["name"][0])}</i>{e(p["name"])}</a>')
    themes = "".join(f'<button data-theme-set="{k}" class="{"on" if s["theme"] == k else ""}">{v}</button>'
                     for k, v in (("light", "Day"), ("night", "Night"), ("paper", "Paper"), ("auto", "Auto")))
    tour1 = "Tap a chair to see one person's day."
    edit = '<a class="btn sunk" href="/?edit=1">Edit</a>' if edit_button else ""
    return f'''<header class="top-bar">
<div class="phone-only phonebar"><b data-clock>{hm(s["now"])}</b><span>{e(addr)}</span></div>
<div class="lockup"><a href="/" class="home-link">{ring(48, 4, 17, [p["colour"] for p in people])}<h1>The {e(fam["name"])} Family</h1></a>
<div class="laptop-only">{tour_box(1, s, f'<nav class="chairs" aria-label="Show one person">{chairs}<a href="/#pip" class="pipring" title="{e(fam["agent_name"])}">{pip_face(36, fam["agent_character"])}</a></nav>', tour1)}</div></div>
<div class="right laptop-only"><div class="themes" role="group" aria-label="Theme">{themes}</div>{edit}<span class="addr">{e(addr)}</span></div>
</header>
<div class="phone-only">{tour_box(1, s, f'<nav class="pchips" aria-label="Show one person">{"".join(chips)}</nav>', tour1)}</div>'''


def main_attrs(s):
    sun = s.get("sun")
    return (f'data-tz="{e(s["fam"]["tz"])}" data-live' + (f' data-sun="{hm(sun[0])},{hm(sun[1])}"' if sun else ""))


def page(s, who, theme, partial=False, look=None, edit=False):
    fam, people = s["fam"], s["people"]
    s["theme"] = theme
    person = next((p for p in people if p["name"].lower() == who), None)
    w = weather(fam, s["now"])
    shown = [b for b in s["layout"] if b["shown"]]
    kinds = [b["kind"] for b in shown]
    s["needs_shown"], s["pip_shown"] = "needs" in kinds, "pip" in kinds
    if (fam["home"] in ("new", "tried") and not s["arranged"] and not edit and s["moment"] in ("morning", "evening")
            and "routines" in kinds and "day" in kinds):  # the children's routine right under the day, at its time
        shown.insert(kinds.index("day") + 1, shown.pop(kinds.index("routines")))
    if person:
        body = person_view(s, person)
    elif edit:
        slots = "".join(edit_slot(s, b, render_block(s, b, w, None)) for b in shown)
        body = (f'<div class="editwrap"><div class="grid editing" data-arrange>{slots}'
                f'<div class="dropslot">+ Drop a block here</div></div>{shelf(s)}</div>')
    else:
        bands, cur = [], []
        for b in shown:  # the needs strip is a band of its own across the table; the blocks sit in the grid around it
            got = render_block(s, b, w, look)
            if b["kind"] == "needs":
                bands += ([f'<div class="grid">{"".join(cur)}</div>'] if cur else []) + [got]
                cur = []
            else:
                cur.append(got)
        bands += [f'<div class="grid">{"".join(cur)}</div>'] if cur else []
        offer = ""
        if fam["home"] == "old" and not s["arranged"]:  # a family's own arrangement is never swapped by one tap
            offer = (f'<div class="newhome" data-key="newhome"><p><b>A calmer home screen is ready.</b> What needs you first, '
                     f'then your day.</p><div class="row">{post("/do/home/0/new", "Use the new home", "btn sunk")}'
                     f'{post("/do/home/0/keep", "Not now", "link")}</div></div>')
        body = (offer + "".join(bands)
                + ('<p class="credit">Weather from MET Norway</p>' if w else "")
                + f'<nav class="foot"><a class="quietlink small" href="/?edit=1">Arrange your table</a>'
                  f'<a class="quietlink small" href="/wall">Use this screen on the wall</a>'
                  f'<a class="quietlink small" href="/pip">{e(fam["agent_name"])}\'s thinking</a>'
                  f'<a class="quietlink small" href="/support">Something not right?</a>'
                  f'<a class="quietlink small" href="https://fam.box/withdraw" lang="et">Taganen lepingust</a></nav>'
                + f'<p class="later">Coming later: your family\'s own websites, and blocks {e(fam["agent_name"])} builds for you.</p>'
                + ('<form method="post" action="/do/demo/0/reset" class="demo"><button class="quietlink small">'
                   'Start the demo again</button></form>' if os.environ.get("FAMBOX_DEMO") else ""))
    if edit:
        top = (f'<header class="edithead"><h1 class="title">Arrange your table</h1><p>Drag to move, pull a corner to resize. '
               f'Or tell {e(fam["agent_name"])}: "Make the calendar bigger".</p><a class="btn raised" href="/">Done</a></header>')
    else:
        top = header(s, person)
        if person or not s["needs_shown"]:  # the needs strip already says it
            top += f'<div class="laptop-only">{status_strip(s)}</div><div class="phone-only">{status_strip(s, short=True)}</div>'
    main = f'<main data-who="{e(who or "")}" {main_attrs(s)}>{top}{body}</main>'
    if partial:
        return main
    return document(f'The {fam["name"]} Family', main, theme=shown_theme(s, theme), bottom=bottom_bar(fam))


THEMES = ("light", "night", "paper", "auto")  # the switch: Day stays day, Night stays night, Auto follows the sun


def chosen_theme(cookie):
    """The screen's choice from its cookie. The old "day" was Warm Day, which followed the sun: that is Auto now."""
    return cookie if cookie in THEMES else "auto"


def shown_theme(s, chosen):
    """The look drawn (tokens.css data-theme). Auto is Night between sunset and sunrise (when the box knows where the
    family lives), Day otherwise."""
    if chosen == "auto":
        sun = s.get("sun")
        return "night" if sun and not sun[0] <= s["now"] < sun[1] else "day"
    return {"light": "day"}.get(chosen, chosen)


def sun_times(lat, lon, d, tz):
    """Sunrise and sunset in the family's time zone (NOAA's approximation, within a few minutes). None in polar day or night."""
    g = 2 * math.pi / 365 * (d.timetuple().tm_yday - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    la = math.radians(lat)
    x = math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(decl)) - math.tan(la) * math.tan(decl)
    if abs(x) > 1:
        return None
    ha = math.degrees(math.acos(x))
    noon = 720 - 4 * lon - eqt
    utc0 = datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("UTC"))
    return tuple((utc0 + timedelta(minutes=noon + k * 4 * ha)).astimezone(tz) for k in (-1, 1))


def document(title, main, css=None, theme="day", bottom=""):
    extra = f'<link rel="stylesheet" href="/static/{css}.css">' if css else ""
    return f'''<!doctype html>
<html lang="en" data-theme="{e(theme)}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<link rel="stylesheet" href="/static/tokens.css"><link rel="stylesheet" href="/static/style.css">{extra}
<script src="/static/app.js" defer></script></head>
<body>{main}{bottom}
</body></html>'''


def wall_page(s, partial=False):
    """Wall mode: across the room. Morning (far blocks only), night (tomorrow, a sleepy agent, dims at 22:30)
    and, after five quiet minutes, a slow frame of the family's own photos. app.js switches between them."""
    fam, now, today = s["fam"], s["now"], s["today"]
    w = weather(fam, now)
    wwords = f'{w["temp"]}°, {(w["words"] + (f", rain after {hm(w["rain"])}" if w["rain"] else "")).lower()}' if w else ""
    nx = next_event(s)
    nxt = (f'<div class="label">Next</div><div class="wnext"><i class="pdot {colour_of(nx, s["by_id"])}"></i>'
           f'{e(nx["title"])} {hm(nx["start"])}</div>' if nx else "")
    line = e(fam["agent_line"] or "")
    cds = "".join(page_card(d, what, key, today, wall=True) for d, what, key in countdown_list(s)[:2])
    st = s["status"]
    need = f'<div class="wneed"><i class="dot"></i>{e(st["sentence"])}</div>' if st else '<div class="wneed empty"></div>'
    tom = today + timedelta(1)
    trows = "".join(leader("All day" if ev["all_day"] else hm(ev["start"]),
                           ev["title"] + (f', {s["by_id"][ev["person_id"]]["name"]}' if ev["person_id"] in s["by_id"] else ""),
                           f"w{ev['id']}") for ev in on_day(s["events"], tom)[:4]) or '<div class="empty">Nothing planned.</div>'
    night = now.hour >= 20 or now.hour < 5
    photos = s["photos"][:20]
    frames = "".join(f'<img src="/photo/{p["id"]}" alt="" data-caption="{e(p["caption"])}"{" class=on" if i == 0 else ""}>'
                     for i, p in enumerate(photos)) or '<div class="drawn on" aria-hidden="true"><i></i><i></i><i></i></div>'
    caption = e(photos[0]["caption"]) if photos else "Your own photos will show here. Add them in Photos."
    cols = [p["colour"] for p in s["people"]]
    main = f'''<main class="wall" data-wall data-who="" {main_attrs(s)} data-night="{"1" if night else ""}">
<section class="wview wday"{" hidden" if night else ""}>
<header class="whead"><div class="lockup">{ring(40, 3.6, 15, cols)}<h1>The {e(fam["name"])} Family</h1></div><span data-date>{e(now.strftime("%A %-d %B"))}</span></header>
<div class="wbody"><div class="wclock"><div class="time" data-clock>{hm(now)}</div><div class="wwords">{e(wwords)}</div></div>
<div class="wright">{nxt}<div class="wpip">{pip_face(52, fam["agent_character"])}<span>{line}</span></div><div class="wcd">{cds}</div></div></div>
{need}</section>
<section class="wview wnight"{"" if night else " hidden"}>
<header class="whead"><h1>The {e(fam["name"])} Family</h1><span>Dims at 22:30</span></header>
<div class="wbody"><div class="wclock"><div class="time" data-clock>{hm(now)}</div><div class="wwords">{e(wwords)}</div></div>
<div class="wright"><h2>Tomorrow</h2><div class="lines">{trows}</div></div></div>
<div class="wpip">{pip_face(52, fam["agent_character"], "sleepy")}<span>Good night. I'll wake you at 07:00.</span></div></section>
<section class="wview widle" hidden>{frames}
<div class="wframe"><div class="time" data-clock>{hm(now)}</div><div class="wcap" data-cap>{caption}</div>
<div class="wcapnext">{f"Next: {e(nx['title'])} {hm(nx['start'])}" if nx else ""}</div></div></section>
</main>'''
    if partial:
        return main
    return document(f'The {fam["name"]} Family', main, theme="wall")


# ---------- changing things ----------

def accept(c, sid, tz):
    sg = c.execute("select * from family.suggestions where id = %s and state = 'pending' for update",
                   (sid,)).fetchone()
    if not sg:
        return
    people = {p["name"].lower(): p["id"] for p in c.execute("select id, name from family.people").fetchall()}
    for it in sg["items"]:
        try:
            who = people.get(str(it.get("person") or "").lower())
            if it.get("task"):
                c.execute("insert into family.tasks (title, person_id, suggestion_id) values (%s, %s, %s)",
                          (str(it["task"]), who, sid))
                continue
            if it.get("goal"):  # Pip's payday or first-of-the-month reminder: put the month's share into a pot
                money.pot_move(c, int(it["goal"]), float(it["amount"]), "Saved first" if it.get("payday") else "A new month")
                continue
            d = date.fromisoformat(it["date"])
            if it.get("time"):
                start = datetime.combine(d, datetime.strptime(it["time"][:5], "%H:%M").time(), tz)
                end, all_day = None, False
            else:
                start = datetime.combine(d, datetime.min.time(), tz)
                end = datetime.combine(date.fromisoformat(it.get("end_date") or it["date"]), datetime.min.time(), tz)
                all_day = True
            title = str(it["title"])
        except (KeyError, TypeError, ValueError) as ex:  # Pip wrote an item we cannot read: skip it, keep the rest
            print(f"suggestion {sid}: skipped an item ({type(ex).__name__})", file=sys.stderr)
            continue
        c.execute("insert into family.events (title, starts_at, ends_at, all_day, person_id, suggestion_id)"
                  " values (%s, %s, %s, %s, %s, %s)", (title, start, end, all_day, who, sid))
    c.execute("update family.suggestions set state = 'accepted', decided_at = now() where id = %s", (sid,))


def amount(x):
    """Euros as people write them: "86", "86.50", "86,50", "€ 1 200". None when it is not a sensible amount."""
    x = re.sub(r"[€\s]|eur(os?)?", "", str(x or "").lower())
    if re.fullmatch(r"\d{1,3}([,.]\d{3})+", x):  # "2,400" or "1.200": grouped thousands
        x = re.sub(r"[,.]", "", x)
    x = x.replace(",", ".")
    if x.count(".") > 1:  # "1,200.50": the first marks group thousands
        x = x.replace(".", "", x.count(".") - 1)
    try:
        v = round(float(x), 2)
    except ValueError:
        return None
    return v if 0 < v < 1_000_000 else None


def day_of(x):
    try:
        d = date.fromisoformat(str(x))
    except ValueError:
        return None
    return d if 1900 <= d.year <= 2200 else None


def fund_name(title):
    """"Lapland in winter" -> "Lapland fund", "Grandma's 80th in Italy" -> "Italy fund", "See the northern lights" -> "Northern lights fund"."""
    words = title.split()
    caps = [w for w in words[1:] if w[:1].isupper()]
    if caps:
        name = caps[-1]
    elif words and words[0].lower() in ("see", "go", "learn", "build", "visit", "buy", "get", "take", "have", "make", "try", "cycle", "do"):
        rest = [w for w in words[1:] if w.lower() not in ("the", "a", "an", "to", "our")]
        name = " ".join(rest) or title
    else:
        name = words[0] if words else "Dream"
    return (name[0].upper() + name[1:]).strip(" .,!") + " fund"


def months_to(by, today):
    return max(1, (by.year - today.year) * 12 + by.month - today.month) if by else None


def save_dream(c, form, forms, people_ids, did=None):
    """The add-a-dream sheet (and its edit). Returns the dream's id, or a sentence saying what is missing."""
    title = form.get("title", "").strip()[:80]
    if not title:
        return "Give your dream a name."
    cover = int(form["cover"]) if form.get("cover", "").isdecimal() and int(form["cover"]) < DREAM_COVERS else 0
    sharers = [int(x) for x in forms.get("sharer", []) if x.isdecimal() and int(x) in people_ids]
    on = day_of(form.get("on_date")) if form.get("when") == "date" else None
    if form.get("when") == "date" and not on:
        return "Pick the date, or choose Someday."
    note = form.get("note", "").strip()[:80] or None
    target = amount(form.get("target")) if form.get("saving") else None
    if form.get("saving") and form.get("target", "").strip() and not target:
        return "Write the saving goal as an amount, like 2400."
    vals = (title, cover, sharers, on, note, bool(form.get("saving")))
    if did:
        c.execute("update family.dreams set title = %s, cover = %s, sharers = %s, on_date = %s, note = %s, saving = %s"
                  " where id = %s", vals + (did,))
    else:
        did = c.execute("insert into family.dreams (title, cover, sharers, on_date, note, saving) values"
                        " (%s, %s, %s, %s, %s, %s) returning id", vals).fetchone()["id"]
    if target:
        g = c.execute("select id from family.goals where dream_id = %s", (did,)).fetchone()
        if g:
            c.execute("update family.goals set target = %s, by_date = %s where id = %s", (target, on, g["id"]))
        else:
            c.execute("insert into family.goals (title, target, by_date, dream_id) values (%s, %s, %s, %s)",
                      (fund_name(title), target, on, did))
    return did


def act(path, tz, form=None, forms=None):
    """POST /do/<thing>/<id>/<verb>. Returns False for an unknown path, True to go back where the post came from,
    or the address to go to next."""
    parts = path.strip("/").split("/")
    if len(parts) != 4 or parts[0] != "do" or not parts[2].isdecimal():
        return False
    thing, i, verb = parts[1], int(parts[2]), parts[3]
    form, forms = form or {}, forms or {}
    today = datetime.now(tz).date()
    with db() as c, c.transaction():
        if (thing, verb) == ("task", "toggle"):
            c.execute("update family.tasks set done_at = case when done_at is null then now() end where id = %s", (i,))
            task_points(c, i)
        elif (thing, verb) == ("task", "add"):  # a chore for one person, maybe with points: "Tidy the shoes for 10 points"
            m = re.fullmatch(r"(.+?)(?:\s*(?:,|for|=|-|:)?\s*(\d{1,4})\s*(?:points?|pts?))?", form.get("text", "").strip(" .")[:120], re.I)
            pid = int(form["person"]) if form.get("person", "").isdecimal() else None
            if m and m[1].strip():
                c.execute("insert into family.tasks (title, person_id, points) values (%s, (select id from family.people where id = %s), %s)",
                          (m[1].strip()[:1].upper() + m[1].strip()[1:], pid, min(1000, int(m[2])) if m[2] and int(m[2]) else None))
        elif (thing, verb) == ("status", "done"):
            c.execute("update family.status set done_at = now() where id = %s", (i,))
        elif (thing, verb) == ("suggestion", "accept"):
            accept(c, i, tz)
        elif (thing, verb) == ("suggestion", "dismiss"):
            c.execute("update family.suggestions set state = 'dismissed', decided_at = now() where id = %s", (i,))
        elif thing == "suggestion" and verb.startswith("drop-") and verb[5:].isdecimal():
            n = int(verb[5:])
            c.execute("update family.suggestions set items = items - %s::int where id = %s and state = 'pending'"
                      " and items->%s->>'label' = %s", (n, i, n, form.get("label", "")))
            c.execute("update family.suggestions set state = 'dismissed', decided_at = now()"
                      " where id = %s and items = '[]'", (i,))
        elif (thing, verb) == ("ai", "save"):  # Pip's thinking: FamBox AI, the family's own key, or rules only
            mode = form.get("mode")
            if mode not in ("fambox", "own", "rules"):
                return "/pip"
            key = "".join(form.get("key", "").split())[:300]
            if mode == "own" and key:
                if form.get("provider") not in ai.PROVIDERS:
                    return "/pip?problem=provider"
                bad = ai.check_key(form["provider"], key)  # one tiny call; the key is never shown or logged
                if bad:
                    return f"/pip?problem={bad}&provider={form['provider']}"
                c.execute("update family.household set ai_provider = %s, ai_key = %s", (form["provider"], key))
            elif mode == "own" and not c.execute("select ai_key from family.household").fetchone()["ai_key"]:
                return "/pip?problem=nokey"
            c.execute("update family.household set ai_mode = %s, ai_state = null, ai_said_on = null", (mode,))
            return "/pip?saved=1"
        elif (thing, verb) == ("ai", "forget"):
            c.execute("update family.household set ai_key = null, ai_provider = null, ai_state = null,"
                      " ai_mode = case when ai_mode = 'own' then 'fambox' else ai_mode end")
            return "/pip?removed=1"
        elif (thing, verb) == ("demo", "reset") and os.environ.get("FAMBOX_DEMO"):
            fam = c.execute("select name, slug from family.household").fetchone()
            new_family(c, "Kask", fam["slug"])
        elif (thing, verb) == ("suggestion", "undo"):
            sg = c.execute("update family.suggestions set state = 'pending', decided_at = null"
                           " where id = %s and state = 'accepted' returning items", (i,)).fetchone()
            if sg:
                c.execute("delete from family.events where suggestion_id = %s", (i,))
                c.execute("delete from family.tasks where suggestion_id = %s", (i,))
                for it in sg["items"]:
                    if isinstance(it, dict) and str(it.get("goal", "")).isdecimal() and amount(it.get("amount")):
                        money.pot_move(c, int(it["goal"]), -amount(it["amount"]), "Undone")
        # the new home (phase D): offered once to families who had the first one; they can go back from edit mode
        elif (thing, verb) == ("home", "new"):
            c.execute("update family.household set home = case when home = 'new' then 'new' else 'tried' end")
            c.execute("delete from family.blocks")
            return "/"
        elif (thing, verb) == ("home", "keep"):
            c.execute("update family.household set home = 'kept' where home = 'old'")
        elif (thing, verb) == ("home", "old"):
            if c.execute("update family.household set home = 'kept' where home = 'tried'").rowcount:
                c.execute("delete from family.blocks")
            return "/"
        # the arrangement of the home screen (edit mode); the same on every device
        elif thing == "block" and verb in ("size", "hide", "show", "move", "order", "reset"):
            kind = form.get("kind", "")
            if verb == "size":
                arrange(c, kind, size=form.get("size"))
            elif verb == "hide":
                arrange(c, kind, shown=False)
            elif verb == "show":
                arrange(c, kind, shown=True, to=int(form["to"]) if form.get("to", "").isdecimal() else None)
            elif verb == "move" and form.get("dir") in ("-1", "1"):
                shown = [b["kind"] for b in layout(c) if b["shown"]]
                if kind in shown:
                    arrange(c, kind, to=max(0, shown.index(kind) + int(form["dir"])))
            elif verb == "order":
                for n, k in enumerate(form.get("order", "").split(",")[:len(BLOCKS)]):
                    arrange(c, k, to=n)
            elif verb == "reset":
                c.execute("delete from family.blocks")
        # routines: a child taps a step; it gets a star for today
        elif thing == "routine" and verb.startswith("step-") and verb[5:].isdecimal():
            if not c.execute("delete from family.routine_done where routine_id = %s and step = %s and on_day = %s",
                             (i, int(verb[5:]), today)).rowcount:
                c.execute("insert into family.routine_done (routine_id, step, on_day) select id, %s, %s from family.routines"
                          " where id = %s and jsonb_array_length(steps) > %s", (int(verb[5:]), today, i, int(verb[5:])))
            step_points(c, i, int(verb[5:]), today)
        elif (thing, verb) == ("routine", "points"):
            n = int(form["points"]) if form.get("points", "").isdecimal() else 0
            c.execute("update family.routines set points = %s where id = %s", (min(100, n) or None, i))
        elif (thing, verb) == ("routine", "add"):
            steps = [x.strip()[:24] for x in re.split(r"[,\n]", form.get("steps", "")) if x.strip()][:8]
            pid = int(form["person"]) if form.get("person", "").isdecimal() else None
            pts = min(100, int(form["points"])) if form.get("points", "").isdecimal() and int(form["points"]) else None
            if steps:
                c.execute("insert into family.routines (person_id, part, points, steps) values"
                          " ((select id from family.people where id = %s), %s, %s, %s)",
                          (pid, "evening" if form.get("part") == "evening" else "morning", pts,
                           json.dumps([{"name": x[0].upper() + x[1:], "icon": icon_for(x)} for x in steps])))
        elif (thing, verb) == ("routine", "remove"):
            c.execute("delete from family.routines where id = %s", (i,))
        elif (thing, verb) == ("shopping", "toggle"):
            c.execute("update family.shopping set done_at = case when done_at is null then now() end where id = %s", (i,))
        elif (thing, verb) == ("note", "add") and form.get("text", "").strip():
            c.execute("insert into family.notes (text) values (%s)", (form["text"].strip()[:200],))
        elif (thing, verb) == ("note", "remove"):
            c.execute("delete from family.notes where id = %s", (i,))
        # the calendar
        elif (thing, verb) == ("event", "add"):
            d = day_of(form.get("date"))
            title = form.get("title", "").strip()[:120]
            t = re.fullmatch(r"(\d{1,2})[:.](\d{2})", form.get("time", "").strip())
            if not d or not title or (t and not (int(t[1]) < 24 and int(t[2]) < 60)):
                return "/calendar?problem=1" + (f"&d={d.isoformat()}" if d else "") + "#add"
            start = datetime.combine(d, datetime.min.time(), tz).replace(hour=int(t[1]), minute=int(t[2])) if t else \
                datetime.combine(d, datetime.min.time(), tz)
            pid = int(form["person"]) if form.get("person", "").isdecimal() else None
            c.execute("insert into family.events (title, starts_at, all_day, person_id, countdown) values"
                      " (%s, %s, %s, (select id from family.people where id = %s), %s)",
                      (title, start, not t, pid, bool(form.get("countdown"))))
            return f"/calendar?d={d.isoformat()}"
        elif (thing, verb) == ("event", "remove"):
            c.execute("delete from family.events where id = %s", (i,))
        elif (thing, verb) == ("event", "countdown"):
            c.execute("update family.events set countdown = not countdown where id = %s", (i,))
        # two-way calendars (calsync.py); never on the demo, whose table strangers try
        elif thing == "cal" and os.environ.get("FAMBOX_DEMO"):
            return "/calendar#both"
        elif (thing, verb) == ("cal", "icloud"):
            login = form.get("login", "").strip()[:200]
            pw = "".join(form.get("password", "").split())[:100]  # never shown again, never logged
            if not login or not pw:
                return "/calendar?problem=icloud-empty#both"
            try:
                found = calsync.ICloud({"login": login, "secret": pw}, tz).calendars()
            except calsync.SignIn:
                return "/calendar?problem=icloud-signin#both"
            except Exception:
                return "/calendar?problem=icloud-offline#both"
            if not found:
                return "/calendar?problem=icloud-none#both"
            calsync.connect(c, "icloud", login, pw, found)
            calsync.soon()
            return "/calendar#both"
        elif (thing, verb) == ("cal", "pick"):
            if calsync.pick(c, i, form.get("calendar", "")):
                calsync.soon()
            return "/calendar#both"
        elif (thing, verb) == ("cal", "stop"):
            calsync.stop(c, i)
            return "/calendar#both"
        elif (thing, verb) == ("feed", "new"):
            c.execute("update family.household set feed_key = %s", (secrets.token_urlsafe(24),))
        # money: written in by hand, never alarming
        elif (thing, verb) == ("money", "plan"):
            if form.get("amount", "").strip() and not amount(form["amount"]):
                return "/money?problem=plan#plan"
            c.execute("update family.household set money_plan = %s", (amount(form.get("amount")),))
        elif (thing, verb) == ("spend", "add"):
            if not amount(form.get("amount")):
                return "/money?problem=spend#spend"
            c.execute("insert into family.spending (amount, note, spent_on) values (%s, %s, %s)",
                      (amount(form["amount"]), form.get("note", "").strip()[:80], today))
        elif (thing, verb) == ("spend", "remove"):
            c.execute("delete from family.spending where id = %s", (i,))
        elif (thing, verb) == ("income", "remove"):
            c.execute("delete from family.income where id = %s", (i,))
        elif (thing, verb) == ("bill", "add"):
            day = int(form["day"]) if form.get("day", "").isdecimal() and 1 <= int(form["day"]) <= 31 else None
            if not form.get("title", "").strip() or not amount(form.get("amount")) or not day:
                return "/money?problem=bill#bills"
            c.execute("insert into family.bills (title, amount, due_day) values (%s, %s, %s)",
                      (form["title"].strip()[:60], amount(form["amount"]), day))
        elif (thing, verb) == ("bill", "paid"):  # once a month; a second tap changes nothing
            money.mark_paid(c, i, today)
        elif (thing, verb) == ("bill", "unpaid"):  # a mistaken tap
            m0 = month_bounds(today)[0]
            if c.execute("update family.bills set paid_month = null where id = %s and paid_month >= %s", (i, m0)).rowcount:
                c.execute("delete from family.spending where id = (select id from family.spending where bill_id = %s"
                          " and spent_on >= %s order by id desc limit 1)", (i, m0))
        elif (thing, verb) == ("money", "payday"):
            n = int(form["day"]) if form.get("day", "").isdecimal() and 1 <= int(form["day"]) <= 31 else None
            c.execute("update family.household set payday = %s", (n,))
        elif (thing, verb) == ("statement", "accept"):
            shown = {int(k[1:]): v for k, v in form.items() if k[:1] == "a" and k[1:].isdecimal()}  # only the lines on the page
            if form.get("skip"):
                c.execute("update family.bank_lines set state = 'skipped' where state = 'new' and id = any(%s)", (list(shown) or [-1],))
            else:
                money.accept(c, shown, only=list(shown) or [-1])
            return "/money"
        elif (thing, verb) == ("kakeibo", "answer") and form.get("text", "").strip():
            m0 = day_of(form.get("month")) or month_bounds(today)[0]
            c.execute("insert into family.kakeibo (month, facts, answer, answered_at) values (%s, %s, %s, now()) on conflict (month)"
                      " do update set answer = excluded.answer, answered_at = now()",
                      (m0.replace(day=1), json.dumps(kakeibo_facts(c, m0)), form["text"].strip()[:2000]))
        elif (thing, verb) == ("journal", "remove"):
            c.execute("delete from family.journal where id = %s", (i,))
        elif (thing, verb) == ("dream", "points"):
            pid = int(form["person"]) if form.get("person", "").isdecimal() else None
            n = int(form["points"]) if form.get("points", "").isdecimal() and 0 < int(form["points"]) <= 100000 else None
            c.execute("update family.dreams set points_for = (select id from family.people where id = %s), points_target = %s"
                      " where id = %s", (pid if n else None, n if pid else None, i))
        elif (thing, verb) == ("bill", "remove"):
            c.execute("delete from family.bills where id = %s", (i,))
        elif (thing, verb) == ("goal", "add"):
            if not form.get("title", "").strip() or not amount(form.get("target")):
                return "/money?problem=goal#pots"
            did = int(form["dream"]) if form.get("dream", "").isdecimal() else None
            c.execute("insert into family.goals (title, target, by_date, dream_id, place) values (%s, %s, %s,"
                      " (select id from family.dreams where id = %s), %s)",
                      (form["title"].strip()[:60], amount(form["target"]), day_of(form.get("by")), did,
                       form.get("place", "").strip()[:60] or None))
        elif (thing, verb) in (("goal", "save"), ("goal", "out")):
            if not amount(form.get("amount")):
                return "/money?problem=save#pots"
            money.pot_move(c, i, amount(form["amount"]) * (1 if verb == "save" else -1))
        elif (thing, verb) == ("goal", "place"):
            c.execute("update family.goals set place = %s where id = %s", (form.get("place", "").strip()[:60] or None, i))
        elif (thing, verb) == ("goal", "remove"):
            c.execute("delete from family.goals where id = %s", (i,))
        # dreams
        elif thing == "dream" and verb in ("add", "edit"):
            ids = {p["id"] for p in c.execute("select id from family.people").fetchall()}
            got = save_dream(c, form, forms, ids, i if verb == "edit" else None)
            if isinstance(got, str):
                return ("/dreams/new?" if verb == "add" else f"/dreams/{i}/edit?") + urllib.parse.urlencode(
                    {k: v for k, v in form.items() if k in ("title", "cover", "when", "on_date", "note", "saving", "target")}
                    | {"sharers": ",".join(forms.get("sharer", [])), "problem": got})
            return f"/dreams/{got}?added=1" if verb == "add" else f"/dreams/{got}"
        elif (thing, verb) == ("dream", "fill"):  # one sentence fills the sheet
            return "/dreams/new?" + urllib.parse.urlencode(pip.dream_from(form.get("text", ""), today, c))
        elif (thing, verb) == ("dream", "lived"):
            c.execute("update family.dreams set lived_on = %s where id = %s and lived_on is null", (today, i))
            return f"/dreams/{i}?lived=1"
        elif (thing, verb) == ("dream", "drawn"):  # back to the drawn cover; the photo stays in Photos
            c.execute("update family.dreams set photo_id = null where id = %s", (i,))
        elif (thing, verb) == ("dream", "again"):
            c.execute("update family.dreams set lived_on = null where id = %s", (i,))
        elif (thing, verb) == ("dream", "remove"):
            c.execute("delete from family.dreams where id = %s", (i,))
            return "/dreams"
        elif (thing, verb) == ("dream", "step-add") and form.get("t", "").strip():
            pid = int(form["person"]) if form.get("person", "").isdecimal() else None
            c.execute("update family.dreams set steps = steps || %s::jsonb where id = %s and jsonb_array_length(steps) < 20",
                      (json.dumps([{"t": form["t"].strip()[:80], "person": pid, "done": False}]), i))
        elif thing == "dream" and verb.startswith("step-") and verb[5:].isdecimal():
            n = int(verb[5:])
            c.execute("update family.dreams set steps = jsonb_set(steps, %s, to_jsonb(not coalesce((steps->%s->>'done')::boolean,"
                      " false))) where id = %s and jsonb_array_length(steps) > %s", ([str(n), "done"], n, i, n))
        elif (thing, verb) == ("photo", "remove"):
            c.execute("update family.dreams set photo_id = null where photo_id = %s", (i,))
            c.execute("delete from family.photos where id = %s", (i,))
        elif (thing, verb) == ("support", "add"):
            if form.get("text", "").strip():
                c.execute("insert into family.support (text, page) values (%s, %s)",
                          (form["text"].strip()[:2000], form.get("page", "")[:200] or None))
            return "/support?thanks=1"
        else:
            return False
    return True


def task_points(c, tid):
    """A chore with points: ticked, its person earns them; unticked, they go again."""
    c.execute("delete from family.points where task_id = %s", (tid,))
    c.execute("insert into family.points (person_id, amount, why, task_id) select person_id, points, title, id from family.tasks"
              " where id = %s and done_at is not null and points is not null and person_id is not null", (tid,))


def step_points(c, rid, step, on_day):
    c.execute("delete from family.points where routine_id = %s and step = %s and on_day = %s", (rid, step, on_day))
    c.execute("insert into family.points (person_id, amount, why, routine_id, step, on_day) select r.person_id, r.points,"
              " r.steps->%s->>'name', r.id, %s, %s from family.routines r join family.routine_done x on x.routine_id = r.id"
              " and x.step = %s and x.on_day = %s where r.id = %s and r.points is not null and r.person_id is not null",
              (step, step, on_day, step, on_day, rid))


def kakeibo_facts(c, m0):
    m = money.month(c, m0.replace(day=1) + timedelta(days=27))  # any day of that month counts the same
    return {k: round(m[k], 2) for k in ("came", "bills", "living", "dreams")}


def icon_for(step):
    """A routine step's picture, from its words."""
    for pat, icon in ((r"wake|get up", "wake"), (r"dress|cloth|pyjama|shoes|coat", "dress"),
                      (r"breakfast|eat|dinner|lunch|snack", "eat"), (r"teeth|brush", "teeth"), (r"bag|school|pack", "bag"),
                      (r"bath|shower|wash", "bath"), (r"read|book|story", "book"), (r"light|sleep|bed", "moon")):
        if re.search(rf"\b({pat})", step, re.I):
            return icon
    return "dot"


def multipart(body, ctype):
    """A form with a file in it: (fields, the file's bytes or None, its file name)."""
    # split on the boundary by hand: the email parser needs about ten times the file's size in memory
    m = re.search(r'boundary="?([^";]+)"?', ctype)
    if not ctype.startswith("multipart/form-data") or not m:
        return {}, None, ""
    data, filename, fields = None, "", {}
    for part in body.split(b"--" + m[1].encode())[1:-1]:
        head, _, value = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]*)"', head)
        value = value[:-2] if value.endswith(b"\r\n") else value
        if not name:
            continue
        fn = re.search(rb'filename="([^"]*)"', head)
        if fn:
            data, filename = value or None, fn[1].decode(errors="replace")
        else:
            fields[name[1].decode(errors="replace")] = value.decode(errors="replace")
    return fields, data, filename


def image_mime(data):
    return next((m for sig, m in ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG", "image/png"), (b"GIF8", "image/gif"))
                 if data and data.startswith(sig)), None) or ("image/webp" if data and data[8:12] == b"WEBP" else None)


def keep_photo(c, data, caption):
    """A picture we can show, kept in Photos. Its id, or None when it is not one."""
    mime = image_mime(data)
    if not mime:
        return None
    return c.execute("insert into family.photos (data, mime, caption) values (%s, %s, %s) returning id",
                     (data, mime, caption.strip()[:80])).fetchone()["id"]


def save_photo(c, body, ctype):
    """A photo from the family (multipart form). Returns its id, or None when it is not a picture we can show."""
    fields, data, _ = multipart(body, ctype)
    pid = keep_photo(c, data, fields.get("caption", ""))
    if pid and fields.get("dream", "").isdecimal():
        c.execute("update family.dreams set photo_id = %s where id = %s", (pid, int(fields["dream"])))
    return pid, fields


def add_journal(c, did, text, photo=None):
    """A line in a dream's journal, maybe with a photo. False when the dream or the words are missing."""
    dr = c.execute("select title from family.dreams where id = %s", (did,)).fetchone()
    text = (text or "").strip()[:280]
    if not dr or not text:
        return False
    pid = keep_photo(c, photo, f'{dr["title"]}: {text}') if photo else None
    c.execute("insert into family.journal (dream_id, text, photo_id) values (%s, %s, %s)", (did, text, pid))
    return True


def ics(c, fam, person_id=None):
    """The family's calendar (or one person's) as an .ics feed for phone calendars: 30 days back, a year ahead."""
    esc = lambda t: re.sub(r"([,;\\])", r"\\\1", t).replace("\r", "").replace("\n", "\\n")
    rows = c.execute("select e.*, p.name from family.events e left join family.people p on p.id = e.person_id"
                     " where e.starts_at > now() - interval '30 days' and e.starts_at < now() + interval '1 year'"
                     + (" and (e.person_id = %s or e.person_id is null)" if person_id else "") + " order by e.starts_at",
                     (person_id,) if person_id else ()).fetchall()
    tz = ZoneInfo(fam["tz"])
    stamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//FamBox//Family table//EN", "CALSCALE:GREGORIAN",
           f"X-WR-CALNAME:{esc('The ' + fam['name'] + ' Family')}", "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]
    for ev in rows:
        st = ev["starts_at"].astimezone(tz)
        out += ["BEGIN:VEVENT", f"UID:e{ev['id']}@{fam['slug']}.fam.box", f"DTSTAMP:{stamp}",
                f"SUMMARY:{esc(ev['title'] + (' (' + ev['name'] + ')' if ev['name'] and not person_id else ''))}"]
        if ev["all_day"]:
            last = (ev["ends_at"] or ev["starts_at"]).astimezone(tz).date()
            out += [f"DTSTART;VALUE=DATE:{st:%Y%m%d}", f"DTEND;VALUE=DATE:{last + timedelta(days=1):%Y%m%d}"]
        else:
            end = (ev["ends_at"] or ev["starts_at"] + timedelta(hours=1)).astimezone(ZoneInfo("UTC"))
            out += [f"DTSTART:{ev['starts_at'].astimezone(ZoneInfo('UTC')):%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}"]
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return ("\r\n".join(out) + "\r\n").encode()


# ---------- web server ----------

LINE_HINT = 'Try "40 groceries", "paid electricity", "put 20 in Lapland" or "salary 3200".'
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
       "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
errors, error_alert, error_lock = [], [0.0], threading.Lock()


def server_error(path):
    """Five server errors within 10 minutes tell the box's keeper once an hour, through SAIA Notify when the box has
    a token for it (the hosted boxes on app-box); elsewhere the error only goes to the log."""
    token = os.environ.get("SAIA_NOTIFY_TOKEN", "").strip()
    now = time.time()
    with error_lock:
        errors[:] = [t for t in errors if now - t < 600] + [now]
        if len(errors) < 5 or now - error_alert[0] < 3600 or not token:
            return
        error_alert[0], count = now, len(errors)
    box = os.environ.get("FAMBOX_ADDRESS", "a family box")
    body = json.dumps({"type": "support_escalation", "id": f"fambox-layer-5xx-{box}-{time.strftime('%Y%m%d%H', time.gmtime(now))}",
                       "data": {"product": f"{box} server errors", "reason": f"{count} server errors in 10 minutes (last: {path.split('?')[0][:80]})",
                                "message": f"Check: ssh app-box docker logs --since 15m (the {box} container)", "link": f"https://{box}/healthz"}}).encode()
    req = urllib.request.Request(os.environ.get("SAIA_NOTIFY_URL", "https://status.dsarle.com/saia-notify/v1/event"), body,
                                 {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    threading.Thread(target=lambda: _quiet(lambda: urllib.request.urlopen(req, timeout=10).read()), daemon=True).start()


def _quiet(fn):
    try:
        fn()
    except Exception as ex:
        print(f"saia-notify failed: {type(ex).__name__}", file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    timeout = 60  # a sender that stalls is let go after a minute

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        if self.headers.get("X-Forwarded-Proto") == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def send_response(self, code, message=None):
        if code >= 500 and code not in (501, 503):  # 501: a method we do not offer; 503: the family is not set up yet
            server_error(self.path)
        super().send_response(code, message)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            raise
        except Exception as ex:  # a bug answers a calm 500 page (and counts for the alert) instead of a dropped connection
            tb = ex.__traceback__
            while tb.tb_next:
                tb = tb.tb_next
            print(f"error on {self.path.split('?')[0][:80]}: {type(ex).__name__} at {Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}",
                  file=sys.stderr, flush=True)  # never the message: it can hold the family's data
            try:
                self.send(500, b"Something went wrong on the box. Please try again in a minute.\n", "text/plain; charset=utf-8")
            except OSError:
                pass

    def send(self, code, body, ctype="text/html; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not any(k == "Cache-Control" for k, _ in headers):
            self.send_header("Cache-Control", "no-store" if ctype.startswith("text/html") else "max-age=300")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def family_now(self, c):
        tz = ZoneInfo(c.execute("select tz from family.household").fetchone()["tz"])
        return datetime.now(tz)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/healthz":
            with db() as c:
                c.execute("select 1 from family.household")
            ver = HERE / "VERSION"  # written by release.sh; absent in development
            return self.send(200, ("ok " + ver.read_text().strip() if ver.exists() else "ok").encode() + b"\n", "text/plain")
        if url.path.startswith("/static/"):
            f = (STATIC / url.path[len("/static/"):]).resolve()
            if STATIC not in f.parents or not f.is_file():
                return self.send(404, b"not found\n", "text/plain")
            ctype = {".woff2": "font/woff2", ".svg": "image/svg+xml"}.get(f.suffix) or mimetypes.guess_type(f.name)[0]
            return self.send(200, f.read_bytes(), ctype or "application/octet-stream")
        with db() as c:
            fam = c.execute("select * from family.household").fetchone()
            if url.path == "/signin":
                k = (q.get("k") or [""])[0]
                return self.send(200, welcome.signed_out(c, k=k[:100] or None).encode())
            if not fam:
                return self.send(503, b"The family is not set up yet.\n", "text/plain")
            feed = re.fullmatch(r"/feed/([A-Za-z0-9_-]{20,64})(?:/(\d+))?\.ics", url.path, re.ASCII)
            if feed:  # phone calendars fetch this without signing in: the long private link is the key
                if not fam["feed_key"] or not secrets.compare_digest(feed[1], fam["feed_key"]):
                    return self.send(404, b"not found\n", "text/plain")
                return self.send(200, ics(c, fam, int(feed[2]) if feed[2] else None), "text/calendar; charset=utf-8")
            if not welcome.signed_in(c, welcome.cookie(self.headers, "fb_session")):
                if url.path == "/changes":
                    return self.send(401, b"sign in first\n", "text/plain")
                return self.send(401, welcome.signed_out(c).encode())
            if url.path == "/changes":
                c.close()
                return self.changes()
            if url.path.startswith("/welcome/") and url.path[9:].isdecimal():
                n = int(url.path[9:])
                if n in welcome.PAGES:
                    return self.send(200, welcome.get(c, n, q).encode())
                if n == len(welcome.STEPS):
                    return self.send(303, b"", "text/plain", [("Location", "/?tour=1")])
            if url.path == "/telegram/group":  # "Add Pip to your family group": a one-day join code for Telegram's group picker
                if pip.DRY or not fam["bot_username"]:
                    return self.send(303, b"", "text/plain", [("Location", "/")])
                return self.send(303, b"", "text/plain", [("Location", f'https://t.me/{fam["bot_username"]}?startgroup={welcome.mint(c, "join", 24)}')])
            if url.path in ("/cal/google/start", "/cal/google/back"):
                return self.google(c, fam, url.path, q)
            if url.path == "/money/line":  # what a typed money line will do, while it is typed
                it = money.line(c, (q.get("text") or [""])[0][:200], self.family_now(c).date())
                return self.send(200, json.dumps({"ok": bool(it), "said": money.say(it)}).encode(), "application/json")
            photo = re.fullmatch(r"/photo/(\d+)", url.path)
            if photo:
                row = c.execute("select data, mime from family.photos where id = %s", (int(photo[1]),)).fetchone()
                if not row:
                    return self.send(404, b"not found\n", "text/plain")
                return self.send(200, bytes(row["data"]), row["mime"], [("Cache-Control", "private, max-age=86400")])
            if not fam["moved_in_at"] and "tour" not in q:
                return self.send(303, b"", "text/plain", [("Location", f'/welcome/{min(fam["step"], len(welcome.PAGES))}')])
            s = load(c, self.family_now(c))
            s["theme"] = chosen_theme(welcome.cookie(self.headers, "theme"))
            partial = "partial" in q
            if url.path == "/wall":
                return self.send(200, wall_page(s, partial).encode())
            if url.path != "/":
                got = pages.render(c, s, url.path, q)
                if got is None:
                    return self.send(404, b"not found\n", "text/plain")
                main, here, title = got
                if partial:
                    return self.send(200, main.encode())
                return self.send(200, document(title, main, "welcome", shown_theme(s, s["theme"]), bottom_bar(fam, here)).encode())
        s["tour"] = int((q.get("tour") or ["0"])[0]) if (q.get("tour") or ["0"])[0] in ("1", "2") else 0
        who = (q.get("who") or [""])[0].lower()
        look = (q.get("look") or [""])[0]
        return self.send(200, page(s, who, s["theme"], partial, int(look) if look.isdecimal() else None, "edit" in q).encode())

    def google(self, c, fam, path, q):
        """Connect Google: off to Google's consent page and back with a code (a cookie holds the state in between)."""
        go = lambda where, *h: self.send(303, b"", "text/plain", [("Location", where), *h])
        if os.environ.get("FAMBOX_DEMO") or not calsync.google_ready():
            return go("/calendar?problem=google-off#both")
        back = f"https://{welcome.address(fam)}/cal/google/back"
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        if path == "/cal/google/start":
            state = secrets.token_urlsafe(24)
            return go(calsync.google_auth_url(back, state),
                      ("Set-Cookie", f"fb_gstate={state}; Path=/cal/google; HttpOnly; SameSite=Lax; Max-Age=900{secure}"))
        state, code = welcome.cookie(self.headers, "fb_gstate"), (q.get("code") or [""])[0]
        if not state or not secrets.compare_digest(state, (q.get("state") or [""])[0]) or not code:
            return go("/calendar?problem=google#both")
        got = calsync.google_code(code[:2000], back)
        old = c.execute("select secret from family.cal_accounts where kind = 'google'").fetchone()
        if not got or not (got[0] or old):
            return go("/calendar?problem=google#both")
        refresh, access, secs = got
        acct = {"id": None, "secret": refresh or old["secret"], "access_token": access,
                "access_until": datetime.now(ZoneInfo("UTC")) + timedelta(seconds=secs)}
        tz = self.family_now(c).tzinfo
        try:
            found = calsync.Google(c, acct, tz).calendars()
        except Exception:
            found = [{"url": "primary", "name": "Main calendar"}]
        login = next((f["url"] for f in found if f.get("primary")), "")
        with c.transaction():
            calsync.connect(c, "google", login, acct["secret"], found, (access, acct["access_until"]))
        calsync.soon()
        return go("/calendar#both", ("Set-Cookie", f"fb_gstate=; Path=/cal/google; HttpOnly; SameSite=Lax; Max-Age=0{secure}"))

    def do_POST(self):
        size = self.headers.get("Content-Length") or "0"
        path = urllib.parse.urlsplit(self.path).path
        photo = path == "/do/photo/0/add"
        multi = photo or path in ("/do/statement/0/add", "/do/letter/0/add") or re.fullmatch(r"/do/journal/\d+/add", path)
        if size.isdecimal() and int(size) > (8_000_000 if multi else 20000):  # a photo or a statement file, or a small form
            return self.send(413, b"That is too big to send.\n", "text/plain")
        origin = self.headers.get("Origin")  # every browser sends it with a post; a missing or "null" one is refused
        if not origin or urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            return self.send(403, b"posts only come from this box's own pages\n", "text/plain")
        if multi:  # a big body is read only from a signed-in device
            with db() as c:
                if not welcome.signed_in(c, welcome.cookie(self.headers, "fb_session")):
                    return self.send(403, b"sign in first\n", "text/plain")
        body = self.rfile.read(int(size) if size.isdecimal() else 0)
        forms = urllib.parse.parse_qs(body.decode(errors="replace") if not multi else "")
        form = {k: v[0] for k, v in forms.items()}
        go = lambda where: self.send(303, b"", "text/plain", [("Location", where)])
        with db() as c:
            if not c.execute("select 1 from family.household").fetchone():
                return self.send(503, b"The family is not set up yet.\n", "text/plain")
            if path == "/signin":
                if not welcome.redeem(c, form.get("k", ""), "link"):
                    return self.send(400, welcome.signed_out(c, problem=True).encode())
                secure = self.headers.get("X-Forwarded-Proto") == "https"
                return self.send(303, b"", "text/plain", [("Location", "/"), ("Set-Cookie", welcome.session_cookie(c, secure))])
            if not welcome.signed_in(c, welcome.cookie(self.headers, "fb_session")):
                return self.send(403, b"sign in first\n", "text/plain")
            if path.startswith("/welcome/") and path[9:].isdecimal() and 1 <= int(path[9:]) <= len(welcome.STEPS):
                with c.transaction():
                    what, x = welcome.post(c, int(path[9:]), form, forms)
                return go(x) if what == "go" else self.send(200, x.encode())
            if path == "/ask":
                pip.reply(c, form.get("text", ""), via="web")
                return go("/#pip")
            if photo:
                with c.transaction():
                    pid, fields = save_photo(c, body, self.headers.get("Content-Type") or "")
                back = "/dreams/" + fields["dream"] if fields.get("dream", "").isdecimal() else "/photos"
                return go(back if pid else back + "?problem=photo")
            if path == "/do/money/0/line":  # one typed line: "40 groceries", "paid electricity", "salary 3200"
                today = self.family_now(c).date()
                with c.transaction():
                    it = money.line(c, form.get("text", ""), today)
                    said = money.apply(c, it, today) if it else ""
                if "application/json" in (self.headers.get("Accept") or ""):
                    return self.send(200, json.dumps({"ok": bool(it), "said": said or LINE_HINT}).encode(), "application/json")
                return go("/money?" + urllib.parse.urlencode({"said": said} if it else {"problem": "line", "text": form.get("text", "")[:200]}))
            if multi:
                fields, data, filename = multipart(body, self.headers.get("Content-Type") or "")
                if path == "/do/letter/0/add":  # a photo of a letter: the dates in it wait on the home screen; the photo is not kept
                    said, _ = pip.letter(c, data) if data and image_mime(data) else ("That isn't a photo I can read. Try a JPEG or PNG.", None)
                    pip.save_reply(c, "web", None, "(a photo of a letter)", said)
                    return go("/#pip")
                if path == "/do/statement/0/add":
                    try:
                        bank, lines = money.read_statement(data or b"", filename)
                    except ValueError:
                        return go("/money?problem=file#statement")
                    with c.transaction():
                        n_new, n_old = money.store(c, bank, lines)
                    return go(f"/money?read={n_new}&seen={n_old}#review")
                did = int(path.split("/")[3])
                with c.transaction():
                    ok = add_journal(c, did, fields.get("text"), data)
                return go(f"/dreams/{did}" + ("" if ok else "?problem=journal") + "#journal")
            tz = self.family_now(c).tzinfo
        done = act(path, tz, form, forms)
        if not done:
            return self.send(404, b"not found\n", "text/plain")
        if isinstance(done, str):
            return go(done)
        back = urllib.parse.urlsplit(self.headers.get("Referer") or "/")
        where = "/" + back.path.lstrip("/") + (f"?{back.query}" if back.query else "")  # never another site
        return go(where)

    def changes(self):
        """Server-sent events: one line per change in the family's data, a ping every 25 s."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # ponytail: one database connection per open screen; a shared listener if a family ever has dozens
        try:
            with psycopg.connect(DSN, autocommit=True) as c:
                c.execute("listen family_changed")
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.flush()
                while True:
                    got = any(True for _ in c.notifies(timeout=25, stop_after=1))
                    self.wfile.write(b"data: changed\n\n" if got else b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def log_message(self, fmt, *args):
        if os.environ.get("FAMBOX_QUIET") is None:  # sign-in keys and Google's one-time codes never reach the log
            super().log_message(fmt, *(re.sub(r"([?&](?:k|code|state)=)[^&\s]+", r"\1…", a) if isinstance(a, str) else a for a in args))


# ---------- set-up ----------

def init():
    with db() as c:
        c.execute((HERE / "schema.sql").read_text())


def wipe(c):
    """Removes the family's data. Sign-in sessions stay, so whoever is signed in stays signed in."""
    c.execute("truncate family.events, family.tasks, family.status, family.suggestions, family.people,"
              " family.household, family.dreams, family.messages, family.chats, family.spending, family.bills,"
              " family.goals, family.routines, family.routine_done, family.meals, family.shopping, family.notes,"
              " family.photos, family.blocks, family.support, family.pot_moves, family.income, family.bank_lines, family.kakeibo,"
              " family.journal, family.points, family.members, family.pending, family.cal_accounts restart identity cascade")


def new_family(c, name, slug):
    """A box as the engine hands it over: the family's name and address, nothing else. Moving in starts."""
    wipe(c)
    c.execute("insert into family.household (name, slug) values (%s, %s)", (name, slug))


def sample():
    """The Kask family from the design, on today's date. Replaces whatever data is there."""
    with db() as c, c.transaction():
        wipe(c)
        c.execute("insert into family.household (name, slug, agent_line, lat, lon, moved_in_at, step) values"
                  " ('Kask', 'kask', 'Busy day: football at 5, and it might rain.', 59.437, 24.754, now(), 8)")
        ids = {}
        for i, (name, role, col) in enumerate([("Mari", "adult", "teal"), ("Andres", "adult", "sky"),
                                               ("Lena", "child", "plum"), ("Oskar", "child", "sage"),
                                               ("Helju", "adult", "rose"), ("Muki", "pet", "clay")]):
            ids[name] = c.execute("insert into family.people (name, role, colour, sort) values (%s,%s,%s,%s)"
                                  " returning id", (name, role, col, i)).fetchone()["id"]
        tz = ZoneInfo("Europe/Tallinn")
        today = datetime.now(tz).date()
        week0 = today - timedelta(days=today.weekday())
        at = lambda d, h, m=0: datetime.combine(d, datetime.min.time(), tz).replace(hour=h, minute=m)
        evs = [(today, 7, 30, "Muki's walk", "Andres"), (today, 8, 10, "School run", "Andres"),
               (today, 10, 0, "Grandma's physio", "Helju"), (today, 16, 0, "Piano", "Lena"),
               (today, 17, 0, "Football", "Oskar"), (today, 19, 30, "Everyone home", None)]
        for off, h, m, title, who in [(0, 16, 0, "Piano", "Lena"), (2, 15, 0, "Swimming", "Lena"),
                                      (3, 17, 0, "Football", "Oskar"), (3, 18, 0, "Choir", "Lena"),
                                      (4, 19, 0, "Book club", "Mari"), (5, 13, 0, "Lunch at Grandma's", None),
                                      (6, 11, 0, "Garden club", "Helju")]:
            d = week0 + timedelta(days=off)
            if d != today:
                evs.append((d, h, m, title, who))
        for d, h, m, title, who in evs:
            c.execute("insert into family.events (title, starts_at, person_id) values (%s, %s, %s)",
                      (title, at(d, h, m), ids.get(who)))
        for title, who, done in [("Book Muki at the vet", "Mari", False), ("Fix Lena's bike light", "Andres", False),
                                 ("Feed Muki", "Lena", True), ("Tidy the shoes", "Oskar", False),
                                 ("Pack swim bag for Wednesday", "Lena", False)]:
            c.execute("insert into family.tasks (title, person_id, done_at) values (%s, %s, %s)",
                      (title, ids[who], datetime.now(tz) if done else None))
        c.execute("insert into family.status (sentence, detail, action_label, person_id) values"
                  " ('Lena''s permission slip is due tomorrow', 'the school trip on Thursday', 'Sign it', %s)",
                  (ids["Lena"],))
        fmt = lambda d: f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"
        d1, d2, d3 = today + timedelta(8), today + timedelta(13), today + timedelta(15)
        letter = [{"label": f"Photo day · {fmt(d1)}", "title": "Photo day", "date": d1.isoformat(), "person": "Lena"},
                  {"label": f"Autumn holiday · {d2.day} to {(d2 + timedelta(4)).day} {d2.strftime('%b')}",
                   "title": "Autumn holiday", "date": d2.isoformat(), "end_date": (d2 + timedelta(4)).isoformat()},
                  {"label": f"Parents' evening · {fmt(d3)}", "title": "Parents' evening", "date": d3.isoformat(),
                   "time": "18:00"}]
        c.execute("insert into family.suggestions (text, items, accept_label) values (%s, %s, 'Add all {n}')",
                  ("I found 3 dates in the school letter. Add them?", json.dumps(letter)))
        c.execute("insert into family.suggestions (text, items, accept_label, quiet) values (%s, %s, 'Remind', true)",
                  ("Rain at football. Remind Oskar to pack a jacket?",
                   json.dumps([{"label": "Pack a jacket", "task": "Pack a jacket for football", "person": "Oskar"}])))
        extras(c, ids, today, tz)


def extras(c, ids, today, tz):
    """Money, dreams, countdowns and routines for a sample family (the design's Kask family, or hosted/sarle-seed.py)."""
    names = list(ids)
    c.execute("update family.household set money_plan = 2600")
    c.execute("insert into family.spending (amount, note, spent_on) values (%s, 'Groceries and the rest so far', %s)",
              (round(2600 * min(0.95, (today.day + 12) / 45)), today.replace(day=1)))
    for title, amt, day in [("Electricity", 86, (today + timedelta(3)).day), ("Internet", 29, (today + timedelta(6)).day),
                            ("Swimming club", 45, 1)]:
        c.execute("insert into family.bills (title, amount, due_day) values (%s, %s, %s)", (title, amt, day))
    far = date(today.year + (today.month >= 2), 2, 14)
    big = names[:-1] if len(names) > 2 else names
    d1 = c.execute("insert into family.dreams (title, cover, sharers, on_date, note, saving, steps) values (%s, 0, %s, %s,"
                   " '5 nights', true, %s) returning id",
                   ("Lapland in winter", [ids[n] for n in big], far, json.dumps(
                       [{"t": "Start the Lapland fund", "person": None, "done": True},
                        {"t": "Choose the dates together", "person": ids[names[0]], "done": True},
                        {"t": "Book the cabin", "person": ids[names[0]], "done": False},
                        {"t": f"Warm boots for {names[3] if len(names) > 3 else names[-1]}", "person": ids[names[1]], "done": False}]))
                   ).fetchone()["id"]
    c.execute("insert into family.goals (title, target, saved, by_date, dream_id) values ('Lapland fund', 2400, 960, %s, %s)", (far, d1))
    for title, cover, who, note in [("Learn to sail together", 2, names[:2] + names[3:4], "Someone is keen"),
                                    ("Build a treehouse", 4, names[1:2] + names[3:4], "Next summer?"),
                                    ("Cycle around the island", 5, names[:3], "3 days")]:
        c.execute("insert into family.dreams (title, cover, sharers, note) values (%s, %s, %s, %s)",
                  (title, cover, [ids[n] for n in who if n in ids], note))
    c.execute("insert into family.dreams (title, cover, sharers, lived_on) values ('Camping in the forest', 6, %s, %s)",
              ([ids[n] for n in names[:4]], today - timedelta(days=60)))
    child = [n for n in names][3] if len(names) > 3 else names[-1]
    c.execute("insert into family.events (title, starts_at, all_day, person_id, countdown) values (%s, %s, true, %s, true)",
              (f"{child}'s birthday", datetime.combine(today + timedelta(9), datetime.min.time(), tz), ids[child]))
    c.execute("insert into family.events (title, starts_at, ends_at, all_day, countdown) values ('Autumn holiday', %s, %s, true, true)",
              (datetime.combine(today + timedelta(13), datetime.min.time(), tz),
               datetime.combine(today + timedelta(17), datetime.min.time(), tz)))
    morning = [("Wake up", "wake"), ("Get dressed", "dress"), ("Breakfast", "eat"), ("Teeth", "teeth"), ("School bag", "bag")]
    evening = [("Bath", "bath"), ("Pyjamas", "dress"), ("Read", "book"), ("Lights out", "moon")]
    r1 = c.execute("insert into family.routines (person_id, part, steps, sort) values (%s, 'morning', %s, 0) returning id",
                   (ids[child], json.dumps([{"name": n, "icon": i} for n, i in morning]))).fetchone()["id"]
    c.execute("insert into family.routines (person_id, part, steps, sort) values (%s, 'evening', %s, 1)",
              (ids[names[2]], json.dumps([{"name": n, "icon": i} for n, i in evening])))
    for step in range(3):
        c.execute("insert into family.routine_done (routine_id, step, on_day, at) values (%s, %s, %s, now() - interval '1 hour')",
                  (r1, step, today))
    money_v2(c, ids, today, d1, r1)


def money_v2(c, ids, today, lapland, routine):
    """Phases B and C for the sample family: what came in, bills in every state, pots with places, a bank statement
    waiting, last month's four answers, a journal, and chores that earn points toward a child's own dream."""
    names = list(ids)
    m0 = today.replace(day=1)
    c.execute("update family.household set payday = 1")
    for amt, note in [(2450, "Salary, " + names[0]), (1980, "Salary, " + names[1]), (200, "Child benefit")]:
        c.execute("insert into family.income (amount, note, on_date) values (%s, %s, %s)", (amt, note, m0))
    for title, amt, due, paid in [("Home loan", 720, 1, True), ("Car insurance", 38, max(1, today.day - 2), False)]:
        bid = c.execute("insert into family.bills (title, amount, due_day) values (%s, %s, %s) returning id", (title, amt, due)).fetchone()["id"]
        if paid or due == today.day:
            money.mark_paid(c, bid, m0)
    money.mark_paid(c, c.execute("select id from family.bills where title = 'Swimming club'").fetchone()["id"], m0)
    c.execute("delete from family.spending where note = 'Groceries and the rest so far'")
    shops = [("Rimi", 64.30), ("Fuel", 58.00), ("Prisma", 41.75), ("School lunch money", 25.00), ("Apotheka", 12.90),
             ("Rimi", 87.15), ("Birthday present for Grandma", 35.00), ("Prisma", 52.40), ("Fuel", 61.20), ("Cinema", 34.00)]
    for i, d_ in enumerate(range(1, today.day + 1, 2)):
        note, amt = shops[i % len(shops)]
        c.execute("insert into family.spending (amount, note, spent_on) values (%s, %s, %s)", (amt + i % 3, note, m0.replace(day=d_)))
    c.execute("update family.goals set place = 'Cash box at home' where dream_id = %s", (lapland,))
    spring = date(today.year + (today.month >= 5), 5, 1)
    for title, target, saved, by, place in [("Car service", 400, 140, spring, "Revolut Pocket"), ("Rainy day", 3000, 1200, None, "Bank")]:
        c.execute("insert into family.goals (title, target, saved, by_date, place) values (%s, %s, %s, %s, %s)", (title, target, saved, by, place))
    for title, amt in [("Lapland fund", 150), ("Car service", 40)]:
        money.pot_move(c, c.execute("select id from family.goals where title = %s", (title,)).fetchone()["id"], amt, "Saved first", m0)
    last = (m0 - timedelta(days=1)).replace(day=1)
    c.execute("insert into family.kakeibo (month, facts, answer, asked_at, answered_at) values (%s, %s, %s, now(), now())",
              (last, json.dumps({"came": 4630, "bills": 900, "living": 2240, "dreams": 190}),
               "More came in than we spent, but eating out added up. In December: the Lapland box first on payday, and one pizza night a week."))
    money.store(c, "Revolut", [{"on": today - timedelta(days=1), "amount": -23.40, "text": "Prisma", "hint": None},
                               {"on": today - timedelta(days=1), "amount": -7.10, "text": "Bolt", "hint": None},
                               {"on": today, "amount": -29.00, "text": "Telia Eesti · Internet", "hint": None}])
    for ago, text in [(20, "Found flights for February: €189 each if we book before December."),
                      (6, f"{names[3] if len(names) > 3 else names[-1]} drew the cabin: a sauna, a reindeer and a very big fire.")]:
        c.execute("insert into family.journal (dream_id, text, on_date) values (%s, %s, %s)", (lapland, text, today - timedelta(days=ago)))
    if len(names) > 3:  # the two children each earn their own dream with chores
        for kid, title, cover, want, earned in [(names[3], "A Lego castle", 4, 300, 120), (names[2], "Riding lessons", 5, 400, 85)]:
            c.execute("insert into family.dreams (title, cover, sharers, note, points_for, points_target) values (%s, %s, %s, %s, %s, %s)",
                      (title, cover, [ids[kid]], "With chores", ids[kid], want))
            for k in range(earned // 5):
                c.execute("insert into family.points (person_id, amount, why, at) values (%s, 5, 'Chores', now() - %s * interval '1 day')",
                          (ids[kid], k + 1))
        c.execute("update family.tasks set points = 10 where title = 'Tidy the shoes'")
        for t in c.execute("update family.tasks set points = 5 where title = 'Feed Muki' returning id").fetchall():
            task_points(c, t["id"])
        c.execute("update family.routines set points = 2 where id = %s", (routine,))
        for step in range(3):
            step_points(c, routine, step, today)


def cli():
    if "--init" in sys.argv:
        init()
        print("family schema ready")
    elif "--sample" in sys.argv:
        init()
        sample()
        print("sample family loaded: the Kask family")
    elif "--new-family" in sys.argv:
        i = sys.argv.index("--new-family")
        name, slug = sys.argv[i + 1], sys.argv[i + 2]
        init()
        with db() as c, c.transaction():
            new_family(c, name, slug)
        print(f"fresh box for the {name} family: moving in starts at the next sign-in")
    elif "--support" in sys.argv:  # what the family wrote in "Something not right?"
        with db() as c:
            rows = c.execute("select at, page, text from family.support order by id").fetchall()
        for r in rows:
            print(f'{r["at"]:%Y-%m-%d %H:%M}  {r["page"] or ""}\n  {r["text"]}\n')
        print(f"{len(rows)} report{'s' if len(rows) != 1 else ''}")
    elif "--import-statement" in sys.argv:  # a statement file (or lhv.py's output on stdin: -) for the Money page
        src = sys.argv[sys.argv.index("--import-statement") + 1]
        raw = sys.stdin.buffer.read() if src == "-" else Path(src).read_bytes()
        try:
            bank, lines = money.read_statement(raw, src)
        except ValueError as ex:
            sys.exit(str(ex))
        with db() as c, c.transaction():
            n_new, n_old = money.store(c, bank, lines)
        print(f"{bank} statement: {n_new} new lines, {n_old} already seen. They wait on the Money page to be accepted.")
    elif "--sign-out-all" in sys.argv:  # someone else got in: every device signs in again with a new link
        with db() as c:
            n = c.execute("delete from family.keys where kind in ('session', 'link')").rowcount
        print(f"Signed out every device ({n} sign-ins and unused links removed). Send new links: --link")
    elif "--link" in sys.argv:
        with db() as c:
            link = welcome.link_for(c)
        if not link:
            sys.exit("Set FAMBOX_ADDRESS (the box's address) first.")
        print(link)
    else:
        if not pip.DRY:
            threading.Thread(target=pip.run, daemon=True, name="pip-telegram").start()
        threading.Thread(target=pip.mornings, daemon=True, name="pip-mornings").start()
        threading.Thread(target=calsync.run, daemon=True, name="calendars").start()
        host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8090"))
        srv = ThreadingHTTPServer((host, port), Handler)
        srv.daemon_threads = True
        print(f"FamBox dashboard on http://{host}:{port}", file=sys.stderr)
        srv.serve_forever()


if __name__ == "__main__":
    import dashboard  # the one copy pip.py and welcome.py also use

    dashboard.cli()
