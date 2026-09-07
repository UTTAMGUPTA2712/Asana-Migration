"""A small blocking token-bucket limiter shared by every Asana call.

Every request path in the app goes through ``RateLimiter.acquire()`` before
hitting the network. That's the mechanism that makes the whole import
"go slowly": no matter how many jobs are queued or how many browser tabs are
polling, only ``rate_limit_per_minute`` requests actually leave the process
each minute, spaced evenly rather than sent in bursts.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, requests_per_minute: int, burst: int | None = None):
        self.set_rate(requests_per_minute, burst)
        self._lock = threading.Lock()
        self._tokens = float(self.burst)
        self._last_refill = time.monotonic()
        # If the API tells us to back off (429 + Retry-After), every caller
        # should honor it, not just the one that got the 429.
        self._paused_until = 0.0

    def set_rate(self, requests_per_minute: int, burst: int | None = None) -> None:
        self.rate_per_second = max(requests_per_minute, 1) / 60.0
        self.burst = burst if burst is not None else max(1, min(5, requests_per_minute))

    def pause_for(self, seconds: float) -> None:
        with self._lock:
            self._paused_until = max(self._paused_until, time.monotonic() + seconds)

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                wait_for_pause = max(0.0, self._paused_until - now)
                if wait_for_pause <= 0:
                    elapsed = now - self._last_refill
                    self._last_refill = now
                    self._tokens = min(self.burst, self._tokens + elapsed * self.rate_per_second)
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return
                    wait = (1.0 - self._tokens) / self.rate_per_second
                else:
                    wait = wait_for_pause
            time.sleep(min(wait, 1.0))
