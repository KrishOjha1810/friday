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


# ---- a day it cannot read is not today -------------------------------------
def test_a_date_it_cannot_read_is_refused_not_assumed():
    """"The 31st of February at 4" and "yesterday at 4" both became TODAY at
    16:00. The parser found no weekday word, fell through to today, and read
    the 4. A meeting in the wrong slot is worse than one you had to type
    yourself, and this one looks exactly like a correct answer."""
    import datetime as _dt
    from friday import when as _w
    now = _dt.datetime(2026, 8, 17, 9, 0)          # a Monday morning
    for said in ("put it in for the 31st of February at 4",
                 "put it in for yesterday at 4",
                 "put it in for last Tuesday at 4",
                 "put it in for the 14th at 4",
                 "put it in for March at 4"):
        stamp, reads = _w.moment(said, now)
        assert stamp == 0, (said, reads)


def test_a_time_today_that_has_gone_is_a_mistake_not_tomorrow():
    """"Today at 9" said at six in the evening means you misspoke. Rolling it
    forward books tomorrow morning under the word "today"."""
    import datetime as _dt
    from friday import when as _w
    evening = _dt.datetime(2026, 8, 17, 18, 0)
    assert _w.moment("today at 9am", evening)[0] == 0
    # A bare time with no day is the one that legitimately rolls forward: "at
    # 4" said in the evening means tomorrow afternoon.
    assert _w.moment("at 4", evening)[0] > 0


def test_the_ordinary_ways_of_saying_it_still_work():
    """The guard is worthless if it costs the feature."""
    import datetime as _dt
    from friday import when as _w
    morning = _dt.datetime(2026, 8, 17, 9, 0)      # Monday
    for said, want in (("Thursday at 4", "Thursday"),
                       ("tomorrow at 10:30am", "Tuesday"),
                       ("friday 2pm", "Friday"),
                       ("tonight at 8", "Monday"),
                       ("today at 9am", "Monday")):
        stamp, reads = _w.moment(said, morning)
        assert stamp and reads.startswith(want), (said, reads)
    # "Tonight at 8" is not eight in the morning.
    assert "20:00" in _w.moment("tonight at 8", morning)[1]


def test_it_says_which_way_the_date_failed():
    """"When?" in answer to "yesterday at 4" is baffling: you did say when, and
    Friday read it and refused. The two cases need different things from you."""
    f = Friday()
    f.announce = lambda *a, **k: None
    vague = f.handle("schedule a meeting")["reply"]
    unusable = f.handle("put it in for yesterday at 4")["reply"]
    assert "When?" in vague, vague
    assert "When?" not in unusable, unusable
    assert "gone" in unusable or "understand" in unusable, unusable


def test_the_abbreviations_everybody_uses():
    """"thurs at 4" named no day the parser knew, fell through to today, and
    Friday confirmed "Thursday" as this afternoon: a confident answer, three
    days early, which is the failure this module says is worse than making you
    type it."""
    import datetime as _dt
    from friday import when as _w
    monday = _dt.datetime(2026, 8, 17, 9, 0)
    for said, want in (("thurs at 4", "Thursday"), ("thu at 4", "Thursday"),
                       ("fri 2pm", "Friday"), ("wed at 3", "Wednesday"),
                       ("tues at 5", "Tuesday"), ("weds 4pm", "Wednesday"),
                       ("sat at 11am", "Saturday"), ("sun 6pm", "Sunday")):
        stamp, reads = _w.moment(said, monday)
        assert stamp and reads.startswith(want), (said, reads)


def test_an_abbreviation_inside_an_ordinary_word_is_not_a_day():
    """Several of the abbreviations are ordinary English words. Matching them
    bare booked "sat down at 4" for Saturday and "the sun is out at 4" for
    Sunday: the wrong DAY, stated as a confident confirmation. So an
    abbreviation counts only where a day actually goes."""
    import datetime as _dt
    from friday import when as _w
    monday = _dt.datetime(2026, 8, 17, 9, 0)
    for said in ("sat down at 4", "the sun is out at 4", "sunset at 4",
                 "wedding at 4", "satisfied at 4", "monitor at 4",
                 "we sat at 4", "i wed at 4", "they sat at 4"):
        _stamp, reads = _w.moment(said, monday)
        assert reads.startswith("Monday"), (said, reads)


def test_an_abbreviated_day_that_has_gone_is_still_refused():
    import datetime as _dt
    from friday import when as _w
    monday = _dt.datetime(2026, 8, 17, 9, 0)
    assert _w.moment("last thurs at 4", monday)[0] == 0


def test_a_spelling_of_tomorrow_does_not_kill_the_request():
    """"tmrw" was listed as a day word and not as a weekday, so it fell into
    the weekday lookup and raised out of an unguarded caller: the request
    thread died and the page got no reply at all."""
    import datetime as _dt
    from friday import when as _w
    monday = _dt.datetime(2026, 8, 17, 10, 0)
    stamp, reads = _w.moment("book a meeting tmrw at 4", monday)
    assert stamp and reads.startswith("Tuesday"), reads


def test_scheduling_verbs_are_their_own_confirmation():
    """"Schedule it for Thursday at 4" carries no other meeting word, so it
    needed one, did not have one, and the commonest phrasing there is fell
    through to the model."""
    for said in ("schedule it for thurs at 4", "book a meeting sat at 4",
                 "pencil me in for wed at 3", "put it in for Thursday at 4"):
        assert classify(said)[0] == "schedule", (said, classify(said))
    # And the ambiguous verbs still need one.
    assert classify("put the kettle on")[0] != "schedule"


def test_reading_a_past_day_does_not_match_an_abbreviation_bare():
    """"What did it say when I sat down" reported "Sat (15 Aug)" and searched
    the wrong day, with a label that told you it had understood."""
    import datetime as _dt
    from friday import when as _w
    monday = _dt.date(2026, 8, 17)
    for said in ("what did it say when I sat down",
                 "what happened while the sun was up",
                 "summarise the wed meeting notes"):
        assert _w.parse(said, monday)[2] == "", (said, _w.parse(said, monday))
    assert _w.parse("what was said on friday", monday)[2].startswith("Friday")
