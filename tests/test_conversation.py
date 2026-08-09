"""Friday's conversation: understanding, and never acting on a guess.

The rule these lock in: commands are deterministic (the same words always do
the same thing), and anything that touches the user's machine is proposed and
confirmed first. A wrong guess here moves someone's work around.

Run: python3 tests/test_conversation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import use_temp_config  # noqa: E402

use_temp_config()   # never touch the real ~/.friday: a test once
                    # deleted a live Slack token this way
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


# ---- tiered: act when certain, ask when guessing --------------------------
def test_a_reversible_action_just_happens():
    """TIER 0. Opening a window is instantly reversible, so asking permission
    every time is friction you would hit fifty times a day, and a confirmation
    you always say yes to has stopped being a safety mechanism."""
    _fake_engine()
    done = {}
    orig = C.actions.focus_session
    try:
        C.actions.focus_session = lambda sid: done.setdefault("sid", sid) or True
        f = Friday()
        r = f.handle("open jobhunt")
        assert r["needs_confirm"] is False      # no permission theatre
        assert done["sid"] == "s1"              # it actually happened
        assert "opened" in r["reply"].lower()
    finally:
        C.actions.focus_session = orig


def test_naming_a_session_exactly_is_your_confirmation():
    """TIER 0. You said which session; asking 'did you mean it?' adds nothing."""
    _fake_engine()
    sent = {}
    orig = C.actions.send_to_session
    try:
        C.actions.send_to_session = lambda sid, msg: sent.update(sid=sid, msg=msg) or True
        f = Friday()
        r = f.handle("tell api to use redis")
        assert r["needs_confirm"] is False
        assert sent["sid"] == "s2" and "redis" in sent["msg"]
    finally:
        C.actions.send_to_session = orig


def test_a_guessed_target_is_confirmed_first():
    """TIER 1. Friday inferred which session you meant, so it checks. Writing
    the wrong instruction into a running agent is not undoable."""
    _fake_engine()
    sent = {}
    orig = C.actions.send_to_session
    try:
        C.actions.send_to_session = lambda sid, msg: sent.update(sid=sid) or True
        f = Friday()
        r = f.handle("tell ap to use redis")     # 'ap' is a guess at 'api'
        assert r["needs_confirm"] is True
        assert not sent                          # nothing sent yet
        f.handle("yes")
        assert sent["sid"] == "s2"
    finally:
        C.actions.send_to_session = orig


def test_no_cancels_a_guessed_action():
    _fake_engine()
    sent = {}
    orig = C.actions.send_to_session
    try:
        C.actions.send_to_session = lambda sid, msg: sent.update(sid=sid) or True
        f = Friday()
        f.handle("tell ap to use redis")
        r = f.handle("no")
        assert f.pending is None and not sent
        assert "left it alone" in r["reply"].lower()
    finally:
        C.actions.send_to_session = orig


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
        r = f.handle("open jobhunt")
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


# ---- answering an agent from the thread -----------------------------------
def _with_routing():
    """Use the real routing module against the fake fleet."""
    import sys
    sys.path.insert(0, "/Users/krishojha/voicebridge")
    from vb import routing
    C.engine.routing = routing
    return routing


def test_a_bare_answer_reaches_the_agent_that_asked():
    """Friday says 'api is asking which token store', you say 'use redis', and
    it must reach api. Having to retype the session name defeats the point."""
    _fake_engine(); routing = _with_routing()
    sent = {}
    orig = C.actions.send_to_session
    try:
        C.actions.send_to_session = lambda sid, msg: sent.update(sid=sid, msg=msg) or True
        f = Friday()
        f.announce("api is asking: which token store?",
                   items=[{"sid": "s2", "label": "api", "kind": "blocked"}])
        r = f.handle("use the redis one")
        assert sent.get("sid") == "s2", "the answer never reached api"
        assert "told api" in r["reply"].lower()
    finally:
        C.actions.send_to_session = orig


def test_ordinary_conversation_is_not_mistaken_for_an_answer():
    _fake_engine(); _with_routing()
    sent = {}
    orig = C.actions.send_to_session
    try:
        C.actions.send_to_session = lambda sid, msg: sent.update(sid=sid) or True
        f = Friday()
        f.announce("api is asking: which token store?",
                   items=[{"sid": "s2", "label": "api", "kind": "blocked"}])
        f.handle("what's running?")          # a command, not an answer
        assert not sent
    finally:
        C.actions.send_to_session = orig


def test_two_waiting_agents_are_never_guessed_between():
    _fake_engine(); _with_routing()
    sent = {}
    orig = C.actions.send_to_session
    try:
        C.actions.send_to_session = lambda sid, msg: sent.update(sid=sid) or True
        f = Friday()
        f.announce("two agents need you",
                   items=[{"sid": "s1", "label": "jobhunt", "kind": "blocked"},
                          {"sid": "s2", "label": "api", "kind": "blocked"}])
        r = f.handle("use the redis one")
        assert not sent, "guessed a target instead of asking"
        assert "which" in r["reply"].lower()
    finally:
        C.actions.send_to_session = orig


def test_asking_what_one_agent_needs_sets_up_the_answer():
    """Tap a 'needs you' chip, hear the question, then just answer it. The
    reply must route without you naming the session again."""
    _fake_engine(); _with_routing()
    sent = {}
    orig = C.actions.send_to_session
    try:
        C.actions.send_to_session = lambda sid, msg: sent.update(sid=sid) or True
        f = Friday()
        r = f.handle("what does api need?")
        assert "which store" in r["reply"].lower()      # it says the question
        f.handle("use the redis one")                    # bare answer
        assert sent.get("sid") == "s2"                   # reached api
    finally:
        C.actions.send_to_session = orig


def test_an_agent_that_needs_nothing_says_so():
    _fake_engine()
    f = Friday()
    r = f.handle("what does jobhunt need?")
    assert "doesn't need anything" in r["reply"].lower()


def test_natural_phrasing_is_understood_not_echoed():
    """'Can you go to the session of voicebridge and tell him…' used to fall
    through to chat, and the model replied by rephrasing the request back at
    the user instead of doing it."""
    for t in ["Can you go to the session of voicebridge and tell him that the design looks good",
              "go to the voicebridge session and tell it we are done",
              "ask api to run the tests",
              "send a message to api that we shipped"]:
        i, p = classify(t)
        assert i == C.TELL, f"not understood: {t}"
        assert p["name"] not in ("the", "a", "it"), f"grabbed filler: {p['name']}"


def test_transcribed_noise_is_ignored_not_answered():
    """Whisper labels a door closing as [SOUND]. Answering it produced
    'I don't know what that sound is. Can you clarify?'"""
    _fake_engine()
    f = Friday()
    for noise in ["[SOUND]", "[BLANK_AUDIO]", "(music)", "  ", "..."]:
        r = f.handle(noise)
        assert r["reply"] == "", f"answered noise: {noise}"
    assert not f.history, "noise polluted the thread"


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
