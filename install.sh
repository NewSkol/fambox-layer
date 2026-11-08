#!/usr/bin/env bash
# FamBox family layer: installs or updates the family dashboard on a FamBox box.
# The engine runs it as the box user after checking the release signature (VibeBox's
# kit-update.sh, which also snapshots the database first and puts it back if this fails).
# Safe to re-run.
#
#   bash install.sh             install or update
#   bash install.sh --dry-run   print every step, change nothing
#
# Reads /etc/fambox/box.env (written by the engine, root only):
#   FAMBOX_ADDRESS        the family's address, e.g. kask.fam.box
#   FAMBOX_GATE_USER/_PASSWORD   a password in front of the dashboard. Required until sign-in
#                         exists (prompt 06): without it this refuses to open the dashboard.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HOME/.fambox/venv"
PSYCOPG='psycopg[binary]==3.3.6'
PORT=8090
DRY=0; [ "${1:-}" = --dry-run ] && DRY=1
say()  { echo "[fambox-layer] $*"; }
fail() { echo "[fambox-layer] FAILED: $*" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then echo "    would run: $*"; else "$@"; fi; }

settings=$(sudo -n cat /etc/fambox/box.env 2>/dev/null)
[ -n "$settings" ] || [ "$DRY" = 1 ] || fail "no /etc/fambox/box.env: the engine writes it before installing the layer"
[ -n "$settings" ] || settings=$'FAMBOX_ADDRESS=<address from box.env>\nFAMBOX_GATE_USER=<user>\nFAMBOX_GATE_PASSWORD=<password>'
get() { printf '%s\n' "$settings" | sed -n "s/^$1=//p" | tail -1; }
ADDRESS=$(get FAMBOX_ADDRESS); GATE_USER=$(get FAMBOX_GATE_USER); GATE_PW=$(get FAMBOX_GATE_PASSWORD)
[ -n "$ADDRESS" ] || fail "FAMBOX_ADDRESS missing in /etc/fambox/box.env"
[ -n "$GATE_USER" ] && [ -n "$GATE_PW" ] || fail "no gate password in /etc/fambox/box.env; the dashboard has no sign-in yet, so it stays closed"
say "release $(cat "$HERE/VERSION" 2>/dev/null || echo unreleased) for $ADDRESS$( [ "$DRY" = 1 ] && echo ' (dry run: nothing will change)')"

say "-- python and psycopg"
if [ "$DRY" = 1 ]; then echo "    would make $VENV and install $PSYCOPG"
else
  [ -x "$VENV/bin/python" ] || python3 -m venv "$VENV" || fail "could not make $VENV"
  "$VENV/bin/python" -c 'import psycopg, sys; sys.exit(psycopg.__version__ != "3.3.6")' 2>/dev/null \
    || "$VENV/bin/pip" -q install "$PSYCOPG" || fail "could not install $PSYCOPG"
fi

say "-- family schema in the $USER database"
run env FAMBOX_DB="dbname=$USER" "$VENV/bin/python" "$HERE/dashboard.py" --init || fail "schema not ready"

say "-- dashboard service on 127.0.0.1:$PORT"
unit="[Unit]
Description=FamBox family dashboard
After=postgresql.service

[Service]
User=$USER
WorkingDirectory=$HERE
Environment=FAMBOX_DB=dbname=$USER HOST=127.0.0.1 PORT=$PORT FAMBOX_QUIET=1 FAMBOX_ADDRESS=$ADDRESS
ExecStart=$VENV/bin/python $HERE/dashboard.py
Restart=on-failure

[Install]
WantedBy=multi-user.target"
if [ "$DRY" = 1 ]; then echo "    would write /etc/systemd/system/fambox-dashboard.service and restart it"
else
  printf '%s\n' "$unit" | sudo tee /etc/systemd/system/fambox-dashboard.service >/dev/null || fail "could not write the service"
  sudo systemctl daemon-reload && sudo systemctl enable -q fambox-dashboard && sudo systemctl restart fambox-dashboard \
    || fail "the dashboard service did not start"
fi

say "-- $ADDRESS in the web server, behind the gate (the health check stays open)"
if [ "$DRY" = 1 ]; then echo "    would write /etc/caddy/fambox.caddy, import it from the Caddyfile, reload Caddy"
else
  hash=$(printf '%s\n' "$GATE_PW" | caddy hash-password 2>/dev/null | tail -1)
  case "$hash" in '$2'*) ;; *) fail "could not hash the gate password" ;; esac
  printf '%s {\n\t@private not path /healthz\n\tbasicauth @private {\n\t\t%s %s\n\t}\n\treverse_proxy 127.0.0.1:%s\n}\n' \
    "$ADDRESS" "$GATE_USER" "$hash" "$PORT" | sudo tee /etc/caddy/fambox.caddy >/dev/null || fail "could not write the site"
  sudo grep -qx 'import /etc/caddy/fambox.caddy' /etc/caddy/Caddyfile \
    || echo 'import /etc/caddy/fambox.caddy' | sudo tee -a /etc/caddy/Caddyfile >/dev/null || fail "could not add the site"
  sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || fail "the web server settings are invalid"
  sudo systemctl reload-or-restart caddy || fail "the web server did not reload"
fi

say "-- health check"
if [ "$DRY" = 1 ]; then echo "    would wait for http://127.0.0.1:$PORT/healthz"; exit 0; fi
for _ in $(seq 1 20); do
  out=$(curl -s --fail "http://127.0.0.1:$PORT/healthz") && { say "dashboard answers: $out"; exit 0; }
  sleep 1
done
sudo journalctl -u fambox-dashboard -n 20 --no-pager -o cat
fail "the dashboard did not answer its health check"
