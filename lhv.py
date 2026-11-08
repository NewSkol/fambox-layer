#!/usr/bin/env python3
"""LHV's read-only bank connection (lhv.ai) for a family's Money page: the statement, through LHV's MCP server.

LHV's MCP server (https://mcp.lhv.ai/mcp) reads accounts and statements and cannot pay or change anything. Signing in
is OAuth with Smart-ID at auth.lhv.ai: a person does it once in a browser; the sign-in lasts 30 days (an hour at a time,
renewed by itself). Findings behind this: ~/ClaudeCode/RAIA/docs/LHV-AI-2026-09-01-RESEARCH.md.

  python3 lhv.py --check                  LHV's sign-in server answers (no sign-in, nothing saved)
  python3 lhv.py --login                  step 1: prints a link to open on your phone or computer; sign in with Smart-ID
  python3 lhv.py --login-done 'ADDRESS'   step 2: the address the browser landed on afterwards (it may say "can't connect")
  python3 lhv.py --accounts               the accounts this sign-in reads (IBANs and names)
  python3 lhv.py --fetch [--days 31] [--iban EE..] [--dry-run]
        the statement as JSON on stdout, for: dashboard.py --import-statement -
        --dry-run prints what it found (dates, amounts, who) and passes nothing on

The sign-in is kept in $FAMBOX_LHV_FILE (default ~/.config/fambox/lhv.json), readable by this user only. Tokens are
never printed. LHV_MCP_URL points at a stand-in server for the tests.
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

MCP = os.environ.get("LHV_MCP_URL", "https://mcp.lhv.ai/mcp")
FILE = Path(os.environ.get("FAMBOX_LHV_FILE", Path.home() / ".config" / "fambox" / "lhv.json"))
REDIRECT = "http://127.0.0.1:8765/callback"  # a loopback address, as LHV's sign-in expects from a person's own tool
SCOPE = "accounts:read transactions:read"


class Problem(Exception):
    pass


class _Stay(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        return None


NO_REDIRECT = urllib.request.build_opener(_Stay).open


def http(url, data=None, headers=None, form=False):
    """One request. Returns (status, headers, body text). Never logs the request: it can carry a token."""
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    h = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json", "User-Agent": "FamBox"} if body else {"User-Agent": "FamBox"}
    req = urllib.request.Request(url, body, dict(h, **(headers or {})))
    try:
        with NO_REDIRECT(req, timeout=30) as r:  # a redirect would carry the token to another address
            return r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as ex:
        return ex.code, ex.headers, ex.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError) as ex:
        raise Problem(f"LHV did not answer ({type(ex).__name__}). Try again in a minute.") from None


def load():
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}


def keep(st):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    os.chmod(tmp, 0o600)
    tmp.write_text(json.dumps(st))
    tmp.replace(FILE)


def discover():
    """LHV's sign-in server and its addresses, read from the MCP server's own description (RFC 9728, RFC 8414)."""
    base = urllib.parse.urlsplit(MCP)
    code, _, body = http(f"{base.scheme}://{base.netloc}/.well-known/oauth-protected-resource{base.path}")
    if code != 200:
        code, _, body = http(f"{base.scheme}://{base.netloc}/.well-known/oauth-protected-resource")
    if code != 200:
        raise Problem(f"LHV's MCP server did not describe itself (HTTP {code}).")
    res = json.loads(body)
    issuer = res["authorization_servers"][0]
    code, _, body = http(issuer.rstrip("/") + "/.well-known/oauth-authorization-server")
    if code != 200:
        raise Problem(f"LHV's sign-in server did not describe itself (HTTP {code}).")
    meta = json.loads(body)
    return {"resource": res.get("resource", MCP), "scopes": res.get("scopes_supported", []), **{
        k: meta.get(k) for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint")}}


def login():
    """Step 1: register FamBox as a client (once), then print the sign-in link. Saves only the client and a one-time
    check value; the link itself carries no secret."""
    st, meta = load(), discover()
    if not st.get("client_id"):
        code, _, body = http(meta["registration_endpoint"], {
            "client_name": "FamBox family table", "redirect_uris": [REDIRECT], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none", "scope": SCOPE})
        if code not in (200, 201):
            raise Problem(f"LHV refused to register FamBox as an app (HTTP {code}).")
        st["client_id"] = json.loads(body)["client_id"]
    verifier = secrets.token_urlsafe(48)
    st.update(meta, verifier=verifier, state=secrets.token_urlsafe(16))
    keep(st)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": st["client_id"], "redirect_uri": REDIRECT, "scope": SCOPE, "state": st["state"],
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": meta["resource"]})


def login_done(address):
    """Step 2: the address the browser landed on holds a one-time code; swap it for the sign-in."""
    st = load()
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(address.strip()).query)
    if q.get("error"):
        raise Problem(f"LHV said: {q['error'][0]}. Run --login again.")
    if not st.get("verifier") or (q.get("state") or [""])[0] != st.get("state"):
        raise Problem("That address is not from the last --login. Run --login again and use the new link.")
    code = (q.get("code") or [""])[0]
    if not code:
        raise Problem("That address has no code in it. Copy the whole address from the browser's address bar.")
    tokens(st, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": st["client_id"],
                "code_verifier": st["verifier"], "resource": st["resource"]})
    st.pop("verifier", None)
    st.pop("state", None)
    keep(st)


def tokens(st, form):
    code, _, body = http(st["token_endpoint"], form, form=True)
    if code != 200:
        raise Problem(f"LHV did not give a sign-in (HTTP {code}). Run --login again.")
    got = json.loads(body)
    st.update(access_token=got["access_token"], expires_at=time.time() + int(got.get("expires_in", 3600)) - 60,
              refresh_token=got.get("refresh_token", st.get("refresh_token")))


def access():
    st = load()
    if not st.get("access_token"):
        raise Problem("Not signed in to LHV yet. Run: python3 lhv.py --login")
    if time.time() > st.get("expires_at", 0):
        if not st.get("refresh_token"):
            raise Problem("The LHV sign-in ran out. Run: python3 lhv.py --login")
        try:
            tokens(st, {"grant_type": "refresh_token", "refresh_token": st["refresh_token"], "client_id": st["client_id"],
                        "resource": st.get("resource", MCP)})
        except Problem:
            raise Problem("The LHV sign-in ran out (it lasts 30 days). Run: python3 lhv.py --login") from None
        keep(st)
    return st["access_token"]


class Session:
    """A conversation with an MCP server over HTTP (JSON-RPC; the answer may come as JSON or as server-sent events)."""

    def __init__(self, token):
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                  "MCP-Protocol-Version": "2025-06-18"}
        self.n = 0
        got = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "fambox", "version": "1"}})
        self.h["MCP-Protocol-Version"] = got.get("protocolVersion", "2025-06-18")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, msg):
        code, headers, body = http(MCP, msg, self.h)
        if code == 401:
            raise Problem("LHV no longer accepts this sign-in. Run: python3 lhv.py --login")
        if code >= 400:
            raise Problem(f"LHV's MCP server said HTTP {code}.")
        if headers.get("Mcp-Session-Id"):
            self.h["Mcp-Session-Id"] = headers["Mcp-Session-Id"]
        if "text/event-stream" in (headers.get("Content-Type") or ""):
            msgs = [json.loads(x[5:]) for x in body.splitlines() if x.startswith("data:") and x[5:].strip()]
        else:
            msgs = [json.loads(body)] if body.strip() else []
        return next((m for m in msgs if m.get("id") == msg.get("id")), None)

    def call(self, method, params):
        self.n += 1
        got = self.send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        if not got or "error" in (got or {}):
            raise Problem(f"LHV's MCP server could not do {method}: {((got or {}).get('error') or {}).get('message', 'no answer')}")
        return got["result"]

    def tool(self, name, args):
        res = self.call("tools/call", {"name": name, "arguments": args})
        if res.get("isError"):
            raise Problem(f"LHV said: {' '.join(x.get('text', '') for x in res.get('content', []))[:200]}")
        if res.get("structuredContent") is not None:
            return res["structuredContent"]
        text = "".join(x.get("text", "") for x in res.get("content", []) if x.get("type") == "text")
        try:
            return json.loads(text)
        except ValueError:
            return text  # camt.053 XML or CSV: money.read_statement reads those too


def accounts(s):
    got = s.tool("list_accounts", {})
    items = got if isinstance(got, list) else next((v for v in got.values() if isinstance(v, list)), []) if isinstance(got, dict) else []
    return [a for a in items if isinstance(a, dict)]


def fetch(days=31, iban=None, limit=500):
    s = Session(access())
    if not iban:
        accs = accounts(s)
        eur = [a for a in accs if str(a.get("currency", "EUR")).upper() == "EUR"] or accs
        if not eur:
            raise Problem("LHV shows no accounts for this sign-in.")
        iban = eur[0].get("iban") or eur[0].get("IBAN")
    today = date.today()
    st = s.tool("get_account_statement", {"iban": iban, "dateFrom": (today - timedelta(days=days)).isoformat(),
                                          "dateTo": today.isoformat(), "limit": limit, "includeReservations": False,
                                          "includeBalances": False})
    return {"bank": "LHV", "iban": iban, "statement": st}


def main(argv):
    arg = lambda k, default=None: argv[argv.index(k) + 1] if k in argv and argv.index(k) + 1 < len(argv) else default
    try:
        if "--check" in argv:
            meta = discover()
            print(f"LHV answers. Sign-in at {urllib.parse.urlsplit(meta['authorization_endpoint']).netloc}; "
                  f"it can read: {', '.join(meta['scopes']) or 'not said'}. Signed in here: {'yes' if load().get('access_token') else 'no'}.")
        elif "--login" in argv:
            link = login()
            print("1. Open this link on your phone or computer and sign in with Smart-ID (choose yourself, not a company):\n")
            print(link)
            print("\n2. Afterwards the browser shows an error page or 'can't connect'. That is expected. Copy the whole address\n"
                  "   from its address bar and run:  python3 lhv.py --login-done 'THE ADDRESS'")
        elif "--login-done" in argv:
            login_done(arg("--login-done", ""))
            print(f"Signed in to LHV. The sign-in is kept in {FILE} (only you can read it) and lasts 30 days.")
        elif "--accounts" in argv:
            for a in accounts(Session(access())):
                print(a.get("iban") or a.get("IBAN"), a.get("currency", ""), a.get("name") or a.get("accountName") or "")
        elif "--fetch" in argv:
            got = fetch(int(arg("--days", "31")), arg("--iban"), int(arg("--limit", "500")))
            if "--dry-run" in argv:
                import money  # only for the dry run's look at the lines; the import itself happens in the family's box
                bank, lines = money.read_statement(json.dumps(got))
                for ln in lines:
                    print(ln["on"], f"{ln['amount']:>10.2f}", money.mask(ln["text"])[:70], file=sys.stderr)
                print(f"Dry run: {len(lines)} lines from {got['iban']}; nothing passed on.", file=sys.stderr)
            else:
                json.dump(got, sys.stdout, default=str)
        else:
            print(__doc__.split("\n\n", 2)[2].split("\n\n")[0])
            return 2
    except Problem as ex:
        print(f"LHV: {ex}", file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError) as ex:  # LHV answered something unexpected; never a traceback with a token in it
        print(f"LHV: an answer I couldn't read ({type(ex).__name__}). Nothing was saved.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
