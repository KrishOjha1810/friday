"""Putting something in the calendar.

You described this twice, in the same sentence both times: "Sam is asking for a
meet, want me to schedule a meet at 3pm". Friday could read the first half and
do nothing about the second, which is half a sentence.

A meeting in the wrong slot is worse than one you had to type yourself, so the
time parsing refuses rather than guesses, and nothing is added without you
seeing the day and time read back.
"""

import datetime
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import when  # noqa: E402
from friday.conversation import Friday, classify  # noqa: E402

SAT_MORNING = datetime.datetime(2026, 8, 15, 9, 0)


def test_the_shapes_people_actually_say():
    for said, expect in (("can we meet Thursday at 4", "Thursday 20 August at 16:00"),
                         ("tomorrow 10:30am", "Sunday 16 August at 10:30"),
                         ("monday at 9am", "Monday 17 August at 09:00"),
                         ("today at 6", "Saturday 15 August at 18:00")):
        _stamp, reads = when.moment(said, SAT_MORNING)
        assert reads == expect, (said, reads)


def test_four_means_the_afternoon():
    """Everybody arranging a meeting means 4pm. Nobody means 4am, and taking
    the literal reading would put it in overnight."""
    _s, reads = when.moment("thursday at 4", SAT_MORNING)
    assert "16:00" in reads, reads


def test_it_refuses_rather_than_guesses():
    """A meeting in the wrong slot is worse than one you typed yourself."""
    for vague in ("are you free Thursday", "sometime next quarter",
                  "let's catch up soon", ""):
        stamp, reads = when.moment(vague, SAT_MORNING)
        assert stamp == 0 and reads == "", (vague, reads)


def test_a_time_already_past_moves_to_the_next_day():
    evening = datetime.datetime(2026, 8, 15, 20, 0)
    stamp, reads = when.moment("at 9am", evening)
    assert stamp > evening.timestamp(), reads
    assert "16 August" in reads, reads


class _Cal:
    def __init__(self, ok=True):
        self.added, self.ok = [], ok

    def available(self):
        return True

    def add(self, title, start, minutes=30):
        if not self.ok:
            return {"error": "the calendar refused it"}
        self.added.append((title, start))
        return {"ok": True, "calendar": "Home"}


def _friday(cal=None):
    f = Friday()
    f.announce = lambda *a, **k: None
    f.feeds.sources["calendar"] = [cal or _Cal(), 60, 0]
    return f


def test_nothing_goes_in_without_you_hearing_the_day_and_time():
    cal = _Cal()
    f = _friday(cal)
    r = f.handle("put it in for thursday at 4")
    assert cal.added == [], "added it without asking"
    assert r["needs_confirm"] is True
    assert "16:00" in r["reply"] or "4" in r["reply"], r["reply"]


def test_saying_yes_adds_it():
    cal = _Cal()
    f = _friday(cal)
    f.handle("put it in for thursday at 4")
    r = f.handle("yes")
    assert len(cal.added) == 1, cal.added
    assert "Home" in r["reply"], r["reply"]


def test_it_is_titled_after_whoever_asked():
    """"Meeting" tells you nothing in a week's time. The person who wanted it
    is the useful part, and Friday already knows who that was."""
    cal = _Cal()
    f = _friday(cal)
    f.inbox.last = {"C1": {"where": "#eng", "who": "Sam", "channel": "C1",
                           "text": "are you free Thursday?"}}
    f.handle("put it in for thursday at 4")
    f.handle("yes")
    assert "Sam" in cal.added[0][0], cal.added


def test_a_vague_time_asks_rather_than_inventing_one():
    cal = _Cal()
    f = _friday(cal)
    r = f.handle("schedule a meeting")
    assert cal.added == []
    assert "when" in r["reply"].lower(), r["reply"]


def test_a_refusal_says_nothing_was_added():
    """"It didn't go in" and "it might have" are different, and only one is
    safe to act on."""
    cal = _Cal(ok=False)
    f = _friday(cal)
    f.handle("put it in for thursday at 4")
    r = f.handle("yes")
    assert "nothing was added" in r["reply"].lower(), r["reply"]


def test_ordinary_sentences_are_not_scheduling():
    for said in ("put the kettle on", "what is running", "put it back"):
        assert classify(said)[0] != "schedule", said


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  scheduling: refuses when unsure, and reads the time back first")
