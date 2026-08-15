"""How much Friday is allowed to say, and what happens to the rest.

Three things spoke unprompted and each kept its own counter: the feeds capped
themselves at three a round and fifteen an hour, the Slack inbox at three a
round, and the watchtower at nothing at all, despite being the highest-volume
source of the three. A busy minute therefore produced three separate "and N more"
notes in the same second, each about a different N.

Worse, suppressed items were thrown away, while the message announcing the
suppression said "say what did I miss for the list". That list did not exist:
`_missed()` replays what was ANNOUNCED, so anything held back was already gone.
Quiet mode was, in effect, a delete key.

So: one budget, one queue, and nothing is destroyed. Alertmanager's rule, which
is the right one, is that a silence suppresses the NOTIFICATION and never the
alert.

The shape is a token bucket rather than a fixed hourly window. A window has a
boundary artefact: something starting at 11:59 gets a fresh allowance at 12:00,
and a burst at 12:01 is treated exactly like a slow trickle. A bucket gives a
genuinely busy moment room and then throttles to a sustained rate, which is what
you want from something that talks.

Things that need YOU are exempt. Rationing an agent that cannot continue without
an answer is rationing the only category that was worth interrupting for.
"""

import threading
import time

# Sustained rate and burst. Four an hour is what voicebridge's own attention
# engine settled on after tuning; fifteen was this dispatcher's guess.
PER_HOUR = 6.0
BURST = 4
NEEDS_YOU = 0        # urgency 0 is exempt: it is the reason this exists


class Budget:
    """A token bucket, plus the things it decided not to say."""

    def __init__(self, per_hour: float = PER_HOUR, burst: int = BURST):
        self.per_hour = per_hour
        self.burst = burst
        self._tokens = float(burst)
        self._at = time.time()
        self._held = []          # [(when, text, source)]
        self._lock = threading.Lock()

    # ---- spending ---------------------------------------------------------
    def _refill(self) -> None:
        now = time.time()
        gained = (now - self._at) * (self.per_hour / 3600.0)
        self._tokens = min(float(self.burst), self._tokens + gained)
        self._at = now

    def allow(self, urgency: int = 1) -> bool:
        """Whether this may be said now. Urgent things always may."""
        if urgency <= NEEDS_YOU:
            return True
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def spare(self) -> int:
        with self._lock:
            self._refill()
            return int(self._tokens)

    # ---- holding ----------------------------------------------------------
    def hold(self, text: str, source: str = "") -> None:
        """Keep something that was not said, so it can be asked for later.

        This is the difference between a silence and a delete. The most recent
        are kept when there are too many, because an hour-old note about a
        finished build helps nobody."""
        if not text:
            return
        with self._lock:
            self._held.append((time.time(), text, source))
            if len(self._held) > 60:
                self._held = self._held[-60:]

    def held(self, since: float = 0.0, clear: bool = True) -> list:
        """Everything held back, oldest first. Clears it by default: once you
        have been told, it is no longer missed."""
        with self._lock:
            rows = [r for r in self._held if r[0] > since]
            if clear:
                self._held = [r for r in self._held if r[0] <= since]
            return rows

    def waiting(self) -> int:
        with self._lock:
            return len(self._held)
