"""Two-way calendars: the family's table kept in step with one iCloud calendar and/or one Google calendar.

Standard library only. iCloud speaks CalDAV with the family member's Apple ID and an app-specific password (Apple
requires one for this). Google speaks its Calendar API after a one-time "Connect Google" (OAuth with an offline
refresh token); the OAuth client comes from GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (docs/google-calendar-setup.md).

One round (sync_account), every few minutes and soon after the table's events change (run()):
1. Pull what changed over there since the last sync token (everything when there is none or it expired).
   - New over there: a new table event, linked in family.cal_links (came_from_there).
   - The same content as the table, usually our own write coming back: only its ETag is noted. This stops loops.
   - Changed on both sides since they were last the same: the later edit wins (events.updated_at against the
     calendar's own "last modified").
   - Removed over there: removed from the table.
2. Push what changed on the table: new events (today to a year ahead) are created there, edited ones written with
   If-Match (a change made there meanwhile waits for the next round), removed ones removed there.
A removal on either side wins over an edit on the other. Passwords and tokens never reach the log.

Repeating events: Google sends each occurrence (singleEvents). iCloud sends the rule, which expand() turns into
occurrences; removing one on the table adds an EXDATE there, other table edits to one occurrence stay on the table.
"""
import base64
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

import dashboard as d

ICLOUD = "https://caldav.icloud.com/"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_SCOPES = ("https://www.googleapis.com/auth/calendar.events "
                 "https://www.googleapis.com/auth/calendar.calendarlist.readonly")
EVERY = 180  # seconds between rounds; a change on the table starts one within seconds
BACK, AHEAD = timedelta(days=30), timedelta(days=365)  # the stretch kept in step, as the phone feed
NAMES = {"icloud": "iCloud", "google": "Google"}
UTC = timezone.utc
DAV, CAL = "{DAV:}", "{urn:ietf:params:xml:ns:caldav}"
WD = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
lock = threading.Lock()


class SignIn(Exception):
    """The calendar refused the family's sign-in (password removed, Google access taken back)."""


class Gone(Exception):
    """The chosen calendar is not there any more."""


class Later(Exception):
    """Changed over there while we wrote: the next round decides."""


def google_ready():
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def http(method, url, body=None, headers=None):
    """(status, headers, bytes). Nothing here is logged: addresses, headers and bodies carry keys and events."""
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as ex:
        return ex.code, ex.headers, ex.read()


def day(x, tz):
    return x.astimezone(tz).date() if isinstance(x, datetime) else x


# ---------- what both sides must agree on: (title, all day, start, end) ----------
# all day: dates, end exclusive; otherwise UTC datetimes. The person goes in the title, "Swimming (Lena)", as in the feed.

def shape(title, all_day, start, end):
    if all_day:
        return (title, True, start, max(end or start, start + timedelta(days=1)))
    return (title, False, start.astimezone(UTC), (end if end and end > start else start + timedelta(hours=1)).astimezone(UTC))


def table_shape(ev, names, tz):
    title = ev["title"] + (f' ({names[ev["person_id"]]})' if ev["person_id"] in names else "")
    if ev["all_day"]:
        return shape(title, True, day(ev["starts_at"], tz), day(ev["ends_at"] or ev["starts_at"], tz) + timedelta(days=1))
    return shape(title, False, ev["starts_at"], ev["ends_at"])


def to_table(c, sh, by_name, tz, event_id=None):
    """Writes a calendar's event onto the table: (event id, its updated_at)."""
    title, all_day, start, end = sh
    m = re.fullmatch(r"(.+?) \(([^()]+)\)", title)
    pid = by_name.get(m[2].lower()) if m else None
    title = (m[1] if pid else title).strip()[:120] or "Busy"
    if all_day:
        last = end - timedelta(days=1)
        start, end = (datetime.combine(x, datetime.min.time(), tz) for x in (start, last))
        end = end if last > sh[2] else None
    if event_id:
        row = c.execute("update family.events set title = %s, starts_at = %s, ends_at = %s, all_day = %s, person_id = %s"
                        " where id = %s returning id, updated_at", (title, start, end, all_day, pid, event_id)).fetchone()
    else:
        row = c.execute("insert into family.events (title, starts_at, ends_at, all_day, person_id) values (%s, %s, %s, %s, %s)"
                        " returning id, updated_at", (title, start, end, all_day, pid)).fetchone()
    return row and (row["id"], row["updated_at"])


# ---------- iCalendar text ----------

def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text).replace("\r", "").split("\n")


def split_prop(line):
    """'DTSTART;TZID=Europe/Tallinn:20261110T150000' -> ('DTSTART', {'TZID': 'Europe/Tallinn'}, '20261110T150000')"""
    quoted, i = False, len(line)
    for n, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            i = n
            break
    name, *ps = line[:i].split(";")
    return name.upper(), {k.upper(): v.strip('"') for k, _, v in (p.partition("=") for p in ps)}, line[i + 1:]


def ical_text(t, back=False):
    if back:
        return re.sub(r"\\(.)", lambda m: "\n" if m[1] in "nN" else m[1], t)
    return re.sub(r"([,;\\])", r"\\\1", t).replace("\r", "").replace("\n", "\\n")


def when(params, value, tz):
    v = value.strip()
    if params.get("VALUE") == "DATE" or len(v) == 8:
        return date(int(v[:4]), int(v[4:6]), int(v[6:8]))
    dt = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
    if v.endswith("Z"):
        return dt.replace(tzinfo=UTC)
    try:
        return dt.replace(tzinfo=ZoneInfo(params["TZID"]))
    except Exception:  # no zone, or one only Outlook knows: the family's own
        return dt.replace(tzinfo=tz)


def duration(v):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return timedelta(0)
    w, dd, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return timedelta(weeks=w, days=dd, hours=h, minutes=mi, seconds=s) * (-1 if m[1] == "-" else 1)


def vevents(text):
    """The VEVENTs of an iCalendar text: [{NAME: (params, value)}, with EXDATE as a list]. Alarms are skipped."""
    out, cur, depth = [], None, 0
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur, depth = {"EXDATE": []}, 0
        elif cur is not None and line.startswith("BEGIN:"):
            depth += 1
        elif cur is not None and line.startswith("END:") and depth:
            depth -= 1
        elif line == "END:VEVENT" and cur is not None:
            out.append(cur)
            cur = None
        elif cur is not None and not depth and line:
            name, params, value = split_prop(line)
            if name == "EXDATE":
                cur["EXDATE"] += [(params, v) for v in value.split(",")]
            else:
                cur[name] = (params, value)
    return out


def span(v, tz):
    start = when(*v["DTSTART"], tz)
    if "DTEND" in v:
        end = when(*v["DTEND"], tz)
    elif "DURATION" in v:
        end = start + duration(v["DURATION"][1])
    else:
        end = start + timedelta(days=1) if not isinstance(start, datetime) else start
    if isinstance(start, datetime) != isinstance(end, datetime):  # a mixed pair: the start decides
        end = start + (timedelta(hours=1) if isinstance(start, datetime) else timedelta(days=1))
    return start, end


def occurrence_key(o):
    return o.isoformat() if not isinstance(o, datetime) else o.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def expand(start, rule, last_day, tz):
    """The occurrences of a repeating event up to last_day: FREQ DAILY, WEEKLY (BYDAY), MONTHLY or YEARLY with
    INTERVAL, COUNT and UNTIL, in the event's own wall-clock time."""
    # ponytail: no BYMONTHDAY/BYSETPOS/"second Tuesday" (BYDAY on monthly); those show their first occurrence only.
    r = {k: v for k, _, v in (p.partition("=") for p in rule.upper().split(";") if p)}
    freq, n = r.get("FREQ"), max(1, int(r["INTERVAL"])) if r.get("INTERVAL", "").isdecimal() else 1
    days = sorted({WD.index(x[-2:]) for x in r["BYDAY"].split(",") if x[-2:] in WD}) if freq == "WEEKLY" and "BYDAY" in r else None
    if (freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") or set(r) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}
            or ("BYDAY" in r and not days)):
        yield start
        return
    count = int(r["COUNT"]) if r.get("COUNT", "").isdecimal() else None
    until = when({}, r["UNTIL"], UTC) if "UNTIL" in r else None
    timed = isinstance(start, datetime)
    if until is not None and not timed:
        until = day(until, tz)
    elif until is not None and not isinstance(until, datetime):
        until = datetime.combine(until, datetime.max.time(), start.tzinfo)
    k = 0
    for i in range(5000):
        if freq == "DAILY":
            cands = [start + timedelta(days=i * n)]
        elif freq == "WEEKLY":
            monday = start - timedelta(days=start.weekday()) + timedelta(weeks=i * n)
            cands = [monday + timedelta(days=x) for x in (days or [start.weekday()])]
        else:
            y, m = divmod(start.month - 1 + i * n * (12 if freq == "YEARLY" else 1), 12)
            try:
                cands = [start.replace(year=start.year + y, month=m + 1)]
            except ValueError:  # the 31st in a short month, 29 February: skipped, as the standard says
                cands = []
        for o in cands:
            if o < start:
                continue
            if (until is not None and o > until) or (count is not None and k >= count) or day(o, tz) > last_day:
                return
            k += 1
            yield o


def instances(href, etag, text, tz, last_day):
    """The occurrences in one CalDAV resource: [{id, etag, updated, shape}]. One-off: id = href; repeating:
    href + "#" + the occurrence's original start."""
    vs = vevents(text)
    master = next((v for v in vs if "RECURRENCE-ID" not in v and "DTSTART" in v), None)
    moved = {occurrence_key(when(*v["RECURRENCE-ID"], tz)): v for v in vs if "RECURRENCE-ID" in v and "DTSTART" in v}
    out = []

    def add(rid, v, start, end):
        if v.get("STATUS", ({}, ""))[1].upper() == "CANCELLED":
            return
        changed = v.get("LAST-MODIFIED") or v.get("DTSTAMP")
        title = ical_text(v.get("SUMMARY", ({}, ""))[1], back=True).strip() or "Busy"
        out.append({"id": rid, "etag": etag, "updated": when(*changed, UTC) if changed else None,
                    "shape": shape(title, not isinstance(start, datetime), start, end)})

    if not master:
        return out
    start, end = span(master, tz)
    if "RRULE" not in master:
        add(href, master, start, end)
        return out
    skip = {occurrence_key(when(p, v, tz)) for p, v in master["EXDATE"]}
    for o in expand(start, master["RRULE"][1], last_day, tz):
        key = occurrence_key(o)
        if key not in skip:
            v = moved.get(key)
            add(f"{href}#{key}", v or master, *(span(v, tz) if v else (o, o + (end - start))))
    return out


def fold(line):
    out = []
    while len(line.encode()) > 75:
        cut = 74
        while len(line[:cut].encode()) > 74:
            cut -= 1
        out.append(line[:cut])
        line = " " + line[cut:]
    return "\r\n".join(out + [line])


def ical_lines(sh):
    title, all_day, s, e = sh
    when_ = ([f"DTSTART;VALUE=DATE:{s:%Y%m%d}", f"DTEND;VALUE=DATE:{e:%Y%m%d}"] if all_day else
             [f"DTSTART:{s:%Y%m%dT%H%M%SZ}", f"DTEND:{e:%Y%m%dT%H%M%SZ}"])
    return [f"SUMMARY:{ical_text(title)}"] + when_


def stamp():
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def vcalendar(uid, sh):
    lines = (["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//FamBox//Family table//EN", "BEGIN:VEVENT", f"UID:{uid}",
              f"DTSTAMP:{stamp()}", f"LAST-MODIFIED:{stamp()}"] + ical_lines(sh) + ["END:VEVENT", "END:VCALENDAR"])
    return "\r\n".join(fold(x) for x in lines) + "\r\n"


def rewrite(text, drop, add):
    """The resource with its main VEVENT's `drop` properties replaced by the lines in `add` (everything else kept)."""
    out, depth, main, done = [], 0, False, False
    for line in unfold(text):
        if line == "BEGIN:VEVENT" and not done:
            main, depth, block = True, 0, [line]
            continue
        if main:
            if line.startswith("BEGIN:"):
                depth += 1
            elif line.startswith("END:") and depth:
                depth -= 1
            elif line == "END:VEVENT":
                main = False
                if any(split_prop(x)[0] == "RECURRENCE-ID" for x in block):  # a moved occurrence: not the main one
                    out += block + [line]
                    continue
                done = True
                out += [block[0]] + add + block[1:] + [line]
                continue
            if depth or split_prop(line)[0] not in drop:
                block.append(line)
            continue
        if line:
            out.append(line)
    return "\r\n".join(fold(x) for x in out) + "\r\n"


# ---------- iCloud (CalDAV) ----------

PROPFIND = ('<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            '<D:prop>%s</D:prop></D:propfind>')
MULTIGET = ('<?xml version="1.0" encoding="utf-8"?><C:calendar-multiget xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            '<D:prop><D:getetag/><C:calendar-data/></D:prop>%s</C:calendar-multiget>')
SYNC = ('<?xml version="1.0" encoding="utf-8"?><D:sync-collection xmlns:D="DAV:"><D:sync-token>%s</D:sync-token>'
        '<D:sync-level>1</D:sync-level><D:prop><D:getetag/></D:prop></D:sync-collection>')
xesc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def multistatus(st, data):
    if st != 207:
        raise RuntimeError(f"calendar answered {st}")
    return ET.fromstring(data)


class ICloud:
    def __init__(self, acct, tz):
        self.auth = "Basic " + base64.b64encode(f'{acct["login"]}:{acct["secret"]}'.encode()).decode()
        self.cal, self.tz = acct.get("calendar") or ICLOUD, tz

    def req(self, method, url, body=None, depth=None, headers=None):
        h = {"Authorization": self.auth, **(headers or {})}
        if body is not None:
            h.setdefault("Content-Type", "application/xml; charset=utf-8")
        if depth is not None:
            h["Depth"] = str(depth)
        st, hd, data = http(method, urllib.parse.urljoin(self.cal, url), body.encode() if isinstance(body, str) else body, h)
        if st == 401:
            raise SignIn()
        return st, hd, data

    def calendars(self):
        """The calendars this Apple ID can write events to: [{url, name}]."""
        found = lambda url, prop, tag: urllib.parse.urljoin(url, multistatus(*self.req("PROPFIND", url, PROPFIND % prop, 0)[::2])
                                                           .findtext(f".//{tag}/{DAV}href") or "")
        principal = found(ICLOUD, "<D:current-user-principal/>", DAV + "current-user-principal")
        home = found(principal, "<C:calendar-home-set/>", CAL + "calendar-home-set")
        x = multistatus(*self.req("PROPFIND", home, PROPFIND % "<D:resourcetype/><D:displayname/><C:supported-calendar-component-set/>", 1)[::2])
        out = []
        for r in x.iter(DAV + "response"):
            comps = [c.get("name") for c in r.iter(CAL + "comp")]
            if r.find(f".//{DAV}resourcetype/{CAL}calendar") is not None and (not comps or "VEVENT" in comps):
                out.append({"url": urllib.parse.urljoin(home, r.findtext(DAV + "href")),
                            "name": (r.findtext(f".//{DAV}displayname") or "Calendar").strip()[:60]})
        return out

    def pull(self, token, last_day):
        st, _, data = self.req("REPORT", self.cal, SYNC % xesc(token or ""), 0)
        if token and st in (400, 403, 409, 410):  # the sync token expired: everything again
            return self.pull(None, last_day)
        if st == 404:
            raise Gone()
        x = multistatus(st, data)
        here = urllib.parse.urlsplit(self.cal).path
        changed, gone = [], []
        for r in x.findall(DAV + "response"):
            href = urllib.parse.urlsplit(urllib.parse.urljoin(self.cal, r.findtext(DAV + "href") or "")).path
            if href.rstrip("/") == here.rstrip("/"):
                continue
            (gone if " 404" in (r.findtext(DAV + "status") or "") else changed).append(href)
        items = []
        for i in range(0, len(changed), 50):
            hrefs = "".join(f"<D:href>{xesc(h)}</D:href>" for h in changed[i:i + 50])
            for r in multistatus(*self.req("REPORT", self.cal, MULTIGET % hrefs, 1)[::2]).findall(DAV + "response"):
                href = urllib.parse.urlsplit(urllib.parse.urljoin(self.cal, r.findtext(DAV + "href") or "")).path
                text = r.findtext(f".//{CAL}calendar-data")
                if text is None:
                    gone.append(href)
                else:
                    items += instances(href, r.findtext(f".//{DAV}getetag"), text, self.tz, last_day)
        return items, gone, set(changed), not token, x.findtext(DAV + "sync-token")

    def create(self, sh):
        uid = f"fambox-{secrets.token_hex(10)}"
        href = urllib.parse.urlsplit(urllib.parse.urljoin(self.cal, uid + ".ics")).path
        st, hd, _ = self.req("PUT", href, vcalendar(uid, sh), headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
        if st not in (200, 201, 204):
            raise RuntimeError(f"calendar answered {st}")
        return href, hd.get("ETag")

    def _change(self, href, etag, edit):
        st, hd, data = self.req("GET", href)
        if st == 404:
            raise Later()  # removed there: the next pull takes it off the table
        if st != 200:
            raise RuntimeError(f"calendar answered {st}")
        if etag and hd.get("ETag") and hd["ETag"] != etag:
            raise Later()
        st, hd2, _ = self.req("PUT", href, edit(data.decode()), headers={"Content-Type": "text/calendar; charset=utf-8",
                                                                         "If-Match": hd.get("ETag") or etag or "*"})
        if st == 412:
            raise Later()
        if st not in (200, 201, 204):
            raise RuntimeError(f"calendar answered {st}")
        return hd2.get("ETag")

    def update(self, rid, etag, sh):
        href, _, one = rid.partition("#")
        if one:  # one occurrence of a repeating event: the table's edit stays on the table
            return etag
        return self._change(href, etag, lambda t: rewrite(t, {"SUMMARY", "DTSTART", "DTEND", "DURATION", "DTSTAMP", "LAST-MODIFIED"},
                                                          ical_lines(sh) + [f"DTSTAMP:{stamp()}", f"LAST-MODIFIED:{stamp()}"]))

    def delete(self, rid, etag):
        href, _, one = rid.partition("#")
        if not one:
            st, _, _ = self.req("DELETE", href)  # a removal wins over an edit made there: no If-Match
            if st not in (200, 204, 404, 410):
                raise RuntimeError(f"calendar answered {st}")
            return
        ex = f"EXDATE;VALUE=DATE:{one.replace('-', '')}" if len(one) == 10 else f"EXDATE:{one}"
        self._change(href, None, lambda t: rewrite(t, {"LAST-MODIFIED"}, [ex, f"LAST-MODIFIED:{stamp()}"]))


# ---------- Google (Calendar API) ----------

def google_form(url, **fields):
    st, _, data = http("POST", url, urllib.parse.urlencode(fields).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
    try:
        return st, json.loads(data or b"{}")
    except ValueError:
        return st, {}


def google_auth_url(redirect, state):
    return GOOGLE_AUTH + "?" + urllib.parse.urlencode({
        "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""), "redirect_uri": redirect, "response_type": "code",
        "scope": GOOGLE_SCOPES, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state})


def google_code(code, redirect):
    """The code Google sent back -> (refresh token, access token, seconds it lasts), or None."""
    st, js = google_form(GOOGLE_TOKEN, grant_type="authorization_code", code=code, redirect_uri=redirect,
                         client_id=os.environ.get("GOOGLE_CLIENT_ID", ""), client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""))
    if st != 200 or not js.get("access_token"):
        return None
    return js.get("refresh_token"), js["access_token"], int(js.get("expires_in") or 3600)


def google_when(x, tz):
    return date.fromisoformat(x["date"]) if "date" in x else datetime.fromisoformat(x["dateTime"])


def google_body(sh, tz):
    title, all_day, s, e = sh
    side = (lambda x: {"date": x.isoformat()}) if all_day else (lambda x: {"dateTime": x.astimezone(tz).isoformat(), "timeZone": str(tz)})
    return {"summary": title, "start": side(s), "end": side(e)}


class Google:
    def __init__(self, c, acct, tz):
        self.c, self.a, self.tz = c, acct, tz
        self.cal = urllib.parse.quote(acct.get("calendar") or "primary", safe="")

    def token(self, fresh=False):
        a = self.a
        if not fresh and a.get("access_token") and a.get("access_until") and a["access_until"] > datetime.now(UTC) + timedelta(minutes=1):
            return a["access_token"]
        st, js = google_form(GOOGLE_TOKEN, grant_type="refresh_token", refresh_token=a["secret"] or "",
                             client_id=os.environ.get("GOOGLE_CLIENT_ID", ""), client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""))
        if js.get("error") == "invalid_grant":
            raise SignIn()
        if st != 200 or not js.get("access_token"):
            raise RuntimeError(f"Google sign-in answered {st}")
        a["access_token"], a["access_until"] = js["access_token"], datetime.now(UTC) + timedelta(seconds=int(js.get("expires_in") or 3600))
        if a.get("id"):
            self.c.execute("update family.cal_accounts set access_token = %s, access_until = %s where id = %s",
                           (a["access_token"], a["access_until"], a["id"]))
        return a["access_token"]

    def api(self, method, path, body=None, etag=None):
        for fresh in (False, True):  # a refused key: one fresh one, then the family signs in again
            h = {"Authorization": "Bearer " + self.token(fresh), "Accept": "application/json"}
            if body is not None:
                h["Content-Type"] = "application/json"
            if etag:
                h["If-Match"] = etag
            st, _, data = http(method, GOOGLE_API + path, json.dumps(body).encode() if body is not None else None, h)
            if st != 401:
                try:
                    return st, json.loads(data) if data else {}
                except ValueError:
                    return st, {}
        raise SignIn()

    def calendars(self):
        """The calendars this Google account can write to: [{url (the id), name}]. Only the main one when the list
        is not allowed."""
        st, js = self.api("GET", "/users/me/calendarList?minAccessRole=writer")
        if st != 200:
            return [{"url": "primary", "name": "Main calendar"}]
        return [{"url": x["id"], "name": (x.get("summaryOverride") or x.get("summary") or "Calendar")[:60], "primary": x.get("primary")}
                for x in js.get("items", [])]

    def pull(self, token, last_day):
        params = {"singleEvents": "true", "showDeleted": "true", "maxResults": "250"}
        if token:
            params["syncToken"] = token
        items, gone = [], []
        while True:
            st, js = self.api("GET", f"/calendars/{self.cal}/events?" + urllib.parse.urlencode(params))
            if st == 410 and token:  # the sync token expired: everything again
                return self.pull(None, last_day)
            if st == 404:
                raise Gone()
            if st != 200:
                raise RuntimeError(f"Google answered {st}")
            for it in js.get("items", []):
                if it.get("status") == "cancelled" or "start" not in it:
                    gone.append(it["id"])
                    continue
                s, e = google_when(it["start"], self.tz), google_when(it.get("end", it["start"]), self.tz)
                upd = datetime.fromisoformat(it["updated"]) if it.get("updated") else None
                items.append({"id": it["id"], "etag": it.get("etag"), "updated": upd,
                              "shape": shape((it.get("summary") or "Busy").strip(), not isinstance(s, datetime), s, e)})
            if js.get("nextPageToken"):
                params["pageToken"] = js["nextPageToken"]
                continue
            return items, gone, set(), not token, js.get("nextSyncToken")

    def create(self, sh):
        st, js = self.api("POST", f"/calendars/{self.cal}/events", google_body(sh, self.tz))
        if st not in (200, 201):
            raise RuntimeError(f"Google answered {st}")
        return js["id"], js.get("etag")

    def update(self, rid, etag, sh):
        st, js = self.api("PATCH", f"/calendars/{self.cal}/events/{urllib.parse.quote(rid, safe='')}", google_body(sh, self.tz), etag)
        if st in (404, 410, 412):
            raise Later()
        if st != 200:
            raise RuntimeError(f"Google answered {st}")
        return js.get("etag")

    def delete(self, rid, etag):
        st, _ = self.api("DELETE", f"/calendars/{self.cal}/events/{urllib.parse.quote(rid, safe='')}")
        if st not in (200, 204, 404, 410):
            raise RuntimeError(f"Google answered {st}")


def client(c, acct, tz):
    return ICloud(acct, tz) if acct["kind"] == "icloud" else Google(c, acct, tz)


# ---------- one round ----------

def sync_account(c, acct, tz, today):
    cl = client(c, acct, tz)
    names = {p["id"]: p["name"] for p in c.execute("select id, name from family.people").fetchall()}
    by_name = {n.lower(): i for i, n in names.items()}
    lo, hi = today - BACK, today + AHEAD
    lo_at = datetime.combine(lo, datetime.min.time(), tz)
    aid = acct["id"]
    link = lambda rid, eid, etag, seen, there: c.execute(
        "insert into family.cal_links (account_id, remote_id, event_id, etag, seen, came_from_there) values (%s, %s, %s, %s, %s, %s)"
        " on conflict (account_id, remote_id) do update set event_id = excluded.event_id, etag = excluded.etag, seen = excluded.seen",
        (aid, rid, eid, etag, seen, there))
    set_etag = lambda rid, etag: c.execute("update family.cal_links set etag = %s where account_id = %s and remote_id = %s", (etag, aid, rid))

    # 1. pull
    items, gone, groups, full, token = cl.pull(acct["sync_token"], hi)
    links = {r["remote_id"]: r for r in c.execute(
        "select l.*, e.starts_at from family.cal_links l left join family.events e on e.id = l.event_id where l.account_id = %s",
        (aid,)).fetchall()}
    seen = set()
    for it in items:
        seen.add(it["id"])
        ln = links.get(it["id"])
        if not ln:
            if lo <= day(it["shape"][2], tz) <= hi:
                eid, upd = to_table(c, it["shape"], by_name, tz)
                link(it["id"], eid, it["etag"], upd, True)
            continue
        if ln["event_id"] is None or (it["etag"] and ln["etag"] == it["etag"]):
            continue  # removed on the table (the push removes it there), or nothing new
        ev = c.execute("select * from family.events where id = %s", (ln["event_id"],)).fetchone()
        if not ev:
            continue
        if table_shape(ev, names, tz) == it["shape"]:
            set_etag(it["id"], it["etag"])
        elif ev["updated_at"] > ln["seen"] and (it["updated"] is None or it["updated"] < ev["updated_at"]):
            set_etag(it["id"], it["etag"])  # the table's edit is the later one: the push writes it there
        else:
            _, upd = to_table(c, it["shape"], by_name, tz, ev["id"])
            c.execute("update family.cal_links set etag = %s, seen = %s where account_id = %s and remote_id = %s",
                      (it["etag"], upd, aid, it["id"]))
    for rid, ln in links.items():
        base = rid.partition("#")[0]
        if rid in seen or not (full or rid in gone or base in gone or base in groups):
            continue
        c.execute("delete from family.cal_links where account_id = %s and remote_id = %s", (aid, rid))
        if ln["event_id"] and ln["starts_at"] >= lo_at:  # older ones stay on the table as history
            c.execute("delete from family.events where id = %s", (ln["event_id"],))
    c.execute("update family.cal_accounts set sync_token = %s where id = %s", (token, aid))

    # 2. push
    for ln in c.execute("select * from family.cal_links where account_id = %s and event_id is null", (aid,)).fetchall():
        try:
            cl.delete(ln["remote_id"], ln["etag"])
        except Later:
            continue
        c.execute("delete from family.cal_links where account_id = %s and remote_id = %s", (aid, ln["remote_id"]))
    for ev in c.execute("select * from family.events e where starts_at >= %s and starts_at < %s and not exists"
                        " (select 1 from family.cal_links l where l.account_id = %s and l.event_id = e.id) order by starts_at",
                        (datetime.combine(today, datetime.min.time(), tz), datetime.combine(hi, datetime.min.time(), tz), aid)).fetchall():
        rid, etag = cl.create(table_shape(ev, names, tz))
        link(rid, ev["id"], etag, ev["updated_at"], False)
    for row in c.execute("select l.remote_id, l.etag, e.* from family.cal_links l join family.events e on e.id = l.event_id"
                         " where l.account_id = %s and e.updated_at > l.seen", (aid,)).fetchall():
        try:
            etag = cl.update(row["remote_id"], row["etag"], table_shape(row, names, tz))
        except Later:
            continue
        c.execute("update family.cal_links set etag = %s, seen = %s where account_id = %s and remote_id = %s",
                  (etag, row["updated_at"], aid, row["remote_id"]))


def sync_all():
    """One round for every connected calendar. Problems are kept for the home screen, never their details."""
    if not lock.acquire(blocking=False):
        return
    try:
        with d.db() as c:
            fam = c.execute("select tz from family.household").fetchone()
            if not fam:
                return
            tz = ZoneInfo(fam["tz"])
            today = datetime.now(tz).date()
            # a refused sign-in is not tried again until the family signs in again (Apple locks an ID after many)
            for acct in c.execute("select * from family.cal_accounts where calendar is not null and secret is not null"
                                  " and problem is distinct from 'signin' order by id").fetchall():
                try:
                    sync_account(c, acct, tz, today)
                    problem = None
                except SignIn:
                    problem = "signin"
                except Gone:
                    problem = "gone"
                except Exception as ex:
                    problem = "offline"
                    print(f"calendars: {acct['kind']} {type(ex).__name__}" + (f" {ex}" if isinstance(ex, RuntimeError) else ""),
                          file=sys.stderr, flush=True)
                c.execute("update family.cal_accounts set problem = %s, problem_since = case when %s::text is null then null"
                          " else coalesce(problem_since, now()) end, synced_at = case when %s::text is null then now()"
                          " else synced_at end where id = %s", (problem, problem, problem, acct["id"]))
    finally:
        lock.release()


def run():
    """Beside the dashboard: a round every few minutes, and a few seconds after the table's events change."""
    time.sleep(20)
    while True:
        try:
            with psycopg.connect(d.DSN, autocommit=True) as c:
                c.execute("listen family_changed")
                while True:
                    sync_all()
                    if any(n.payload == "events" for n in c.notifies(timeout=EVERY, stop_after=1)):
                        time.sleep(5)  # let a few quick changes settle
                        for _ in c.notifies(timeout=0.1):
                            pass
        except Exception as ex:  # never stops the dashboard
            print(f"calendars: {type(ex).__name__}", file=sys.stderr, flush=True)
            time.sleep(60)


def problem_line(a):
    """The one plain line for "What needs us now", or None."""
    name = NAMES[a["kind"]]
    if a["problem"] == "signin":
        return f"{name} calendar needs you to sign in again"
    if a["problem"] == "gone":
        return f"{name} calendar “{a['calendar_name']}” can't be found any more"
    if a["problem"] == "offline" and a["problem_since"] and a["problem_since"] < datetime.now(UTC) - timedelta(hours=1):
        return f"{name} calendar hasn't kept in step for a while"
    return None


# ---------- connecting (the Calendar page) ----------

def forget(c, aid):
    """Takes off the table what came from this calendar and forgets the links. Nothing changes over there."""
    ids = [r["event_id"] for r in c.execute("delete from family.cal_links where account_id = %s returning event_id, came_from_there",
                                            (aid,)).fetchall() if r["came_from_there"] and r["event_id"]]
    c.execute("delete from family.events where id = any(%s)", (ids,))


def connect(c, kind, login, secret, found, access=None):
    """Keeps a sign-in: a new one, or the same account again after a refusal (its calendar stays when still there).
    With one calendar it is chosen at once; otherwise the family chooses."""
    old = c.execute("select * from family.cal_accounts where kind = %s", (kind,)).fetchone()
    access_token, access_until = access or (None, None)
    if old and old["login"] == login and old["calendar"] in [f["url"] for f in found]:
        c.execute("update family.cal_accounts set secret = %s, choices = %s, access_token = %s, access_until = %s,"
                  " problem = null, problem_since = null where id = %s",
                  (secret, json.dumps(found), access_token, access_until, old["id"]))
        return
    if old:
        stop(c, old["id"])
    one = found[0] if len(found) == 1 else {}
    c.execute("insert into family.cal_accounts (kind, login, secret, choices, calendar, calendar_name, access_token, access_until)"
              " values (%s, %s, %s, %s, %s, %s, %s, %s)",
              (kind, login, secret, json.dumps(found), one.get("url"), one.get("name"), access_token, access_until))


def pick(c, aid, url):
    a = c.execute("select * from family.cal_accounts where id = %s", (aid,)).fetchone()
    ch = next((x for x in (a or {}).get("choices", []) if x["url"] == url), None)
    if not ch:
        return False
    if a["calendar"] != url:
        forget(c, aid)
        c.execute("update family.cal_accounts set calendar = %s, calendar_name = %s, sync_token = null, problem = null,"
                  " problem_since = null, synced_at = null where id = %s", (url, ch["name"], aid))
    return True


def stop(c, aid):
    forget(c, aid)
    c.execute("delete from family.cal_accounts where id = %s", (aid,))


def soon():
    """A round in a moment, once the page's own change is saved."""
    threading.Thread(target=lambda: (time.sleep(2), sync_all()), daemon=True, name="calendars-now").start()
