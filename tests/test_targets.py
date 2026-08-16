"""Who the words actually reach.

Every failure in here ends with a running coding agent receiving text that was
never meant for it, and being told the delivery went fine. That is the one
mistake this product cannot make: a wrong answer you can see is an annoyance,
but a wrong instruction typed into an agent that then acts on it is damage you
find out about later, from the diff.

The shape they share is that Friday found SOMETHING that looked like a target
and used it. A name you said that is not a session, a session mentioned in
passing further along the sentence, a second agent you addressed in the same
breath, an article, a session with no name at all. In each case the honest
answer was available and cheap: say which part it could not resolve and stop.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import actions, fleetcache  # noqa: E402
from friday.conversation import Friday, classify  # noqa: E402

SENT = []          # (sid, text) typed into a running agent
FOCUSED = []       # sids whose window was brought to the front
STOPPED = []       # sids that were sent an interrupt


def _friday(*labels, asking=""):
    """A fleet of two, "api" and "web", unless you name your own.

    Both of those are ordinary English words as well as session names, which is
    the whole reason they are the default here: they turn up inside sentences
    that are not about them.
    """
    rows = {}
    for i, label in enumerate(labels or ("api", "web")):
        rows[f"s{i}"] = {"sid": f"s{i}", "label": label,
                         "question": asking if i == 0 else "",
                         "status": "needs" if (asking and not i) else "idle",
                         "path": "", "mtime": time.time() - i}
    fleetcache.snapshot = lambda: rows
    SENT.clear()
    FOCUSED.clear()
    STOPPED.clear()
    actions.send_to_session = lambda sid, t: SENT.append((sid, t)) or True
    actions.focus_session = lambda sid: FOCUSED.append(sid) or True
    actions.interrupt_session = lambda sid: STOPPED.append(sid) or True
    f = Friday()
    f.announce = lambda *a, **k: None
    return f


# ---- a name that is not one of your sessions -------------------------------
def test_a_name_that_is_not_a_session_does_not_re_anchor_onto_one():
    """Re-splitting looks for the best session name ANYWHERE in the sentence,
    so a name you said that Friday does not have was quietly replaced by a
    session mentioned later on, and the message was cut at that point.

    "ask sam to review the api changes" reached the api session carrying the
    single word "changes". Sam is a person, the api session is not sam, and
    "changes" is not the instruction. Three separate wrongs from one sentence,
    reported as a successful send.
    """
    for said in ("ask sam to review the api changes",
                 "tell jobhunt to check the api rate limits",
                 "tell the frontend team the web build is green"):
        f = _friday()
        f.handle(said)
        assert not SENT, (said, SENT)


def test_it_says_which_name_it_could_not_find():
    """Stopping is only half the answer. You said a name, and the useful reply
    names it back so you can see what Friday heard."""
    f = _friday()
    r = f.handle("ask sam to review the api changes")
    assert "sam" in r["reply"].lower(), r["reply"]


def test_a_session_named_later_in_the_sentence_is_a_subject_not_a_target():
    """The distinction being drawn: a session name that appears after the name
    slot is part of what you are talking ABOUT. "the api changes" is a subject.
    If it were a target the sentence would have started with it."""
    f = _friday()
    r = f.handle("tell jobhunt to check the api rate limits")
    assert not SENT, SENT
    assert "rate limits" not in r["reply"], r["reply"]


def test_a_name_the_parser_cut_in_half_is_still_repaired():
    """The guard above must not kill the case re-splitting was written for.
    "tell voice bridge to run the tests" gets cut at the first space, and
    "voice" resolves close enough to voicebridge to be acted on, so the sentence
    genuinely does have to be re-read against the live list of names. That name
    sits in the slot the sentence put it in, which is what separates it from the
    sam case."""
    f = _friday("voicebridge")
    f._target_names = lambda: ["voicebridge"]
    name, msg, _want, _exact = f._resplit(
        "tell voice bridge to run the tests", "voice",
        "bridge to run the tests", False)
    assert name == "voicebridge", name
    assert msg == "run the tests", msg


# ---- two targets in one breath ---------------------------------------------
def test_a_second_target_joined_by_and_is_not_prompt_text_for_the_first():
    """"ask api and web to run the tests" sent api the words "and web to run
    the tests". A coding agent reading that will go and do something about web,
    so the second target does not merely get missed, it gets acted on by the
    wrong agent, which is the worse of the two outcomes."""
    f = _friday()
    f.handle("ask api and web to run the tests")
    assert not SENT, SENT


def test_both_of_the_two_are_recognised_as_targets():
    """You addressed two agents, so a reply mentioning one of them has silently
    dropped the other."""
    f = _friday()
    r = f.handle("ask api and web to run the tests")
    reply = r["reply"].lower()
    assert "api" in reply and "web" in reply, r["reply"]


def test_the_other_targets_name_never_becomes_the_message():
    """The invariant underneath both of the above, and the one that has to hold
    however the sentence is resolved: whatever Friday eventually sends, and
    whether or not you approve it, no agent is ever handed the name of another
    agent as the thing to do."""
    f = _friday()
    f.handle("ask api and web to run the tests")
    f.handle("yes")
    for sid, text in SENT:
        assert not text.lower().startswith("and "), (sid, text)
        assert "web to run" not in text.lower(), (sid, text)


# ---- a session with no name ------------------------------------------------
def test_a_bare_yes_does_not_reach_a_session_friday_cannot_name():
    """A session whose label is the empty string can be routed to but not
    talked about. Friday sent it a bare "yes" and answered "Told it yes.", so
    the only record of which agent was given permission to delete a production
    database was the word "it".

    A yes typed into an agent is not a message, it is authority, and authority
    granted to something you cannot name back is not something you agreed to.
    """
    f = _friday("", asking="Delete the production database?")
    f.handle("yes")
    assert not SENT, SENT


def test_a_nameless_session_is_shown_by_its_sid():
    """The fix on the routing side is only half of it: an unnameable session is
    still there and still blocked, and listing it as an empty string ("- :
    Delete the production database?") is a line you cannot act on. The sid is
    not pretty, but it is stable and it is something you can say back."""
    f = _friday("", asking="Delete the production database?")
    r = f.handle("is anyone stuck")
    assert "s0" in r["reply"], r["reply"]
    assert "- :" not in r["reply"], r["reply"]


# ---- articles are not names ------------------------------------------------
def test_an_article_is_never_a_session_name():
    """"open a new tab" was classified as the command "open" with the name "a",
    which then prefix-matched the first session whose label starts with that
    letter. Friday answered "Opened api." to a sentence that was not about api
    at all. _FILLER exists for exactly this and was not consulted here."""
    articles = {"a", "an", "the", "my"}
    for said in ("open a new tab", "open the other one", "go to my inbox",
                 "show me an example"):
        intent, p = classify(said)
        assert p.get("name", "") not in articles, (said, intent, p)


def test_opening_a_new_tab_does_not_pull_a_session_to_the_front():
    """The behaviour behind the classification. Grabbing focus is not
    destructive, but it moves you to a window you did not ask for while you were
    mid-sentence about something else, and it tells you it did the right
    thing."""
    f = _friday()
    f.handle("open a new tab")
    assert not FOCUSED, FOCUSED


# ---- names that do not look like names -------------------------------------
def test_a_one_character_session_name_can_still_be_stopped():
    """Session labels come from directory names, and a directory can be called
    "7". The stop pattern required at least two characters, so "stop 7" matched
    nothing, fell through to the model, and came back as conversation while the
    agent carried on doing whatever you wanted stopped."""
    f = _friday("7", "web")
    f.handle("stop 7")
    assert STOPPED == ["s0"], STOPPED


def test_a_vague_request_for_quiet_is_not_a_session_name():
    """"silence the notifications for a bit" is a request to be left alone. The
    mute pattern allowed "for now" as a trailing phrase but not "for a bit", so
    the rest of the sentence was swallowed into the name and Friday went looking
    for a session called "notifications for a bit". Answering a request for
    quiet with a complaint about a session nobody mentioned reads as Friday not
    having listened."""
    said = "silence the notifications for a bit"
    _intent, p = classify(said)
    assert "for a bit" not in p.get("name", ""), p
    f = _friday()
    r = f.handle(said)
    assert "notifications for a bit" not in r["reply"], r["reply"]
    assert not SENT, SENT


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  targets: no wrong agent, no borrowed name, no invented target")
