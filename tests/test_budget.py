"""How much Friday may say, and what happens to the rest.

Three things spoke unprompted and each kept its own counter: the feeds capped at
three a round and fifteen an hour, the inbox at three a round, and the watchtower
at nothing at all despite being the loudest of the three. A busy minute produced
three separate "and N more" notes in the same second, each about a different N.

And the suppressed items were destroyed while the message about them said "say
what did I miss for the list". That list did not exist. Quiet mode was a delete
key.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import budget as budgets  # noqa: E402


def test_a_burst_is_allowed_and_then_it_throttles():
    """A token bucket, not a fixed window. A window has a boundary artefact:
    something starting at 11:59 gets a fresh allowance a minute later, and a
    burst is treated exactly like a trickle."""
    b = budgets.Budget(per_hour=6, burst=4)
    allowed = [b.allow(1) for _ in range(8)]
    assert allowed[:4] == [True] * 4, allowed
    assert allowed[4:] == [False] * 4, allowed


def test_something_that_needs_you_is_never_rationed():
    """Rationing the one category worth interrupting for defeats the point."""
    b = budgets.Budget(per_hour=6, burst=1)
    assert b.allow(1) is True
    assert b.allow(1) is False, "the bucket should be empty"
    for _ in range(10):
        assert b.allow(0) is True, "an agent blocked on you was rationed"


def test_the_allowance_returns_over_time():
    b = budgets.Budget(per_hour=3600, burst=1)   # one per second
    assert b.allow(1) is True
    assert b.allow(1) is False
    time.sleep(1.1)
    assert b.allow(1) is True, "the bucket never refilled"


def test_what_is_not_said_is_kept():
    """This is the whole point. A silence suppresses the notification, never
    the alert."""
    b = budgets.Budget()
    b.hold("api says: the migration finished", "api")
    b.hold("jobhunt says: 4 postings scored", "jobhunt")
    assert b.waiting() == 2
    rows = b.held()
    assert [r[1] for r in rows] == ["api says: the migration finished",
                                    "jobhunt says: 4 postings scored"]


def test_being_told_clears_it():
    """Once you have heard it, it is no longer missed, and repeating it every
    time you ask would be its own kind of noise."""
    b = budgets.Budget()
    b.hold("something", "x")
    assert len(b.held()) == 1
    assert b.held() == [], "it was repeated"


def test_it_does_not_hoard_forever():
    """An hour-old note about a finished build helps nobody, and an unbounded
    list is a memory leak in a process that runs all day."""
    b = budgets.Budget()
    for i in range(200):
        b.hold(f"thing {i}", "x")
    assert b.waiting() <= 60, b.waiting()
    rows = b.held()
    assert rows[-1][1] == "thing 199", "kept the oldest instead of the newest"


def test_the_three_voices_draw_on_one_allowance():
    """Separate counters meant one busy moment produced three separate
    "and N more" notes, each about a different N."""
    b = budgets.Budget(per_hour=6, burst=3)
    voices = [b.allow(1) for _ in range(3)]      # as if watchtower, inbox, feeds
    assert voices == [True, True, True]
    assert b.allow(1) is False, "a fourth voice found its own fresh budget"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  budget: one allowance, urgent exempt, nothing thrown away")
