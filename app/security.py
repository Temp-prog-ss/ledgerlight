"""Passwords (scrypt), session tokens (JWT) and a small in-memory rate limiter."""
import base64
import hashlib
import hmac
import os
import time
from collections import defaultdict, deque

import jwt

from app import config

def hash_password(pw):
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw, stored):
    try:
        _, salt, dk = stored.split("$")
        test = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(test, base64.b64decode(dk))
    except Exception:
        return False


def issue_token(user, org):
    now = int(time.time())
    return jwt.encode({"sub": user["id"], "org": org["id"], "brand": org["brand"], "role": user["role"],
                       "iat": now, "exp": now + config.settings.token_ttl_hours * 3600},
                      config.settings.secret_key, algorithm="HS256")


def read_token(token):
    return jwt.decode(token, config.settings.secret_key, algorithms=["HS256"])


def is_staff(user):
    """Staff is a database role, set by hand with scripts/make_staff.py. Never inferred from an email address."""
    return user.get("role") == "staff"


class Throttle:
    """At most `limit` hits per key per `window` seconds."""

    def __init__(self, limit=10, window=60):
        self.limit, self.window, self.hits = limit, window, defaultdict(deque)

    def allow(self, key):
        t = time.monotonic()
        q = self.hits[key]
        while q and t - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(t)
        return True
