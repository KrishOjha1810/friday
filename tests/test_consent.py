"""Consent: the yes has to belong to the thing you were shown.

Friday's one promise about anything irreversible is that you see the exact words
first, and that the confirmation you give applies to what you saw. Every case
here is a way that promise came apart in practice. None of them look like
attacks; they are ordinary sentences said in an ordinary order, which is what
makes them worth a file of their own.

The shape of the harm is the same each time. Something goes out under your name,
or a switch is thrown on your behalf, and the reply says it worked. You cannot
tell from the reply that it was not what you meant, and by then it has already
happened, so there is nothing to undo it with.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import actions, connectors, fleetcache, trackers  # noqa: E402
from friday.conversation import Friday  # noqa: E402

SENT = []


def _friday(*labels, asking=""):
    """A Friday over a small fleet, with sends recorded rather than performed.

    The first session is the one holding a question, because most of these
    cases turn on Friday having just told you somebody was blocked."""
    rows = {}
    for i, label in enumerate(labels or ("api", "web")):
        rows[f"s{i}"] = {"sid": f"s{i}", "label": label,
                         "question": asking if i == 0 else "",
                         "status": "needs" if (asking and not i) else "idle",
                         "path": "", "topic": f"{label} work",
                         "mtime": time.time() - i}
    fleetcache.snapshot = lambda: rows
    # Kept so a test can change the world between the offer and the yes, which
    # is where several of these bugs lived.
    globals()["_ROWS"] = rows
    SENT.clear()
    actions.send_to_session = lambda sid, t: SENT.append((sid, t)) or True
    f = Friday()
    f.announce = lambda *a, **k: None
    return f


# ---- fakes for the two things that reach other people ----------------------
class _Jira:
    """A tracker that records what it was asked to file instead of filing it."""

    name = "jira"

    def __init__(self):
        self.created = []

    def ready(self):
        return True

    def setup_hint(self):
        return "connect it"

    def projects(self):
        return [{"key": "PROJ", "name": "Project"}]

    def my_issues(self, limit=8):
        # Reading is what makes something count as a tracker at all, so this
        # has to answer even when the test only cares about writing.
        return []

    def create(self, summary, project="", body="", kind="Task"):
        self.created.append((project, summary))
        return {"ok": True, "key": "PROJ-9", "url": "https://x/browse/PROJ-9"}


class _Slack:
    """A Slack that records posts. Nothing in this file may reach the network,
    and a test that files a real ticket or posts a real message is worse than
    no test, because you only find out from a colleague."""

    name = "slack"

    def __init__(self):
        self.posted = []

    def ready(self):
        return True

    def post(self, channel, text):
        self.posted.append((channel, text))
        return {"ok": True}


def _armed(on: bool) -> None:
    """Lift, or put back, the sandbox's second lock on writing.

    The sandbox disarms the connectors themselves as well as redirecting the
    config directory, because a temp config does not help when `gh` is signed
    in at the machine level. While they are disarmed, can_write() is False
    whatever the switch says, so a test about the switch would pass without
    proving anything. Every connector touched below is a fake that records
    instead of sending, which is what makes lifting it safe, and every test
    puts it back in its finally."""
    if on and hasattr(connectors, "arm"):
        connectors.arm()
    if not on and hasattr(connectors, "disarm"):
        connectors.disarm()


def _writing(on: bool) -> None:
    """The switch a person flips by saying "let yourself post"."""
    connectors.allow_write(on)
    connectors.gh_allow_write(on)


def _with_tracker():
    """Make a recording Jira the only tracker, with writing on.

    Returns the Jira and an undo. A real `gh` signed in on this machine counts
    as a second tracker, which turns every ticket into "which tracker?", so the
    whole set has to be replaced rather than added to."""
    _armed(True)
    _writing(True)
    jira = _Jira()
    trackers.forget()
    real_all, real_get = connectors.all_connectors, connectors.get
    connectors.all_connectors = lambda: {"jira": jira}
    connectors.get = lambda n: (jira if (n or "").lower() == "jira"
                                else real_get(n))

    def undo():
        connectors.all_connectors, connectors.get = real_all, real_get
        trackers.forget()
        # Leaving the machine's real posting switch on after a test run would
        # be the same bug this file is about, one layer down.
        _writing(False)
        _armed(False)

    return jira, undo


def _fake_slack():
    """Put the recording Slack in place of the real one, writing left alone.

    Worth doing even in tests that are not about Slack: if a sentence stops
    going to the fleet and starts going to a channel instead, the fake is what
    stands between this file and a message your colleagues read."""
    sl = _Slack()
    real_get = connectors.get
    connectors.get = lambda n: (sl if (n or "").lower() == "slack"
                                else real_get(n))

    def undo():
        connectors.get = real_get

    return sl, undo


def _with_slack(draft_where="#general", draft_channel="C_GENERAL"):
    """A recording Slack, writing on, and a draft already shown to you."""
    _armed(True)
    _writing(True)
    sl, drop = _fake_slack()

    def undo():
        drop()
        _writing(False)
        _armed(False)

    return sl, {"text": "Looking at it now, should have a fix this afternoon.",
                "where": draft_where, "channel": draft_channel,
                "who": "Sam"}, undo


# ---- a yes that arrives late -----------------------------------------------
def test_a_yes_after_another_turn_does_not_file_the_old_ticket():
    """The pending action never expired, so a yes said much later performed
    whatever was last offered, however long ago and whatever had happened in
    between.

    The order that made it dangerous: you ask for a ticket, Friday offers it,
    then you ask who needs you and Friday says an agent is blocked on a
    question. "Yes" now plainly means the agent. Friday filed the ticket and
    the agent was never answered, so the one thing you were told was waiting
    stayed waiting while something else went out under your name."""
    f = _friday("api", asking="Should I force-push?")
    jira, undo = _with_tracker()
    try:
        offered = f.handle("file a ticket: the parser dies on PDFs")
        assert offered["needs_confirm"] is True, "nothing was pending to go stale"
        f.handle("who needs me?")
        r = f.handle("yes")
        assert jira.created == [], f"filed a stale ticket: {jira.created}"
        assert "Filed" not in r["reply"], r["reply"]
    finally:
        undo()


def test_a_yes_straight_after_the_offer_still_files_it():
    """The fix is about staleness, not about making confirmation harder. The
    ordinary case, where you answer the question you were just asked, has to
    keep working or nothing can be filed at all."""
    f = _friday("api")
    jira, undo = _with_tracker()
    try:
        f.handle("file a ticket in PROJ: the parser dies on PDFs")
        f.handle("yes")
        assert jira.created == [("PROJ", "the parser dies on PDFs")], jira.created
    finally:
        undo()


def test_a_yes_after_another_turn_does_not_send_the_old_message():
    """Same rule, and worse consequences, for words typed into a running agent.
    A message approved for one moment and delivered several turns later lands
    in an agent that has moved on, and a coding agent acts on what it reads.

    Two sessions share a name here only because that is a reliable way to make
    Friday offer rather than send: naming a session exactly is your
    confirmation, and it cannot be when the name fits two of them."""
    f = _friday("api", "friday", "friday", asking="Should I force-push?")
    offered = f.handle("tell friday to deploy the release branch")
    assert offered["needs_confirm"] is True, "nothing was pending to go stale"
    assert not SENT, SENT
    f.handle("who needs me?")
    f.handle("yes")
    stale = [t for _sid, t in SENT if "deploy the release branch" in t]
    assert not stale, f"sent a stale instruction: {SENT}"


def test_an_intervening_turn_does_not_silently_answer_the_agent_either():
    """Whichever way the fix goes, dropping the pending action or asking which
    one you meant, the reply may not claim the old thing was done. A confident
    report of an action you did not intend is the part you cannot recover
    from."""
    f = _friday("api", asking="Should I force-push?")
    jira, undo = _with_tracker()
    try:
        offered = f.handle("file a ticket: the parser dies on PDFs")
        assert offered["needs_confirm"] is True, "nothing was pending to go stale"
        f.handle("who needs me?")
        r = f.handle("yes")
        assert "PROJ-9" not in r["reply"], r["reply"]
    finally:
        undo()


# ---- a sentence about a session is not a grant of permission ---------------
def test_a_sentence_about_a_session_does_not_turn_on_posting():
    """"you can post the results into the api session" is an instruction about
    a coding agent on your own machine. It matched the pattern for giving
    Friday permission to write, and flipped the global switch that lets Friday
    post into Slack and file tickets under your name.

    Nothing visible happened at the time, which is the problem: the next thing
    that wanted permission simply had it."""
    _armed(True)
    _writing(False)
    f = _friday("api")
    _sl, drop = _fake_slack()
    # Once this sentence stops being read as a grant of permission it becomes
    # ordinary conversation, and the local model is neither needed nor wanted
    # in a test about a switch.
    f._chat = lambda said: f._say("(no model in tests)")
    try:
        assert connectors.can_write() is False, "the fixture did not start " \
                                                "with posting off"
        f.handle("you can post the results into the api session")
        assert connectors.can_write() is False, "a sentence about a session " \
                                                "turned on Slack posting"
        assert connectors.gh_can_write() is False, "a sentence about a " \
                                                   "session turned on ticket " \
                                                   "writing"
    finally:
        drop()
        _writing(False)
        _armed(False)


def test_saying_friday_may_post_still_turns_it_on():
    """The narrowing has to leave the real sentence working. This is the only
    way posting is ever meant to get switched on."""
    _armed(True)
    _writing(False)
    f = _friday("api")
    # A fake Slack, because turning posting on asks the saved token which
    # scopes it carries, and that question goes to slack.com.
    _sl, drop = _fake_slack()
    try:
        f.handle("let yourself post")
        assert connectors.can_write() is True, "the real permission sentence " \
                                               "stopped working"
    finally:
        drop()
        _writing(False)
        _armed(False)


def test_turning_it_off_is_still_heard():
    _armed(True)
    _writing(True)
    f = _friday("api")
    _sl, drop = _fake_slack()
    try:
        f.handle("stop posting")
        assert connectors.can_write() is False
    finally:
        drop()
        _writing(False)
        _armed(False)


# ---- the destination you named is the destination --------------------------
def test_naming_a_different_channel_stops_the_send():
    """The draft was written for one channel and "send it to #random" was read
    as a bare "send it": the channel was parsed off the end of the sentence and
    thrown away, so the message went to #general.

    This is the worst kind of wrong, because you said the right thing and were
    told it was sent. A message in the wrong Slack channel cannot be taken back
    and everybody in it has already read it."""
    f = _friday("api")
    sl, draft, undo = _with_slack()
    try:
        f._last_draft = dict(draft)
        r = f.handle("send it to #random")
        assert sl.posted == [], f"posted anyway: {sl.posted}"
        assert "Sent" not in r["reply"], r["reply"]
    finally:
        undo()


def test_send_it_with_no_destination_goes_where_the_draft_was_for():
    """"send it" means the thing you were shown, in the place it was written
    for. No destination named, nothing to disagree with."""
    f = _friday("api")
    sl, draft, undo = _with_slack()
    try:
        f._last_draft = dict(draft)
        f.handle("send it")
        assert [c for c, _t in sl.posted] == ["C_GENERAL"], sl.posted
    finally:
        undo()


def test_naming_the_channel_the_draft_was_for_sends_it():
    """Saying the destination out loud when it is the right one is agreement,
    not a conflict, and must not turn into a question."""
    f = _friday("api")
    sl, draft, undo = _with_slack()
    try:
        f._last_draft = dict(draft)
        f.handle("send it to #general")
        assert [c for c, _t in sl.posted] == ["C_GENERAL"], sl.posted
    finally:
        undo()


def test_the_words_that_go_out_are_the_words_you_were_shown():
    f = _friday("api")
    sl, draft, undo = _with_slack()
    try:
        f._last_draft = dict(draft)
        f.handle("send it")
        assert sl.posted and sl.posted[0][1] == draft["text"], sl.posted
    finally:
        undo()


# ---- everyone is not the fleet ---------------------------------------------
def test_telling_everyone_does_not_reach_the_agent_fleet():
    """"tell everyone standup is at 10" is a thing you say about people. It
    matched the ask-every-session pattern and went to every coding agent, and
    on the way through it was rewritten into a question: each agent received
    "Answer in one or two sentences...: standup is at 10?".

    So a statement meant for colleagues became an interruption in every working
    agent, each of which now believes you asked it something and will answer."""
    f = _friday("api", "web", "docs")
    sl, undo = _fake_slack()
    try:
        f.handle("tell everyone standup is at 10")
        assert not SENT, f"broadcast to the fleet: {SENT}"
        assert sl.posted == [], f"posted without asking: {sl.posted}"
    finally:
        undo()


def test_a_statement_is_never_turned_into_a_question():
    """Even if some future reading of "everyone" does reach sessions, adding a
    question mark to something you stated is putting words in your mouth."""
    f = _friday("api", "web")
    _sl, drop = _fake_slack()
    try:
        f.handle("tell everyone standup is at 10")
        assert not [t for _sid, t in SENT if t.rstrip().endswith("?")], SENT
    finally:
        drop()


def test_naming_a_slack_channel_does_not_broadcast_to_agents():
    """A sentence with a channel in it is about Slack. Fanning it out to the
    fleet is both the wrong audience and an interruption of work."""
    f = _friday("api", "web")
    sl, undo = _fake_slack()
    try:
        f.handle("tell everyone in #eng standup is at 10")
        assert not SENT, f"a channel message reached the fleet: {SENT}"
        # And it may not quietly become a Slack post either: anything people
        # read has to be shown to you and confirmed first.
        assert sl.posted == [], f"posted without asking: {sl.posted}"
    finally:
        undo()


def test_asking_every_session_still_reaches_every_session():
    """The feature itself is worth keeping: putting one question to five
    agents is the part that is actually conducting rather than relaying. It is
    "everyone" and channels that must not trigger it, not the whole idea."""
    f = _friday("api", "web")
    _sl, drop = _fake_slack()
    try:
        r = f.handle("ask all the sessions what they're working on")
        # It shows the exact words and the targets first now. Writing into the
        # whole fleet at once is the largest single action available here, and
        # it used to happen with no confirmation at all.
        assert r["needs_confirm"], r
        assert not SENT, SENT
        f.handle("yes")
        assert sorted(sid for sid, _t in SENT) == ["s0", "s1"], SENT
    finally:
        drop()


def test_a_broadcast_goes_to_the_list_you_were_shown():
    """A session that starts between the offer and the yes joined a broadcast
    approved for two others. The confirmation described one thing and a
    different thing happened, which is the failure confirmations exist to
    prevent."""
    f = _friday("api", "web")
    rows = _ROWS
    f.handle("ask everyone what they're doing")
    rows["s2"] = {"sid": "s2", "label": "late", "question": "",
                  "status": "idle", "path": "", "mtime": time.time()}
    SENT.clear()
    f.handle("yes")
    assert sorted(sid for sid, _t in SENT) == ["s0", "s1"], SENT


def test_a_session_that_closed_is_not_reported_as_sent():
    """It can close between the offer and the yes, and the stored id was used
    regardless and reported as sent: a claim about work that did not happen."""
    f = _friday("api", "web")
    rows = _ROWS
    f.handle("tell api to deploy and tell web to build")
    rows.pop("s1")
    SENT.clear()
    r = f.handle("yes")
    assert [sid for sid, _t in SENT] == ["s0"], SENT
    assert "closed" in r["reply"], r["reply"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    _writing(False)
    _armed(False)
    print("ok  consent: a yes belongs to what you were shown, and to nothing else")


def test_yes_is_not_an_answer_to_which_one():
    """Every one of these bound a yes to the FIRST option, and this is a voice
    product, so "yes" is the likeliest spoken reply to any sentence ending in a
    question mark. "Tell it to drop the migrations table" then "yes" typed that
    into whichever session came first and reported it as sent."""
    f = _friday("jobhunt", "api", "web")
    f.handle("tell it to drop the migrations table")
    SENT.clear()
    r = f.handle("yes")
    assert not SENT, SENT
    assert "doesn't tell me" in r["reply"], r["reply"]
    # Naming it still works.
    f.handle("api")
    assert SENT and SENT[0][1] == "drop the migrations table", SENT


def test_a_re_shown_offer_can_actually_be_accepted():
    """It was re-shown with the original timestamps, so the next yes found it
    stale again and showed it again, forever: the action could never be accepted
    and the only escape was "no"."""
    f = _friday("api")
    f.handle("tell ap to delete the old branches")
    f.handle("what's running")
    SENT.clear()
    f.handle("yes")            # re-shown
    f.handle("yes")            # and accepted
    assert SENT and SENT[0][1] == "delete the old branches", SENT


def test_a_draft_answers_the_newest_message():
    """A dict does not reorder on re-assignment, so the "newest" message was
    whichever channel had spoken FIRST. A reply written under your name went to
    the wrong colleague in the wrong channel, and was reported as sent."""
    f = _friday("api")
    f.inbox._report({"id": "CENG", "name": "eng"},
                    {"who": "Ana", "text": "first thing", "when": 1})
    f.inbox._report({"id": "CGEN", "name": "general"},
                    {"who": "Ravi", "text": "middle thing", "when": 2})
    f.inbox._report({"id": "CENG", "name": "eng"},
                    {"who": "Sam", "text": "the newest thing", "when": 3})
    who = [m["who"] for _c, m in f._newest_messages()]
    assert who[0] == "Sam", who
