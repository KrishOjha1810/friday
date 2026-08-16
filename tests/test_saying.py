"""Things a person says that used to go wrong.

Not malformed input in the fuzzing sense. These are all sentences somebody would
actually say, or stop halfway through saying, where Friday did something other
than what they meant. The worst of them send words into a running agent, which
is why they are here rather than in a list of polish.

The rule underneath most of these: when Friday is not sure, it must ask. The
failure is never "it asked me something obvious", it is "it did something and
told me it worked".
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import actions, fleetcache, plan as plans  # noqa: E402
from friday.conversation import Friday, classify  # noqa: E402

SENT = []


def _friday(*labels, asking=""):
    rows = {}
    for i, label in enumerate(labels or ("api", "web")):
        rows[f"s{i}"] = {"sid": f"s{i}", "label": label,
                         "question": asking if i == 0 else "",
                         "status": "needs" if (asking and not i) else "idle",
                         "path": "", "mtime": time.time() - i}
    fleetcache.snapshot = lambda: rows
    SENT.clear()
    actions.send_to_session = lambda sid, t: SENT.append((sid, t)) or True
    f = Friday()
    f.announce = lambda *a, **k: None
    return f


# ---- a sentence you did not finish -----------------------------------------
def test_a_half_finished_instruction_is_not_sent():
    """"tell api to" left the dangling "to" as the message and sent it. A
    sentence you stopped halfway through became a prompt in a running agent."""
    f = _friday("api")
    r = f.handle("tell api to")
    assert not SENT, SENT
    assert "What should I say to api" in r["reply"], r["reply"]


def test_a_name_with_no_message_asks_rather_than_guessing():
    f = _friday("api")
    f.handle("tell api please")
    assert not SENT, SENT


def test_a_real_instruction_still_goes():
    f = _friday("api")
    f.handle("tell api to run the tests")
    assert SENT == [("s0", "run the tests")], SENT


# ---- two instructions in one breath ----------------------------------------
def test_a_second_instruction_is_not_fed_to_the_first_agent():
    """"tell api to deploy and tell web to build" sent api the words "deploy
    and tell web to build". Not a truncation, worse: a coding agent reading
    "tell web to build" will go and do something about web, so the second half
    does not vanish, it is carried out by the wrong agent."""
    f = _friday("api", "web")
    r = f.handle("tell api to deploy and tell web to build")
    assert not SENT, SENT
    assert "two instructions" in r["reply"], r["reply"]
    assert "api: deploy" in r["reply"], r["reply"]
    assert "web: build" in r["reply"], r["reply"]


def test_both_go_to_the_right_place_once_approved():
    f = _friday("api", "web")
    f.handle("tell api to deploy and tell web to build")
    f.handle("yes")
    assert SENT == [("s0", "deploy"), ("s1", "build")], SENT


def test_the_conjunction_is_removed_as_a_word_not_as_letters():
    """strip(" ,.and") strips CHARACTERS, so "deploy" came out as "eploy"."""
    f = _friday("api", "web")
    r = f.handle("tell api to deploy and tell web to build")
    assert "api: deploy" in r["reply"], r["reply"]


def test_telling_me_when_it_is_done_is_one_instruction():
    """"me" is not a session, and that sentence means one thing."""
    f = _friday("api", "web")
    r = f.handle("tell api to build and tell me when it's done")
    assert "two instructions" not in r["reply"], r["reply"]


# ---- a name written the way people write names -----------------------------
def test_a_quoted_name_is_still_a_name():
    """Quotes are how you write a session name that reads as an ordinary word.
    They stopped it matching at all, so the sentence fell through to the model,
    which answered with invented prose about the session."""
    for said in ("tell 'api' to deploy", 'tell "api" to deploy'):
        intent, p = classify(said)
        assert intent == "tell" and p["name"] == "api", (said, intent, p)


def test_a_shouted_instruction_is_the_same_instruction():
    f = _friday("api")
    f.handle("TELL API TO DEPLOY")
    assert SENT and SENT[0][0] == "s0", SENT


# ---- yes, and things that only look like yes -------------------------------
def test_a_bare_yes_is_bare():
    for said in ("yes", "yes please", "yeah.", "sure", "ok", "do it"):
        assert classify(said)[0] == "confirm", said
    for said in ("no", "nope", "never mind", "cancel"):
        assert classify(said)[0] == "cancel", said


def test_an_instruction_that_starts_with_yes_is_an_instruction():
    """"go ahead and restart the api server" was read as a bare "yes" and
    answered "Nothing was waiting on you", because the pattern matched the
    opening word and let the rest of the sentence go."""
    assert classify("go ahead and restart the api server")[0] != "confirm"
    assert classify("no, use postgres instead")[0] != "cancel"


def test_a_bare_approval_answers_the_agent_when_there_is_no_plan():
    """"approved" and "go ahead" meant RUN THE PLAN, ahead of the yes check. Said
    with an agent blocked on a question, that starts a plan instead of answering
    it, which is a different action taken silently."""
    f = _friday("api", asking="Should I force-push?")
    f.handle("go ahead")
    assert SENT == [("s0", "yes")], SENT


def test_run_the_plan_always_means_the_plan():
    assert classify("run the plan")[0] == "plango"
    assert classify("start the plan")[0] == "plango"


# ---- pointing at nothing ---------------------------------------------------
def test_a_reference_with_nothing_to_refer_to_is_refused():
    """"Use the redis one" means nothing on its own. It fell through to the
    local model, which answered as a general question and produced confident
    prose about a machine it cannot see. An assistant that invents an answer
    about YOUR system is worse than one that says it does not know, because you
    cannot tell the difference from the reply."""
    fleetcache.snapshot = lambda: {}
    f = Friday()
    f.announce = lambda *a, **k: None
    for said in ("use the redis one", "the second one", "do that",
                 "the first one"):
        r = f.handle(said)
        assert "don't know what that refers to" in r["reply"], (said, r["reply"])


def test_a_reference_is_left_alone_when_there_is_something_to_point_at():
    """With an agent asking a question, "the redis one" is an answer and the
    routing should get it."""
    f = _friday("api", asking="Postgres or redis?")
    r = f.handle("use the redis one")
    assert "don't know what that refers to" not in r["reply"], r["reply"]


def test_a_long_sentence_is_not_treated_as_a_bare_reference():
    """A sentence carrying enough of its own meaning is answerable even though
    it contains "the one". The guard is short utterances, where the reference
    IS the whole content and there is nothing else to go on."""
    fleetcache.snapshot = lambda: {}
    f = Friday()
    f.announce = lambda *a, **k: None
    assert f._pointing_at_nothing(
        "use the redis one for the cache because postgres was too slow "
        "last time we tried it") is None


# ---- sentences that merely mention a verb ----------------------------------
def test_an_ordinary_sentence_does_not_become_a_command():
    """Swept forty plausible developer sentences that should do nothing. Three
    of them reached an ACTING intent, and every acting intent is a chance to do
    the wrong thing to a running agent."""
    from friday.conversation import classify as _c
    acting = {"tell", "stop", "mute", "allow", "send", "ticket", "move",
              "plan", "plango", "askall", "open", "new", "schedule",
              "plan_ask", "tracker_pref", "quiet"}
    for said in ("we should probably tell the team", "open source is good",
                 "open a new terminal", "i muted myself on the call",
                 "allow me to explain", "send my regards to the team",
                 "stop and think about this", "the plan changed",
                 "i cancelled my subscription", "let me open the docs"):
        assert _c(said)[0] not in acting, (said, _c(said))


def test_the_real_commands_still_are_commands():
    """The guard above is only worth having if it does not cost the feature."""
    from friday.conversation import classify as _c
    for said, want in (("tell api to deploy", "tell"), ("open api", "open"),
                       ("stop api", "stop"), ("mute api", "mute"),
                       ("run the plan", "plango"), ("send it", "send"),
                       ("let yourself post", "allow"), ("quiet", "quiet"),
                       ("file a ticket: x", "ticket")):
        assert _c(said)[0] == want, (said, _c(said))


def test_a_guessed_session_name_is_not_opened_unasked():
    """Opening is reversible, which is why an exact name goes straight through.
    A guess is not, and not because of the window: opening makes it the target,
    and the target is where your next bare message goes. "Show me the money"
    opened a session called moneyman, and the next sentence would have gone
    there."""
    f = _friday("moneyman", "docs-site")
    opened = []
    actions.focus_session = lambda sid: opened.append(sid) or True
    r = f.handle("show me the money")
    assert not opened, opened
    assert "moneyman" in r["reply"], r["reply"]
    assert f.target == "", f.target


def test_an_exact_name_is_still_opened_at_once():
    f = _friday("moneyman", "docs-site")
    opened = []
    actions.focus_session = lambda sid: opened.append(sid) or True
    f.handle("open moneyman")
    assert opened == ["s0"], opened


# ---- nothing at all --------------------------------------------------------
def test_saying_nothing_does_nothing():
    f = _friday("api")
    for said in ("", "   ", "?"):
        assert not f.handle(said)["reply"].strip(), said
    assert not SENT, SENT


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  saying: half-sentences, two-in-one, and references to nothing")
