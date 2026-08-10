"""Bringing a Slack message to you with something you can do about it.

The failure modes are all about trust rather than mechanics: reciting the backlog
on startup, reading your own messages back to you, saying the same thing twice,
or offering to do something Friday cannot do. The last one is the worst, because
you would rely on it.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import inbox as inbox_mod  # noqa: E402


class _Slack:
    """A Slack that answers from a script."""
    def __init__(self, rows):
        self.rows = rows            # channel id -> [message]
        self.calls = []

    def ready(self):
        return True

    def channels(self):
        return [{"id": cid, "name": "moonshot"} for cid in self.rows]

    def read_channel(self, cid, limit=8, oldest=0, latest=0):
        self.calls.append((cid, oldest))
        return [r for r in self.rows.get(cid, [])
                if not oldest or (r.get("when") or 0) > float(oldest)]

    def _call(self, method, **kw):
        return {"ok": True, "user_id": "U_ME"}


def _inbox(slack):
    said = []
    box = inbox_mod.Inbox(lambda text, items=None: said.append(text))
    inbox_mod.connectors = type("C", (), {"get": staticmethod(lambda n: slack)})
    inbox_mod.engine = type("E", (), {
        "attention": type("A", (), {"is_quiet": staticmethod(lambda: False)})})
    return box, said


def test_the_backlog_is_not_read_out_on_startup():
    """Everything already in Slack when Friday starts is not news, and reading
    it out is how the feature gets turned off in its first minute."""
    now = time.time()
    slack = _Slack({"C1": [{"who": "U_MAN", "text": "old thing", "when": now - 9}]})
    box, said = _inbox(slack)
    box._tick(quiet=True)
    assert said == [], said


def test_a_new_message_is_reported_once_with_who_and_where():
    now = time.time()
    rows = {"C1": [{"who": "U_MAN", "text": "can you review the sheet?",
                    "when": now - 50}]}
    slack = _Slack(rows)
    box, said = _inbox(slack)
    box._tick(quiet=True)                     # establishes the baseline
    rows["C1"].append({"who": "U_MAN", "text": "also, are you free Thursday?",
                       "when": now})
    box._tick()
    assert len(said) == 1, said
    assert "U_MAN" in said[0] and "#moonshot" in said[0], said[0]
    assert "Thursday" in said[0], said[0]
    box._tick()
    assert len(said) == 1, "said the same message twice"


def test_your_own_messages_are_not_reported_back_to_you():
    now = time.time()
    rows = {"C1": [{"who": "U_MAN", "text": "hi", "when": now - 50}]}
    slack = _Slack(rows)
    box, said = _inbox(slack)
    box._tick(quiet=True)
    rows["C1"].append({"who": "U_ME", "text": "on it", "when": now})
    box._tick()
    assert said == [], said


def test_a_message_about_a_time_offers_the_time():
    """A meeting request is answered with a time, not a sentence, so the offer
    has to be different."""
    now = time.time()
    rows = {"C1": [{"who": "U_MAN", "text": "hello", "when": now - 50}]}
    slack = _Slack(rows)
    box, said = _inbox(slack)
    box._tick(quiet=True)
    rows["C1"].append({"who": "U_MAN", "text": "want to jump on a call at 4?",
                       "when": now})
    box._tick()
    assert "time" in said[0].lower(), said[0]


def test_friday_never_offers_to_send_what_it_cannot_send():
    """The app holds read scopes only, deliberately. Offering to reply and then
    not sending would be the worst of both, and you would rely on it."""
    now = time.time()
    rows = {"C1": [{"who": "U_MAN", "text": "hello", "when": now - 50}]}
    slack = _Slack(rows)
    box, said = _inbox(slack)
    box._tick(quiet=True)
    rows["C1"].append({"who": "U_MAN", "text": "please confirm", "when": now})
    box._tick()
    low = said[0].lower()
    assert "draft" in low, said[0]
    assert "i'll send" not in low and "i will send" not in low, said[0]


def test_a_flood_is_capped_and_says_so():
    """Twelve messages at once must not become twelve announcements, and the
    ones held back must be admitted to rather than dropped silently."""
    now = time.time()
    rows = {"C1": [{"who": "U_MAN", "text": "first", "when": now - 99}]}
    slack = _Slack(rows)
    box, said = _inbox(slack)
    box._tick(quiet=True)
    for i in range(12):
        rows["C1"].append({"who": "U_MAN", "text": f"msg {i}", "when": now + i})
    box._tick()
    assert len(said) == inbox_mod.MAX_PER_ROUND + 1, said
    assert "more Slack message" in said[-1], said[-1]


def test_quiet_covers_slack_too():
    now = time.time()
    rows = {"C1": [{"who": "U_MAN", "text": "hello", "when": now - 50}]}
    slack = _Slack(rows)
    said = []
    box = inbox_mod.Inbox(lambda text, items=None: said.append(text),
                          hushed=lambda: True)
    inbox_mod.connectors = type("C", (), {"get": staticmethod(lambda n: slack)})
    rows["C1"].append({"who": "U_MAN", "text": "urgent", "when": now})
    box._tick()
    assert said == [], said


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  inbox: yours only, once each, and never a promise it can't keep")
