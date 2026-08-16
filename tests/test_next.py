"""What to work on, and why.

The one place Friday offers an opinion rather than a report, and the front half
of the conductor idea: being handed six lists is not useful, being told which
one thing to start with is.

The ranking is deliberately boring and explainable, and no model decides it. A
model would be more fluent and less predictable, and the entire value here is
that you can disagree with the reason.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import conversation as C, feeds  # noqa: E402
from friday.conversation import Friday, classify  # noqa: E402


def _friday(waiting=(), broken=False, unpushed=False, tickets=()):
    f = Friday()
    f.announce = lambda *a, **k: None
    f._waiting = lambda: [
        {"label": lbl, "question": q, "mtime": time.time() - 600}
        for lbl, q in waiting]

    class _GH:
        def poll(self):
            return ([{"text": "GitHub: 2 workflows failing in me/app (nightly).",
                      "urgency": 1}] if broken else [])

    class _Git:
        def poll(self):
            return ([{"text": "api has 3 commits not pushed anywhere.",
                      "urgency": 2}] if unpushed else [])

    feeds.GitHubFeed, feeds.GitFeed = _GH, _Git

    class _Tracker:
        name = "jira"

        def ready(self):
            return True

        def my_issues(self, n=5):
            return [{"key": k, "summary": t} for k, t in tickets]

    # Patched at the seam, not on Friday: suggesting what to work on reads
    # EVERY tracker now, because `_tracker()` deliberately returns nothing when
    # two are connected and you have not said where things get filed.
    from friday import trackers
    trackers.available = lambda: ([_Tracker()] if tickets else [])
    f._tracker = lambda want="": _Tracker() if tickets else None
    from friday import connectors
    real = connectors.get
    connectors.get = lambda n: (type("X", (), {"ready": lambda s: broken})()
                                if n == "github" else real(n))
    return f


def test_the_words_are_understood():
    for said in ("what should I work on", "what next", "where should I start",
                 "what should I be doing"):
        assert classify(said)[0] == "next", said


def test_an_agent_waiting_on_you_comes_first():
    """Your answer is the only thing that unblocks it, and it is blocked right
    now. Everything else can wait its turn."""
    f = _friday(waiting=[("api", "Should I force-push?")], broken=True,
                tickets=[("PROJ-1", "a task")])
    r = f.handle("what should I work on?")
    assert "api" in r["reply"], r["reply"]
    assert "force-push" in r["reply"], r["reply"]


def test_a_broken_build_beats_a_new_task():
    """It is blocking everybody including you; a ticket is blocking nobody."""
    f = _friday(broken=True, tickets=[("PROJ-1", "add a setting")])
    r = f.handle("what should I work on?")
    assert "build" in r["reply"].lower(), r["reply"]


def test_a_ticket_is_offered_when_nothing_is_on_fire():
    f = _friday(tickets=[("PROJ-7", "the login bug")])
    r = f.handle("what should I work on?")
    assert "PROJ-7" in r["reply"], r["reply"]
    assert "login bug" in r["reply"], r["reply"]


def test_it_says_WHY_not_just_what():
    """An instruction you cannot argue with is one you will not follow."""
    f = _friday(waiting=[("api", "Should I drop the old parser?")])
    r = f.handle("what should I work on?")
    assert "drop the old parser" in r["reply"], "gave no reason"


def test_it_tells_you_what_comes_after():
    f = _friday(waiting=[("api", "which one?")], broken=True,
                tickets=[("PROJ-1", "a task")])
    r = f.handle("what should I work on?")
    assert "After that" in r["reply"], r["reply"]


def test_an_item_that_cannot_be_named_is_not_offered():
    """A ticket with no key and no title used to read as "start ."."""
    f = _friday(tickets=[("", "")])
    r = f.handle("what should I work on?")
    assert "start ." not in r["reply"], r["reply"]
    assert r["reply"].strip(), "said nothing at all"


def test_a_second_tracker_does_not_silence_the_suggestion():
    """`_tracker()` returns nothing when two are connected and you have not
    said where tickets get FILED. Reading has no such constraint, and using
    that here meant connecting a second tracker quietly stopped Friday ever
    proposing work."""
    from friday import trackers

    class _T:
        name = "jira"

        def ready(self):
            return True

        def my_issues(self, n=5):
            return [{"key": "PROJ-3", "summary": "the thing"}]

    f = _friday()
    trackers.available = lambda: [_T(), _T()]
    f._tracker = lambda want="": None       # genuinely ambiguous
    r = f.handle("what should I work on?")
    assert "PROJ-3" in r["reply"], r["reply"]


def test_an_empty_day_is_not_reported_as_a_broken_one():
    """Nothing to do and nothing connected look identical from here, and only
    one of them is good news."""
    f = _friday()
    r = f.handle("what should I work on?")
    low = r["reply"].lower()
    assert "nothing is waiting" in low, r["reply"]
    assert "connected" in low, "did not offer the other explanation"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  next: an opinion you can argue with, ranked by who is blocked")
