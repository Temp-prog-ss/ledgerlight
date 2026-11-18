"""Set KEY=value in .env, reading the value from stdin (so secrets never appear in the process list).

    printf '%s' "$VALUE" | python scripts/set_env.py KEY
"""
import re
import sys
from pathlib import Path

key = sys.argv[1]
value = sys.stdin.read().strip()
p = Path(__file__).resolve().parents[1] / ".env"
text = p.read_text() if p.exists() else ""
line = f"{key}={value}"
if re.search(rf"(?m)^#?\s*{re.escape(key)}=.*$", text):
    text = re.sub(rf"(?m)^#?\s*{re.escape(key)}=.*$", lambda m: line, text, count=1)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"
p.write_text(text)
p.chmod(0o600)
