#!/bin/bash
# Double-click to copy your local Ledgerlight data into the Render cloud database.
cd "$(dirname "$0")" || exit 1
echo "== Copy Ledgerlight data to the cloud =="
echo
echo "Before you continue, close the Terminal window that is running Ledgerlight locally,"
echo "so the local worker stops (otherwise both copies collect data and send emails)."
echo
if [ ! -d .venv ]; then echo "Run outreach-test-setup.command once first (it creates .venv)."; read -r -p "Press Return to close."; exit 1; fi
source .venv/bin/activate
pip install -q -r requirements.txt
echo "In Render: open ledgerlight-db → Connect → copy the 'External Database URL'."
read -r -s -p "Paste it here and press Return (it won't show): " URL; echo
python -m scripts.migrate_to_postgres "$URL"
echo
read -r -p "Press Return to close."
