"""Give an existing user staff (admin) access. Run on the server only:

    python -m scripts.make_staff someone@yourcompany.co.uk
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m scripts.make_staff EMAIL")
    db.init_db()
    n = db.execute("UPDATE users SET role='staff' WHERE email=?", (sys.argv[1].lower().strip(),))
    db.audit("cli", "make_staff", {"email": sys.argv[1], "updated": n})
    print("Done." if n else "No user with that email. They must sign up first.")
