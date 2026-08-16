"""What Friday works out about you, and the three limits on it.

The pitch said the product is the attention model, and that knowing when NOT to
speak is the whole difficulty. What was built is a good rule table, and rules are
fixed: Friday interrupted you about a repo you stopped caring about in March
exactly as eagerly on day ninety as on day one, and the only lever was muting the
whole source, which is a sledgehammer.

Almost everything here tests a limit rather than the learning, because the
learning is the easy part and the limits are what make it something you would
leave switched on.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import learn  # noqa: E402
from friday.conversation import Friday, classify  # noqa: E402


def _ignored(key, times=10):
    learn.forget(key)
    for _ in range(times):
        learn.saw(key)


def _wanted(key, times=8):
    learn.forget(key)
    for _ in range(times):
        learn.saw(key)
        learn.acted(key)


# ---- the three limits ------------------------------------------------------
def test_it_can_never_silence_a_blocked_agent():
    """An agent that cannot continue without you is not a preference. If Friday
    ever learns its way out of mentioning one, the feature has eaten the
    product."""
    _ignored("blocked:api", times=50)
    assert learn.score("blocked:api") == -1.0
    assert learn.adjust(0, "blocked:api") == 0


def test_it_can_only_ever_make_things_quieter():
    """Learning something UP means Friday deciding on its own to interrupt more,
    which is the direction nobody wants a mistake in."""
    _wanted("spoke:api", times=40)
    assert learn.score("spoke:api") == 1.0
    assert learn.adjust(1, "spoke:api") == 1
    assert learn.adjust(2, "spoke:api") == 2


def test_it_defers_rather_than_deletes():
    """A low score costs an item its place in the budget, so it waits for "what
    did I miss". A learned model that silently discards is one you cannot trust
    after the first thing you never heard about."""
    _ignored("spoke:noisy")
    assert learn.adjust(1, "spoke:noisy") == 2      # a tier, not a bin
    assert learn.adjust(2, "spoke:noisy") == 2      # and no further


# ---- when it forms an opinion at all ---------------------------------------
def test_two_ignored_notifications_is_not_evidence():
    """It is Tuesday."""
    _ignored("spoke:new", times=2)
    assert learn.score("spoke:new") == 0.0
    assert learn.adjust(1, "spoke:new") == 1


def test_it_forms_one_once_there_is_something_to_go_on():
    _ignored("spoke:old")
    assert learn.score("spoke:old") < -0.5


def test_acting_counts_for_more_than_not_acting():
    """Acting is deliberate. Not acting is mostly noise: you were in a meeting,
    the screen was off, you saw it and it was fine."""
    learn.forget("spoke:mixed")
    for _ in range(6):
        learn.saw("spoke:mixed")
    for _ in range(2):
        learn.acted("spoke:mixed")
    assert learn.score("spoke:mixed") > -0.5, learn.score("spoke:mixed")


def test_old_habits_fade():
    """A fortnight on you have probably changed what you are working on, which
    is exactly when a learned preference goes from helpful to baffling."""
    _ignored("spoke:march")
    data = learn._load()
    data["spoke:march"]["at"] = time.time() - learn.HALF_LIFE * 4
    learn._save(data)
    assert learn.score("spoke:march") == 0.0, "still holding a grudge"


# ---- muting is a deliberate signal -----------------------------------------
def test_muting_teaches_more_than_ignoring_does():
    """It is the only signal you give on purpose."""
    learn.forget("spoke:loud")
    learn.never_again("spoke:loud")
    assert learn.score("spoke:loud") < -0.5


# ---- it has to be answerable -----------------------------------------------
def test_it_explains_itself_in_counts():
    """The answer to "why didn't you tell me" has to be something you can
    disagree with, which a number on its own is not."""
    _ignored("spoke:jobhunt", times=9)
    said = learn.why("spoke:jobhunt")
    assert "9" in said and "0" in said, said


def test_you_can_ask_what_it_learned():
    f = Friday()
    f.announce = lambda *a, **k: None
    assert classify("what have you learned")[0] == "learned"
    _ignored("spoke:jobhunt", times=9)
    r = f.handle("what have you learned")
    assert "jobhunt" in r["reply"], r["reply"]
    assert "quieter, never louder" in r["reply"], r["reply"]


def test_you_can_ask_why_it_is_quiet_about_one_thing():
    f = Friday()
    f.announce = lambda *a, **k: None
    _ignored("spoke:jobhunt", times=9)
    r = f.handle("why are you quiet about jobhunt")
    assert "9" in r["reply"], r["reply"]
    assert "forget" in r["reply"], "did not say how to undo it"


def test_you_can_undo_it():
    """The day it decides wrongly that you do not care about something, the only
    acceptable answer is a way to say otherwise."""
    f = Friday()
    f.announce = lambda *a, **k: None
    _ignored("spoke:jobhunt", times=9)
    f.handle("forget what you learned about jobhunt")
    assert learn.score("spoke:jobhunt") == 0.0
    assert learn.adjust(1, "spoke:jobhunt") == 1


def test_it_says_plainly_when_it_has_learned_nothing():
    learn.forget()
    f = Friday()
    f.announce = lambda *a, **k: None
    r = f.handle("what have you learned")
    assert "Nothing yet" in r["reply"], r["reply"]


# ---- the signal has to be real ---------------------------------------------
def test_working_in_a_session_all_morning_is_not_evidence():
    """Replying to a session you were already in does not show Friday's
    notifications are useful, and counting it would teach Friday that
    everything it says lands."""
    learn.forget()
    f = Friday()
    f.announce = lambda *a, **k: None
    f._acted_on("api")                    # never mentioned
    assert learn.score("spoke:api") == 0.0
    assert learn._load() == {}, learn._load()


def test_acting_shortly_after_being_told_does_count():
    learn.forget()
    f = Friday()
    said = []
    f.announce = lambda t, items=None, **k: said.append(t)
    Friday.announce(f, "api says something", items=[{"sid": "s1", "label": "api",
                                                     "kind": "spoke"}])
    f._acted_on("api")
    row = learn._load().get("spoke:api")
    assert row and row["acted"] > 0, learn._load()


def test_a_thing_mentioned_long_ago_does_not_count():
    """Without the clock, any later mention of the same session would count and
    everything would look interesting."""
    learn.forget()
    f = Friday()
    f.announce = lambda *a, **k: None
    f._told = {"spoke:api": time.time() - f.ACTED_WITHIN - 60}
    f._acted_on("api")
    assert not learn._load().get("spoke:api"), learn._load()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    learn.forget()
    print("ok  learning: quieter only, never about a blocked agent, always undoable")
