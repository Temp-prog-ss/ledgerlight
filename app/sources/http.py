"""Shared HTTP session and a sliding-window rate limiter."""
import threading
import time
from collections import deque

import requests

USER_AGENT = "Ledgerlight/1.0 (+https://ledgerlight.example; data@ledgerlight.example)"


def session():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    adapter = requests.adapters.HTTPAdapter(max_retries=requests.adapters.Retry(
        total=4, backoff_factor=1.5, status_forcelist=(500, 502, 503, 504), allowed_methods=("GET", "POST")))
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


class RateLimiter:
    """Allow at most `limit` calls per `window` seconds (thread-safe)."""

    def __init__(self, limit, window=300, sleep=time.sleep, clock=time.monotonic):
        self.limit, self.window, self._sleep, self._clock = limit, window, sleep, clock
        self.calls = deque()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                t = self._clock()
                while self.calls and t - self.calls[0] >= self.window:
                    self.calls.popleft()
                if len(self.calls) < self.limit:
                    self.calls.append(t)
                    return
                wait = self.window - (t - self.calls[0]) + 0.01
            self._sleep(wait)
