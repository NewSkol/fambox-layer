"""Money v2 (phase B): every euro has a pot.

The month line (what came in, then Bills, Living and Dreams, and what is left to live on), bills with a day,
pots that say where their money physically is, and bank statement files (LHV, Revolut, camt.053, or the JSON the
LHV connector writes). One parser reads a typed line for the Money page and for Pip:
  "40 groceries", "paid electricity", "put 20 in the box for Lapland", "take 50 from Lapland", "salary 3200",
  "payday is the 25th", "new pot car service 400 by May".

The parsing functions use the standard library only (lhv.py imports them without a database); the ones that read or
write the family's data take a psycopg connection `c`.
"""
import csv
import io
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

NUM = r"€?\s?(\d[\d.,]*)\s?(?:€|euros?|eur)?"  # the same amount pattern as pip.py
PLACES = ("Cash box at home", "Revolut Pocket", "Bank")
INCOME_WORDS = r"salary|wages?|pay ?check|paycheque|income|child benefit|family benefit|benefit|pension|bonus|allowance|palk"
STOP = {"the", "a", "an", "our", "my", "in", "into", "to", "for", "from", "of", "out", "box", "cash", "pot", "fund", "pocket",
        "jar", "goal", "vault", "money", "savings", "bill", "at", "on", "home", "and"}
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def eur(x):
    x = float(x)
    return f"€{x:,.0f}" if x == int(x) else f"€{x:,.2f}"


def amount(x):
    """A positive amount as people write it; the same rules as dashboard.amount (kept here so lhv.py needs no database)."""
    x = re.sub(r"[€\s]|eur(os?)?", "", str(x or "").lower())
    if re.fullmatch(r"\d{1,3}([,.]\d{3})+", x):
        x = re.sub(r"[,.]", "", x)
    x = x.replace(",", ".")
    if x.count(".") > 1:
        x = x.replace(".", "", x.count(".") - 1)
    try:
        v = round(float(x), 2)
    except ValueError:
        return None
    return v if 0 < v < 1_000_000 else None


def month_of(d):
    return d.replace(day=1)


def next_month(d):
    return (d.replace(day=1) + timedelta(days=32)).replace(day=1)


def words(text):
    return [w for w in re.findall(r"[a-zäöüõšž0-9]+", text.lower()) if w not in STOP and len(w) > 2]


def best(text, items, names):
    """The item whose name(s) share the most words with `text` (a word matches when one starts the other). None if none."""
    ws = words(text)
    scored = []
    for it in items:
        mine = [w for n in names(it) for w in words(n or "")]
        score = sum(1 for w in ws if any(m.startswith(w) or w.startswith(m) for m in mine))
        if score:
            scored.append((score, -it["id"], it))
    return max(scored, key=lambda x: x[:2])[2] if scored else None


def pot_names(dreams):
    by_id = {dr["id"]: dr for dr in dreams}
    return lambda g: [g["title"], (by_id.get(g["dream_id"]) or {}).get("title")]


def find_pot(text, pots, dreams=()):
    p = best(text, pots, pot_names(dreams))
    if not p and re.search(r"\b(box|pocket|vault|bank)\b", text, re.I):  # "the box": the one pot kept there
        there = [g for g in pots if g["place"] and re.search(re.escape(re.search(r"\b(box|pocket|vault|bank)\b", text, re.I)[1]),
                                                              g["place"], re.I)]
        p = there[0] if len(there) == 1 else None
    return p


def by_date_from(text, today):
    """"May", "June 2027", "2027-05-01", "1.5.2027": the first day of that month (or that day), in the future."""
    t = text.strip().lower()
    try:
        return date.fromisoformat(t)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", t)
    if m:
        try:
            return date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            return None
    m = re.fullmatch(r"([a-z]{3})[a-z]*\.?(?:\s+(\d{4}))?", t)
    if m and m[1] in MONTHS:
        try:
            d = date(int(m[2]) if m[2] else today.year, MONTHS.index(m[1]) + 1, 1)
        except ValueError:  # "May 0000"
            return None
        return d if m[2] or d > today else d.replace(year=d.year + 1)
    return None


def read_line(text, pots=(), bills=(), dreams=(), today=None, loose=True):
    """One typed line to what it would do, without doing it. Returns a dict with "do" (spend, bill, pot_in, pot_out,
    income, new_pot, payday) and its details, or None. `loose` (the Money page's line) also reads "groceries 40";
    Pip reads only the forms that cannot be a date ("Add swimming on Friday at 5" ends in a number too)."""
    today = today or date.today()
    t = re.sub(r"\s+", " ", (text or "").strip(" .!?")).strip()
    if not t or len(t) > 200:
        return None
    m = re.fullmatch(r"(?:our |my |the )?pay ?day(?: is)?(?: on)?(?: the)? (\d{1,2})(?:st|nd|rd|th)?(?: of (?:each|every|the) month)?|"
                     r"we get paid on the (\d{1,2})(?:st|nd|rd|th)?", t, re.I)
    if m and 1 <= int(m[1] or m[2]) <= 31:
        return {"do": "payday", "day": int(m[1] or m[2])}
    m = re.fullmatch(r"(?:take|took|taken|use|used|spend|spent)\s+" + NUM + r"\s+(?:out\s+)?(?:of|from)\s+(.+)", t, re.I)
    if m and amount(m[1]):
        p = find_pot(m[2], pots, dreams)
        if p:
            return {"do": "pot_out", "amount": amount(m[1]), "id": p["id"], "name": p["title"]}
    m = (re.fullmatch(r"(?:put|save|saved|add|added|move|moved|set aside)\s+" + NUM + r"\s+(?:in|into|to|for|towards?|aside for)\s+(.+)", t, re.I)
         or re.fullmatch(NUM + r"\s+(?:in|into|to|towards?)\s+(.+)", t, re.I))
    if m and amount(m[1]):
        p = find_pot(m[2], pots, dreams)
        if p:
            return {"do": "pot_in", "amount": amount(m[1]), "id": p["id"], "name": p["title"]}
    m = re.fullmatch(r"(?:new |a new |another )?pot(?: for)?:? (.+?) " + NUM + r"(?: by (.+))?", t, re.I)
    if m and amount(m[2]):
        by = by_date_from(m[3], today) if m[3] else None
        if not m[3] or by:
            return {"do": "new_pot", "title": m[1][:1].upper() + m[1][1:60], "amount": amount(m[2]), "by": by}
    m = (re.fullmatch(r"(?:got |received |earned )?(?:the |our |my )?(" + INCOME_WORDS + r")(?: came in| is in| in)?:? (?:of )?" + NUM, t, re.I)
         or re.fullmatch(r"(?:got |received |earned )?" + NUM + r" (?:of |in |as )?(" + INCOME_WORDS + r")(?: came in| is in| in)?", t, re.I))
    if m:
        amt, what = (m[2], m[1]) if not m[1][0].isdigit() and not m[1].startswith("€") else (m[1], m[2])
        if amount(amt):
            return {"do": "income", "amount": amount(amt), "note": what[:1].upper() + what[1:].lower()}
    m = re.fullmatch(r"(?:got|received) " + NUM + r"(?: (?:from|for) (.+))?", t, re.I)
    if m and amount(m[1]):
        return {"do": "income", "amount": amount(m[1]), "note": (m[2] or "Money in")[:1].upper() + (m[2] or "Money in")[1:80]}
    m = (re.fullmatch(r"(?:we |i )?(?:paid|pay) (?:the )?(.+?)(?: bill)?(?: " + NUM + r")?", t, re.I)
         or re.fullmatch(r"(?:the )?(.+?)(?: bill)? (?:is |was )?paid(?: " + NUM + r")?", t, re.I))
    if m and not re.match(r"\d|€", m[1]):
        b = best(m[1], bills, lambda b: [b["title"]])
        if b:
            return {"do": "bill", "id": b["id"], "name": b["title"], "amount": amount(m[2]) if m[2] else float(b["amount"])}
    m = re.fullmatch(r"(?:we |i )?(?:spent|paid) " + NUM + r" (?:on|for|at) (.+)", t, re.I)
    if m and amount(m[1]):
        b = best(m[2], bills, lambda b: [b["title"]]) if re.match(r"paid", t, re.I) else None
        if b:
            return {"do": "bill", "id": b["id"], "name": b["title"], "amount": amount(m[1])}
        return spend(m[1], m[2])
    m = re.fullmatch(NUM + r" (?:on |for |at )?([^\d€].*)", t, re.I)
    if m and amount(m[1]) and not re.match(r"(am|pm|o'?clock|points?|minutes?|hours?|days?|weeks?|months?|years?|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
                                           m[2], re.I):
        return spend(m[1], m[2])
    if loose:
        m = re.fullmatch(r"([^\d€].*?):? " + NUM, t, re.I)
        if m and amount(m[2]) and not re.search(r"(\bat|\bon)$|\b(today|tonight|tomorrow|mon|tue|wed|thu|fri|sat|sun)", m[1], re.I):
            return spend(m[2], m[1])
    return None


def spend(amt, note):
    note = re.sub(r"^(?:on|for|at) ", "", note.strip(), flags=re.I)
    return {"do": "spend", "amount": amount(amt), "note": note[:1].upper() + note[1:80], "what": note[:80]}


def say(it, today=None):
    """What a line will do, in a few words (the preview under the Money page's line)."""
    if not it:
        return ""
    a = eur(it["amount"]) if it.get("amount") else ""
    return {
        "spend": lambda: f"Writes down {a} for {it.get('what') or it['note']}, under Living.",
        "bill": lambda: f"Marks {it['name']} paid this month: {a}.",
        "pot_in": lambda: f"Puts {a} in the {it['name']}.",
        "pot_out": lambda: f"Takes {a} out of the {it['name']}.",
        "income": lambda: f"Writes down {a} coming in: {it['note'].lower()}.",
        "new_pot": lambda: f"Makes a new pot, {it['title']}: {a}" + (f" by {it['by']:%B %Y}." if it.get("by") else "."),
        "payday": lambda: f"Sets payday to the {it['day']}{suffix(it['day'])} of each month.",
    }[it["do"]]()


def suffix(n):
    return "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


# ---------- the family's numbers ----------

def lists(c):
    return (c.execute("select * from family.goals order by id").fetchall(),
            c.execute("select * from family.bills order by due_day, id").fetchall(),
            c.execute("select id, title from family.dreams").fetchall())


def line(c, text, today, loose=True):
    pots, bills, dreams = lists(c)
    return read_line(text, pots, bills, dreams, today, loose)


def pot_move(c, gid, amt, note="", on=None):
    """Money into (+) or out of (-) a pot. Never below zero: taking out more than is there takes out what is there."""
    g = c.execute("select saved from family.goals where id = %s for update", (gid,)).fetchone()
    if not g:
        return None
    amt = round(max(float(amt), -float(g["saved"])), 2)
    if amt:
        c.execute("insert into family.pot_moves (goal_id, amount, note, on_date) values (%s, %s, %s, coalesce(%s, current_date))",
                  (gid, amt, note[:80], on))
    return c.execute("update family.goals set saved = saved + %s where id = %s returning saved", (amt, gid)).fetchone()["saved"]


def mark_paid(c, bid, on, amt=None):
    """A bill paid for the month of `on`: once a month; the payment is written down under Bills."""
    b = c.execute("select * from family.bills where id = %s", (bid,)).fetchone()
    if not b or (b["paid_month"] and b["paid_month"] >= month_of(on)):
        return False
    c.execute("update family.bills set paid_month = %s where id = %s", (month_of(on), bid))
    c.execute("insert into family.spending (amount, note, bill_id, spent_on) values (%s, %s, %s, %s)",
              (amt or b["amount"], b["title"], bid, on))
    return True


def apply(c, it, today, note=""):
    """Does what read_line read. Returns Pip's sentence."""
    do = it["do"]
    if do == "spend":
        c.execute("insert into family.spending (amount, note, spent_on) values (%s, %s, %s)", (it["amount"], it["note"], today))
        return f"Written down: {eur(it['amount'])} for {it.get('what') or it['note']}. " + left_words(c, today)
    if do == "bill":
        if not mark_paid(c, it["id"], today, it["amount"]):
            return f"{it['name']} is already marked paid for {today:%B}."
        return f"Marked paid: {it['name']}, {eur(it['amount'])}. " + left_words(c, today)
    if do in ("pot_in", "pot_out"):
        before = c.execute("select saved from family.goals where id = %s", (it["id"],)).fetchone()["saved"]
        saved = pot_move(c, it["id"], it["amount"] if do == "pot_in" else -it["amount"], note)
        g = c.execute("select * from family.goals where id = %s", (it["id"],)).fetchone()
        where = f" ({g['place']})" if g["place"] else ""
        verb = "Put" if do == "pot_in" else "Took"
        return f"{verb} {eur(abs(saved - before))} {'in' if do == 'pot_in' else 'out of'} the {g['title']}{where}: {eur(saved)} of {eur(g['target'])}."
    if do == "income":
        c.execute("insert into family.income (amount, note, on_date) values (%s, %s, %s)", (it["amount"], it["note"], today))
        return f"Written down: {eur(it['amount'])} came in ({it['note'].lower()}). " + left_words(c, today)
    if do == "new_pot":
        c.execute("insert into family.goals (title, target, by_date) values (%s, %s, %s)", (it["title"], it["amount"], it.get("by")))
        each = per_month(it["amount"], 0, it.get("by"), today)
        return f"New pot: {it['title']}, {eur(it['amount'])}" + (f", about {eur(each)} a month." if each else ".")
    if do == "payday":
        c.execute("update family.household set payday = %s", (it["day"],))
        return f"Payday is the {it['day']}{suffix(it['day'])}. That day I'll ask if you want to save first."
    return ""


def per_month(target, saved, by, today):
    """What to put in each month to reach a pot's amount by its date, rounded up to tens. None without a date."""
    if not by or by <= today or saved >= target:
        return None
    months = max(1, (by.year - today.year) * 12 + by.month - today.month)
    return math.ceil(float(target - saved) / months / 10) * 10


def bill_state(b, today):
    """("paid" | "late" | "today" | "soon" | "later", words, due day) for this month's bill."""
    m0 = month_of(today)
    last = (next_month(today) - timedelta(days=1)).day
    due = m0.replace(day=min(b["due_day"], last))
    if b["paid_month"] and b["paid_month"] >= m0:
        return "paid", "Paid", due
    n = (due - today).days
    if n < 0:
        return "late", f"{-n} day{'s' if n != -1 else ''} late", due
    if n == 0:
        return "today", "Due today", due
    return ("soon" if n <= 3 else "later"), ("Due tomorrow" if n == 1 else f"Due in {n} days"), due


def month(c, today, fam=None):
    """This month in numbers. `base` is what came in, or the family's plan when nothing is written in yet."""
    fam = fam or c.execute("select money_plan from family.household").fetchone() or {}
    m0, m1 = month_of(today), next_month(today)
    q = lambda sql: float(c.execute(sql, (m0, m1)).fetchone()["s"] or 0)
    came = q("select sum(amount) s from family.income where on_date >= %s and on_date < %s")
    living = q("select sum(amount) s from family.spending where bill_id is null and spent_on >= %s and spent_on < %s")
    paid = q("select sum(amount) s from family.spending where bill_id is not null and spent_on >= %s and spent_on < %s")
    dreams = max(0.0, q("select sum(amount) s from family.pot_moves where on_date >= %s and on_date < %s"))
    unpaid = float(c.execute("select coalesce(sum(amount), 0) s from family.bills where paid_month is null or paid_month < %s",
                             (m0,)).fetchone()["s"])
    bills = paid + unpaid
    plan = float(fam["money_plan"]) if fam.get("money_plan") is not None else None
    base = came or plan
    left = None if base is None else base - bills - living - dreams
    return dict(came=came, plan=plan, base=base, from_plan=not came and plan is not None, bills=bills, living=living,
                dreams=dreams, left=left, name=m0.strftime("%B"), to_go=(m1 - today).days - 1)


def left_words(c, today):
    m = month(c, today)
    days = f"{m['to_go']} day{'s' if m['to_go'] != 1 else ''} to go" if m["to_go"] else "last day of the month"
    if m["base"] is None:
        return "Write down what comes in (\"salary 3200\") and I'll keep count of what is left."
    of = f" of the {eur(m['plan'])} plan" if m["from_plan"] else ""
    if m["left"] >= 0:
        return f"{eur(m['left'])} left to live on{of}, {days}."
    return f"{eur(-m['left'])} more than came in{of}, {days}."


# ---------- bank statements: LHV and Revolut CSV, camt.053, the LHV connector's JSON ----------

def mask(text):
    """Card and other long numbers keep only their last four digits."""
    return re.sub(r"(?<![\d*])(?:\d[ -]?){11,18}\d(?!\d)", lambda m: "•••• " + re.sub(r"\D", "", m[0])[-4:], text)


def signed(x):
    """"-1 234,56", "1,234.56", "−12.3": a signed amount. None when it is not one."""
    s = re.sub(r"[\s €]", "", str(x or "")).replace("−", "-").lstrip("+")
    if not s or s == "-":
        return None
    comma, point = s.rfind(","), s.rfind(".")
    if comma >= 0 and point >= 0:
        s = s.replace(".", "").replace(",", ".") if comma > point else s.replace(",", "")
    elif comma >= 0:
        s = s.replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None
    return round(float(s), 2)


def day(x):
    s = str(x or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s) or re.match(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", s)
    if not m:
        return None
    y, mo, d = (m[1], m[2], m[3]) if len(m[1]) == 4 else (m[3], m[2], m[1])
    try:
        return date(int(y), int(mo), int(d))
    except ValueError:
        return None


# Which column is which, by its name. Ported (smaller) from RAIA's engine/src/importers/csv-shape.ts ROLES, which was
# checked against LHV's published sixteen columns (Estonian and English) and Revolut's export; plus Revolut's personal
# export (Type, Product, Started Date, Completed Date, Description, Amount, Fee, Currency, State, Balance).
ROLES = [
    ("date", [r"^completed date$", r"^date completed", r"^kuup[äa]ev$", r"^date$", r"^booking ?date$", r"kande.?p[äa]ev",
              r"transaction date", r"kuup[äa]ev|date"], r"started"),
    ("dc", [r"deebet\s*/\s*kreedit", r"debit\s*/\s*credit", r"\(d/c\)", r"^d/c$", r"^d/k$"], None),
    ("amount", [r"^summa$", r"^amount$", r"summa|amount"], r"orig|fee|balance|saldo|tasu|komisjon"),
    ("fee", [r"^fee$", r"^teenustasu$"], None),
    ("currency", [r"^valuuta$", r"^currency$", r"valuuta|currency"], r"orig|exchange"),
    ("name", [r"(saaja|maksja).*nimi", r"beneficiary'?s? name", r"counterparty name", r"^payee$", r"^payer$", r"^merchant$"],
     r"konto|account|iban|bic|pank|bank|kood|code|algataja|ultimate"),
    ("text", [r"^selgitus$", r"^description$", r"selgitus|description|details|explanation"], None),
    ("state", [r"^state$", r"^status$"], None),
    ("type", [r"^type$"], None),
]


def columns(header):
    cells = [h.replace("﻿", "").strip().strip('"').lower() for h in header]
    found, used = {}, set()
    for role, pats, unless in ROLES:
        for p in pats:
            i = next((i for i, h in enumerate(cells) if i not in used and re.search(p, h) and not (unless and re.search(unless, h))), None)
            if i is not None:
                found[role] = i
                used.add(i)
                break
    return found if "date" in found and "amount" in found else None


def read_statement(raw, name=""):
    """A statement file's bytes (or text) to (bank, lines); each line {on, amount, text, hint}. Raises ValueError with a
    sentence for the family when the file is not a statement we can read."""
    if isinstance(raw, bytes):
        if raw[:2] == b"PK" or raw[:4] == b"%PDF":
            raise ValueError("That is an Excel or PDF file. Download the statement as CSV and send that.")
        for enc in ("utf-8-sig", "cp1257", "latin-1"):
            try:
                raw = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    text = raw.replace("\x00", "").lstrip("\ufeff \r\n\t")
    if text.startswith("<"):
        return "bank", camt(text)
    if text.startswith(("{", "[")):
        try:
            data = json.loads(text)
        except ValueError:
            raise ValueError("That file looks like data, but it is not a statement we can read.") from None
        return from_json(data)
    return from_csv(text, name)


def from_csv(text, name=""):
    rows = text.splitlines()
    for n, first in enumerate(rows[:12]):
        delim = ";" if first.count(";") > first.count(",") else ","
        cols = columns(next(csv.reader([first], delimiter=delim)))
        if cols:
            break
    else:
        raise ValueError("I couldn't find the dates and amounts in that file. Download the statement as CSV from "
                         "your internet bank (LHV: Statement, CSV; Revolut: Statement, Excel or CSV) and try again.")
    head = [h.strip().lower() for h in next(csv.reader([rows[n]], delimiter=delim))]
    bank = "Revolut" if "product" in head and "started date" in head else "LHV" if any(
        "kliendi konto" in h or "customer account" in h for h in head) or "lhv" in name.lower() else "bank"
    out = []
    for r in csv.reader(rows[n + 1:], delimiter=delim):
        cell = lambda k: r[cols[k]].strip() if k in cols and cols[k] < len(r) else ""
        on, amt = day(cell("date")), signed(cell("amount"))
        if not on or amt is None or amt == 0:
            continue
        if cell("state") and cell("state").upper() not in ("COMPLETED", "BOOKED", ""):
            continue  # Revolut: pending, reverted, declined
        if cell("currency") and cell("currency").upper() != "EUR":
            continue  # the family's money is in euros; other currencies would add apples to pears
        kind = cell("type").upper()
        if kind == "EXCHANGE":
            continue
        dc = cell("dc").upper()
        if dc[:1] == "D" and amt > 0:
            amt = -amt
        elif dc[:1] in ("C", "K") and amt < 0:
            amt = -amt
        fee = signed(cell("fee")) or 0
        if bank == "Revolut" and fee > 0:
            amt = round(amt - fee, 2)
        who, what = cell("name"), cell("text")
        parts = [x for x in (who, what) if x]
        out.append({"on": on, "amount": amt, "text": " · ".join(dict.fromkeys(parts)) or "Bank line",
                    "hint": "own" if kind == "TOPUP" else None})
    return bank, out


def _local(el):
    return el.tag.rsplit("}", 1)[-1]


def _find(el, *path):
    """The first element down a path of local names (namespaces ignored), or None."""
    for name in path:
        el = next((x for x in el if _local(x) == name), None) if el is not None else None
    return el


def camt(text):
    """ISO 20022 camt.053 (LHV's statement XML; possibly what lhv.ai's statement tool returns)."""
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.I):  # a statement never has one; entities are how XML attacks start
        raise ValueError("That file looks like XML, but it is not a statement we can read.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise ValueError("That file looks like XML, but it is not a statement we can read.") from None
    out = []
    for e in root.iter():
        if _local(e) != "Ntry":
            continue
        sts = _find(e, "Sts")
        status = "" if sts is None else ((_find(sts, "Cd").text if _find(sts, "Cd") is not None else sts.text) or "").strip()
        if status not in ("BOOK", ""):
            continue
        amt_el = _find(e, "Amt")
        if amt_el is not None and amt_el.get("Ccy", "EUR") != "EUR":
            continue
        amt = signed(amt_el.text if amt_el is not None else "")
        bd = _find(e, "BookgDt")
        on = day(next((x.text for x in (bd if bd is not None else []) if _local(x) in ("Dt", "DtTm")), ""))
        if amt is None or not on:
            continue
        ind = _find(e, "CdtDbtInd")
        if ind is None or (ind.text or "").strip() not in ("DBIT", "CRDT"):
            continue
        out_ = ind.text.strip() == "DBIT"
        tx = _find(e, "NtryDtls", "TxDtls")
        who = _find(tx, "RltdPties", "Cdtr" if out_ else "Dbtr", "Nm") if tx is not None else None
        if who is None and tx is not None:  # camt.053.001.08 wraps the party once more
            who = _find(tx, "RltdPties", "Cdtr" if out_ else "Dbtr", "Pty", "Nm")
        what = " ".join((x.text or "").strip() for x in (tx.iter() if tx is not None else []) if _local(x) == "Ustrd") or \
            ((_find(e, "AddtlNtryInf").text or "") if _find(e, "AddtlNtryInf") is not None else "")
        name = (who.text or "").strip() if who is not None else ""
        out.append({"on": on, "amount": -abs(amt) if out_ else abs(amt),
                    "text": " · ".join(x for x in (name, what.strip()) if x) or "Bank line", "hint": None})
    return out


def from_json(data):
    """A statement as JSON: the LHV connector's {"statement": ...}, or a list of transactions with common key names."""
    if isinstance(data, dict) and "statement" in data:
        inner = data["statement"]
        bank = data.get("bank") or "LHV"
        if isinstance(inner, str):
            return bank, read_statement(inner)[1]
        return bank, from_json(inner)[1]
    items = data
    for _ in range(3):  # {"transactions": [...]}, {"data": {"entries": [...]}}
        if isinstance(items, dict):
            items = next((v for k, v in items.items() if isinstance(v, (list, dict)) and k.lower() in
                          ("transactions", "entries", "items", "data", "statement", "booked", "rows", "results")), None)
    if not isinstance(items, list):
        raise ValueError("That file looks like data, but it has no list of payments in it.")
    pick = lambda d, *ks: next((d[k] for k in ks if isinstance(d, dict) and d.get(k) not in (None, "")), None)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if str(pick(it, "status", "state", "entryStatus") or "BOOK").upper()[:4] in ("PDNG", "PEND", "INFO", "DECL", "REVE"):
            continue
        a = pick(it, "amount", "transactionAmount", "sum", "value")
        cur = pick(a, "currency", "ccy") if isinstance(a, dict) else pick(it, "currency", "ccy")
        if cur and str(cur).upper() != "EUR":
            continue
        amt = signed(pick(a, "amount", "value") if isinstance(a, dict) else a)
        on = day(pick(it, "bookingDate", "booking_date", "date", "valueDate", "transactionDate", "completedDate"))
        if amt is None or not on or amt == 0:
            continue
        ind = str(pick(it, "creditDebitIndicator", "creditDebit", "direction", "dc") or "").upper()
        if ind in ("D", "DBIT", "DEBIT", "OUT", "OUTGOING"):
            amt = -abs(amt)
        elif ind in ("C", "K", "CRDT", "CREDIT", "IN", "INCOMING"):
            amt = abs(amt)
        party = pick(it, "counterpartyName", "creditorName", "debtorName", "counterparty", "merchant", "name", "payee", "payer")
        if isinstance(party, dict):
            party = pick(party, "name")
        what = pick(it, "remittanceInformation", "remittanceInformationUnstructured", "description", "details", "explanation",
                    "reference")
        if isinstance(what, list):
            what = " ".join(map(str, what))
        out.append({"on": on, "amount": amt, "text": " · ".join(str(x).strip() for x in (party, what) if x) or "Bank line",
                    "hint": None})
    return "bank", out


def pot_in_line(text, pots, dreams):
    """The pot a bank line names, strictly: a whole word of four letters or more from the pot's (or its dream's) name,
    and a word that says it is a pot ("To pocket EUR Lapland"). "Card payment" is never the Car service pot."""
    if not re.search(r"pocket|vault|savings|s[äa][äa]st|pot\b|fund|box|hoius|kogumis", text, re.I):
        return None
    ws = set(re.findall(r"[a-zäöüõšž0-9]+", text.lower()))
    names = pot_names(dreams)
    hits = [g for g in pots if any(w in ws for n in names(g) for w in words(n or "") if len(w) >= 4)]
    return hits[0] if hits else None


def propose(c, ln, pots, bills, dreams):
    """What a bank line most likely is: a bill paid, money into or out of a pot, spending, money in, or nothing new."""
    t, amt, on = ln["text"], float(ln["amount"]), ln["on"]
    if ln.get("hint") == "own":
        return {"do": "skip", "why": "Moving your own money"}
    near = lambda table, col, extra="": c.execute(f"select 1 from family.{table} where abs(amount) = %s and {col} between %s and %s"
                                                  + extra + " limit 1", (abs(amt), on - timedelta(days=3), on + timedelta(days=3))).fetchone()
    p = pot_in_line(t, pots, dreams)
    if p:
        if near("pot_moves", "on_date", f" and goal_id = {int(p['id'])}"):
            return {"do": "skip", "why": "Already in the pot"}
        return {"do": "pot_in" if amt < 0 else "pot_out", "id": p["id"]}
    if amt < 0:
        if near("spending", "spent_on", " and bill_id is null"):
            return {"do": "skip", "why": "Already written down"}
        b = best(t, bills, lambda b: [b["title"]]) or next(
            (b for b in bills if float(b["amount"]) == -amt and abs((on.replace(day=min(b["due_day"], 28)) - on).days) <= 5), None)
        if b:
            if b["paid_month"] and b["paid_month"] >= month_of(on):
                return {"do": "skip", "why": f"{b['title']} is already marked paid"}
            return {"do": "bill", "id": b["id"]}
        return {"do": "spend"}
    if near("income", "on_date"):
        return {"do": "skip", "why": "Already written down"}
    return {"do": "income"}


def store(c, bank, lines):
    """Keeps a statement's lines for the family to accept. Returns (new, already seen). The same line twice in one file
    (two coffees on one day) stays two lines; the same file twice adds nothing."""
    pots, bills, dreams = lists(c)
    seen, new, old = {}, 0, 0
    for ln in lines:
        if abs(ln["amount"]) >= 10_000_000:
            continue
        text = mask(ln["text"])[:200]
        key = (ln["on"], ln["amount"], text)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            text = f"{text} ({seen[key]})"
        prop = propose(c, dict(ln, text=text), pots, bills, dreams)
        got = c.execute("insert into family.bank_lines (on_date, amount, text, source, hint, proposal) values (%s, %s, %s, %s, %s, %s)"
                        " on conflict do nothing returning id", (ln["on"], ln["amount"], text, bank, ln.get("hint"),
                                                                 json.dumps(prop))).fetchone()
        new, old = new + bool(got), old + (not got)
    return new, old


def accept(c, choices=None, only=None):
    """Accepts the family's new bank lines: each as chosen ({id: "spend" | "bill:3" | ...}) or as proposed.
    Returns how many of each were done."""
    done = {}
    for ln in c.execute("select * from family.bank_lines where state = 'new'" + (" and id = any(%s)" if only else "") +
                        " order by on_date, id for update", (only,) if only else ()).fetchall():
        pick = (choices or {}).get(ln["id"])
        prop = ln["proposal"] or {"do": "skip"}
        do, ref = (pick.split(":") + [None])[:2] if pick else (prop["do"], prop.get("id"))
        ref = int(ref) if ref and str(ref).isdecimal() else None
        amt, on, text = abs(float(ln["amount"])), ln["on_date"], ln["text"][:80]
        if do == "spend":
            c.execute("insert into family.spending (amount, note, spent_on) values (%s, %s, %s)", (amt, text, on))
        elif do == "bill" and ref and not mark_paid(c, ref, on, amt):
            do = "skip"
        elif do in ("pot_in", "pot_out") and ref:
            if pot_move(c, ref, amt if do == "pot_in" else -amt, text, on) is None:
                do = "skip"
        elif do == "income":
            c.execute("insert into family.income (amount, note, on_date) values (%s, %s, %s)", (amt, text, on))
        elif do not in ("bill", "pot_in", "pot_out") or not ref:
            do = "skip"
        c.execute("update family.bank_lines set state = %s, proposal = %s where id = %s",
                  ("skipped" if do == "skip" else "done", json.dumps({"do": do, "id": ref}), ln["id"]))
        done[do] = done.get(do, 0) + 1
    return done


def done_words(done):
    parts = [f"{n} {w}" for k, w in (("bill", "bills paid"), ("spend", "spendings"), ("income", "money in"),
                                      ("pot_in", "into pots"), ("pot_out", "out of pots")) if (n := done.get(k))]
    return ", ".join(parts) or "nothing new"


def summary(c, n_new, bank):
    """Pip's sentence after reading a statement: what it found and what it thinks each line is."""
    rows = c.execute("select proposal->>'do' d, count(*) n from family.bank_lines where state = 'new' group by 1").fetchall()
    got = {r["d"]: r["n"] for r in rows}
    total = sum(got.values())
    if not n_new:
        return f"I read your {bank} statement: nothing new in it, every line is already on your table."
    return (f"I read {n_new} new line{'s' if n_new != 1 else ''} from your {bank} statement: {done_words(got)}"
            + (f", {got['skip']} to leave out" if got.get("skip") else "") +
            f". Reply YES to accept all {total}, or change them on the Money page first.")


if __name__ == "__main__":  # a quick look at a statement file, without a database: python3 money.py FILE
    import sys
    bank, got = read_statement(open(sys.argv[1], "rb").read(), sys.argv[1])
    for ln in got:
        print(ln["on"], f"{ln['amount']:>10.2f}", mask(ln["text"])[:70])
    print(f"{bank}: {len(got)} lines")
