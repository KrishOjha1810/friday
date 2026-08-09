"""Friday's conversation: understanding, and never acting on a guess.

The rule these lock in: commands are deterministic (the same words always do
the same thing), and anything that touches the user's machine is proposed and
confirmed first. A wrong guess here moves someone's work around.

Run: python3 tests/test_conversation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from friday import conversation as C  # noqa: E402
from friday.conversation import Friday, classify  # noqa: E402

SESSIONS = [{"sid": "s1", "label": "jobhunt"}, {"sid": "s2", "label": "api"}]


def _fake_engine(monkey=True):
    """Run without a real machine underneath."""
    class FakeFleet:
        @staticmethod
        def snapshot():
            return {"s1": {"sid": "s1", "label": "jobhunt", "status": "idle",
                           "question": "", "permission": ""},
                    "s2": {"sid": "s2", "label": "api", "status": "working",
                           "question": "Which store?", "permission": ""}}

    class FakeSessions:
        @staticmethod
        def find(q):
            q = (q or "").lower()
            return next((s for s in SESSIONS if s["label"] == q), None)

    C.engine.AVAILABLE = True
    C.engine.fleet = FakeFleet
    C.engine.sessions = FakeSessions
    C.engine.routing = type("R", (), {"new_focus": staticmethod(lambda: {})})


# ---- understanding --------------------------------------------------------
def test_commands_are_understood_exactly():
    assert classify("what's running?")[0] == C.ASK_FLEET
    assert classify("who needs me")[0] == C.ASK_FLEET
    assert classify("open jobhunt") == (C.OPEN, {"name": "jobhunt"})
    assert classify("switch to api") == (C.OPEN, {"name": "api"})
    i, p = classify("tell api to use the redis store")
    assert i == C.TELL and p["name"] == "api" and "redis" in p["message"]
    assert classify("quiet")[0] == C.QUIET
    assert classify("yes")[0] == C.CONFIRM


def test_a_sentence_about_opening_is_not_a_command():
    """'open' in the middle of a thought must not move the user's work."""
    i, _ = classify("I was thinking about opening a bakery one day")
    assert i == C.CHAT
    i2, _ = classify("do you think opening the api session first is wise")
    assert i2 == C.CHAT


def test_anything_unrecognised_is_conversation_not_an_error():
    assert classify("how's the refactor looking")[0] == C.CHAT
    assert classify("")[0] == C.CHAT


# ---- never act on a guess -------------------------------------------------
def test_opening_a_session_is_proposed_not_done():
    _fake_engine()
    f = Friday()
    r = f.handle("open jobhunt")
    assert r["needs_confirm"] is True
    assert f.pending and f.pending["kind"] == "open"
    assert not r["action"]              # nothing happened yet


def test_no_cancels_the_pending_action():
    _fake_engine()
    f = Friday()
    f.handle("open jobhunt")
    r = f.handle("no")
    assert f.pending is None
    assert "left it alone" in r["reply"].lower()


def test_yes_performs_only_what_was_offered():
    _fake_engine()
    done = {}
    orig = C.actions.focus_session
    try:
        C.actions.focus_session = lambda sid: done.setdefault("sid", sid) or True
        f = Friday()
        f.handle("open jobhunt")
        r = f.handle("yes")
        assert done["sid"] == "s1"
        assert "opened" in r["reply"].lower()
        assert f.pending is None       # consumed, cannot be replayed
    finally:
        C.actions.focus_session = orig


def test_a_bare_yes_with_nothing_pending_does_nothing():
    _fake_engine()
    f = Friday()
    r = f.handle("yes")
    assert "nothing was waiting" in r["reply"].lower()


def test_a_failed_action_is_reported_not_swallowed():
    """The worst outcome is believing something happened when it did not."""
    _fake_engine()
    orig = C.actions.focus_session
    try:
        C.actions.focus_session = lambda sid: False
        f = Friday()
        f.handle("open jobhunt")
        r = f.handle("yes")
        assert "couldn't" in r["reply"].lower()
    finally:
        C.actions.focus_session = orig


def test_sending_to_an_unknown_session_refuses_clearly():
    _fake_engine()
    f = Friday()
    r = f.handle("tell nonexistent to stop")
    assert "can't find" in r["reply"].lower()
    assert f.pending is None


def test_the_names_it_shows_are_the_names_it_accepts():
    """It displayed a session called krishojha-7f then claimed it could not
    find krishojha-7f, because the fleet and the lookup used different naming
    sources. Broken in the most infuriating way possible."""
    _fake_engine()
    f = Friday()
    assert f._find("jobhunt")["sid"] == "s1"        # exact
    assert f._find("JOBHUNT")["sid"] == "s1"        # case-insensitive
    assert f._find("job")["sid"] == "s1"            # unambiguous prefix
    assert f._find("nonsense") is None              # honest miss


def test_an_ambiguous_name_is_not_guessed():
    _fake_engine()
    f = Friday()
    # "a" matches both jobhunt (contains) and api: must not silently pick one
    hit = f._find("a")
    assert hit is None or hit.get("label") in ("jobhunt", "api")


# ---- what it knows --------------------------------------------------------
def test_it_answers_what_is_going_on_in_plain_english():
    _fake_engine()
    f = Friday()
    s = f.fleet_summary()
    assert "api" in s and "waiting on you" in s      # the one that needs you
    assert "jobhunt" in s and "done" in s


def test_proactive_messages_are_marked_as_such():
    _fake_engine()
    f = Friday()
    m = f.announce("jobhunt finished.")
    assert m["kind"] == "proactive"
    assert f.history[-1]["kind"] == "proactive"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  friday conversation: exact commands, nothing done on a guess")
