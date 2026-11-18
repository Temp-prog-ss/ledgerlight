#!/bin/bash
# Double-click to: install, test, create your console login + Gmail sending mailbox,
# send a test email to your own addresses, then open the outreach console.
cd "$(dirname "$0")" || exit 1
clear
echo "=================================================="
echo "  Ledgerlight outreach: test setup"
echo "=================================================="

pause_exit() { echo; read -r -p "Press Enter to close this window." _; exit "${1:-1}"; }

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
  echo "Python 3.10 or newer is needed. Opening the download page: install it, then double-click this file again."
  open "https://www.python.org/downloads/macos/"
  pause_exit
fi

echo; echo "Installing (first time takes a minute)…"
[ -d .venv ] || python3 -m venv .venv || pause_exit
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip >/dev/null 2>&1
python -m pip install -q -r requirements.txt || { echo "Install failed (see above)."; pause_exit; }
echo "Running the test suite…"
python -m pytest -q -rf > test-report.txt 2>&1
TEST_STATUS=$?
tail -1 test-report.txt
if [ "$TEST_STATUS" -ne 0 ]; then
  grep -E "^FAILED" test-report.txt
  echo "Tests failed, so stopping here. The full report is in test-report.txt (Claude can read it)."
  pause_exit
fi

current() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2-; }
ask() { # ask KEY "Question" [secret]
  local key="$1" q="$2" secret="$3" cur val
  cur="$(current "$key")"
  if [ -n "$cur" ]; then
    if [ -n "$secret" ]; then q="$q [already set; Enter to keep]"; else q="$q [$cur]"; fi
  fi
  if [ -n "$secret" ]; then read -r -s -p "$q: " val; echo; else read -r -p "$q: " val; fi
  [ -z "$val" ] && val="$cur"
  if [ -z "$val" ]; then echo "  This one is needed."; ask "$@"; return; fi
  printf '%s' "$val" | python scripts/set_env.py "$key"
  export "$key=$val"
}

echo
echo "A few details. Typed answers are saved only in this folder's private .env file."
echo
ask OUTREACH_FROM "Gmail or Workspace address to send FROM"
ask OUTREACH_FROM_NAME "Sender name to show (e.g. Sam from SiteSentinel)"
echo
echo "Next you need a Google APP password for that address (not your normal password)."
echo "Opening the page now: create one called 'Ledgerlight', copy the 16 letters."
echo "(No option there? Turn on 2-Step Verification first, or ask your Workspace admin.)"
open "https://myaccount.google.com/apppasswords"
ask OUTREACH_PW_MAIN "Paste the app password (hidden as you type)" secret
echo
ask OUTREACH_TEST_RECIPIENTS "Your OWN addresses to send the test to (comma-separated)"
ask OUTREACH_COMPANY_NAME "Business name for the email footer"
ask OUTREACH_POSTAL_ADDRESS "Business postal address for the footer"
ask LL_STAFF_EMAIL "Your email for logging in to the console"
if ! python -c "import sys,os;sys.path.insert(0,'.');from app import db;db.init_db();sys.exit(0 if db.one('SELECT id FROM users WHERE email=?',(os.environ['LL_STAFF_EMAIL'].lower(),)) else 1)" 2>/dev/null; then
  read -r -s -p "Choose a console password (10+ characters, hidden): " LL_STAFF_PASSWORD; echo
  export LL_STAFF_PASSWORD
fi
printf 'false' | python scripts/set_env.py LL_DRY_RUN_DELIVERY
printf 'http://localhost:8000' | python scripts/set_env.py LL_PUBLIC_BASE_URL
export LL_DRY_RUN_DELIVERY=false LL_PUBLIC_BASE_URL=http://localhost:8000

python -m scripts.outreach_test || pause_exit
unset LL_STAFF_PASSWORD

echo
echo "Starting the console at http://localhost:8000/outreach (keep this window open)…"
python -m app.worker > worker.log 2>&1 &
WORKER=$!
trap 'kill $WORKER 2>/dev/null' EXIT
( sleep 3; open "http://localhost:8000/outreach" ) &
python -m uvicorn app.web:create_app --factory --port 8000 --log-level warning
