"""What happens when the fleet all talks at once.

The interesting failures in a supervisor are not "does it work", they are what
it does when five things happen in the same second. That is also the normal
case: agents finish in bursts, because you started them in a burst.

The question behind all of this is whether there is a queue. There is, and it is
deliberately not a stored one. Everything about who is waiting is derived from
the live fleet each time it is asked, because a stored queue goes stale in ways
nobody can see: the agent times out, you answer it in its own terminal, it gives
up and moves on. Each of those leaves an entry that is still there and no longer
true, and Friday would go on offering you a question nobody is asking.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

TMP = use_temp_config()

from friday import (actions, budget as B, connectors, fleetcache,  # noqa: E402
                    watchtower)
from friday.conversation import Friday  # noqa: E402

watchtower.SETTLE = 0.0


def _fleet(*spec):
    """(label, question) pairs into a fleet that has just spoken."""
    rows = {}
    for i, (label, q) in enumerate(spec):
        p = TMP / f"crowd-{i}.jsonl"
        p.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"{label} finished the migration."}]}}) + "\n")
        rows[f"s{i}"] = {"sid": f"s{i}", "label": label, "question": q,
                         "status": "needs" if q else "idle", "path": str(p),
                         "topic": f"{label} work", "mtime": time.time() - i}
    return rows


def _tower(rows, budget=None):
    said = []
    w = watchtower.Watchtower(announce=lambda t, items=None: said.append((t, items)),
                              budget=budget or B.Budget())
    w._fleet = lambda: list(rows.values())
    w._looking_at = lambda: ""
    # Not the machine's quiet setting. The watchtower asks voicebridge whether
    # to stay silent, so running this suite with voice switched off made every
    # announcement vanish and the failures looked like logic bugs.
    w._hushed = lambda: False
    return w, said


# ---- five at once ----------------------------------------------------------
def test_five_replies_is_one_interruption_not_five():
    """Five separate announcements cost five times the attention for one
    moment's worth of news. compose() existed for exactly this and the
    watchtower never called it."""
    w, said = _tower(_fleet(("api", ""), ("web", ""), ("docs", ""),
                            ("infra", ""), ("mail", "")))
    w._tick()
    assert len(said) == 1, f"{len(said)} interruptions"


def test_the_ones_asking_you_come_first_and_are_never_merged_away():
    """The whole reason to interrupt is that somebody cannot continue without
    you. Those are what you act on; the rest is news."""
    w, said = _tower(_fleet(("web", ""), ("api", "Should I force-push?"),
                            ("docs", ""), ("jobhunt", "Which database?")))
    w._tick()
    text, items = said[0]
    body = text.split("\n")
    first_two = " ".join(body[:4])
    assert "api" in first_two and "jobhunt" in first_two, text
    kinds = [i["kind"] for i in items]
    assert kinds[0] == "blocked" and kinds[1] == "blocked", kinds


def test_a_blocked_session_does_not_say_it_merely_spoke():
    """In a merged list the lead is the only part read at a glance, and "says"
    understates a session that cannot continue."""
    w, said = _tower(_fleet(("api", "Should I force-push?")))
    w._tick()
    assert "needs you" in said[0][0], said[0][0]


def test_it_says_how_many_are_waiting_when_several_are():
    """You are about to answer one of them, and the others have to still exist
    afterwards."""
    w, said = _tower(_fleet(("api", "Which one?"), ("web", "Which one?"),
                            ("docs", "Which one?")))
    w._tick()
    assert "3 sessions are waiting" in said[0][0], said[0][0]


def test_the_count_does_not_count_itself():
    w, said = _tower(_fleet(("api", "a?"), ("web", "b?")))
    w._tick()
    text = said[0][0]
    assert "3 things" not in text, text


def test_everything_still_arrives_as_separate_things_to_act_on():
    """Merging is about the interruption, not about what you can do afterwards.
    The page still needs one item per session."""
    rows = _fleet(("api", ""), ("web", ""), ("docs", ""))
    w, said = _tower(rows)
    w._tick()
    labels = {i["label"] for i in said[0][1]}
    assert labels == {"api", "web", "docs"}, labels


def test_what_does_not_fit_is_held_not_dropped():
    """"and four others finished" has to be a count you can then ask about."""
    bud = B.Budget(per_hour=1, burst=1)
    w, said = _tower(_fleet(*[(f"s{i}", "") for i in range(6)]), budget=bud)
    w._tick()
    assert bud.waiting() >= 4, bud.waiting()
    assert "what did I miss" in said[0][0], said[0][0]


def test_a_blocked_agent_is_never_rationed_away():
    """Rationing the one category worth interrupting for is the failure that
    makes the whole feature pointless."""
    bud = B.Budget(per_hour=1, burst=0)
    w, said = _tower(_fleet(("api", "Should I force-push?"), ("web", "")),
                     budget=bud)
    w._tick()
    assert said, "said nothing at all"
    assert "api" in said[0][0], said[0][0]


# ---- the queue afterwards --------------------------------------------------
def _friday(rows):
    fleetcache.snapshot = lambda: rows
    f = Friday()
    f.announce = lambda *a, **k: None
    return f


def test_answering_one_offers_the_next():
    """Answering one of five and hearing nothing about the other four is how a
    queue silently becomes a pile."""
    rows = _fleet(("api", "Should I force-push?"), ("jobhunt", "Which database?"),
                  ("docs", "Which version?"))
    # Explicit, because the offer order is oldest-first and the point of the
    # test is which one comes next, not how the fixture happened to be built.
    rows["s0"]["mtime"] = time.time() - 3000      # api, longest stuck
    rows["s1"]["mtime"] = time.time() - 2000      # jobhunt
    rows["s2"]["mtime"] = time.time() - 1000      # docs
    f = _friday(rows)
    actions.send_to_session = lambda sid, text: True
    f._find_how = lambda n: (rows["s0"], "exact")
    r = f.handle("tell api yes")
    assert "api" in r["reply"], r["reply"]
    assert "Next" in r["reply"] and "jobhunt" in r["reply"], r["reply"]
    assert "1 more after that" in r["reply"], r["reply"]


def test_the_last_answer_does_not_invent_a_next():
    rows = _fleet(("api", "Should I force-push?"))
    f = _friday(rows)
    actions.send_to_session = lambda sid, text: True
    f._find_how = lambda n: (rows["s0"], "exact")
    r = f.handle("tell api yes")
    assert "Next" not in r["reply"], r["reply"]


def test_the_queue_is_whoever_is_actually_stuck_right_now():
    """Not a list Friday keeps. An agent that gave up, or that you answered in
    its own terminal, must not still be offered."""
    rows = _fleet(("api", "Should I force-push?"), ("web", "Which branch?"))
    f = _friday(rows)
    assert len(f._waiting()) == 2
    rows["s1"]["question"] = ""              # it moved on by itself
    assert [r["label"] for r in f._waiting()] == ["api"]


def test_the_longest_stuck_is_offered_first():
    """Whoever has been blocked longest has cost the most."""
    rows = _fleet(("new", "a?"), ("old", "b?"))
    rows["s1"]["mtime"] = time.time() - 3600
    f = _friday(rows)
    assert [r["label"] for r in f._waiting()][0] == "old"


# ---- a bare yes ------------------------------------------------------------
def test_yes_reaches_the_one_session_that_asked():
    """The most natural possible answer to "Should I proceed?". Friday said
    "Nothing was waiting on you" while a session sat blocked, which is both
    wrong and the exact moment the product is meant to earn its keep."""
    rows = _fleet(("api", "Should I proceed?"))
    f = _friday(rows)
    sent = []
    actions.send_to_session = lambda sid, t: sent.append((sid, t)) or True
    r = f.handle("yes")
    assert sent == [("s0", "yes")], sent
    assert "api" in r["reply"], r["reply"]


def test_no_reaches_it_too():
    rows = _fleet(("api", "Delete the old branch?"))
    f = _friday(rows)
    sent = []
    actions.send_to_session = lambda sid, t: sent.append((sid, t)) or True
    f.handle("no")
    assert sent == [("s0", "no")], sent


def test_a_bare_yes_with_two_waiting_is_a_question_back():
    """"Yes" carries no clue about which one you meant, and a yes typed into
    the wrong agent is not a message, it is permission."""
    rows = _fleet(("api", "Should I proceed?"), ("web", "Should I proceed?"))
    f = _friday(rows)
    sent = []
    actions.send_to_session = lambda sid, t: sent.append((sid, t)) or True
    r = f.handle("yes")
    assert not sent, sent
    assert "2 sessions are waiting" in r["reply"], r["reply"]
    assert "api" in r["reply"] and "web" in r["reply"], r["reply"]


def test_yes_with_nothing_waiting_still_says_so():
    rows = _fleet(("api", ""))
    f = _friday(rows)
    sent = []
    actions.send_to_session = lambda sid, t: sent.append((sid, t)) or True
    r = f.handle("yes")
    assert not sent, sent
    assert "Nothing was waiting" in r["reply"], r["reply"]


def test_a_session_that_vanished_is_said_plainly():
    """Between being announced and being answered, an agent can be closed. A
    confident "sent" here would be a lie about work that never happened."""
    rows = _fleet(("api", "Should I force-push?"))
    f = _friday(rows)
    rows.clear()
    r = f.handle("tell api yes")
    assert "can't find" in r["reply"].lower(), r["reply"]


# ---- an hour away ----------------------------------------------------------
def test_an_hour_away_is_summarised_by_who_not_by_line():
    """Twenty-two notes from four sessions is four things that happened. A list
    you will not read is not an answer, and the reason you asked is that you
    were not there."""
    f = _friday({})
    for i in range(22):
        f.budget.hold(f"session{i % 4} says: finished job {i}", f"session{i % 4}")
    reply = f.handle("what did I miss")["reply"]
    assert "22 things" in reply and "4 sources" in reply, reply
    assert reply.count("\n- ") == 4, reply
    assert "and 5 earlier" in reply, reply


def test_being_told_means_it_is_no_longer_missed():
    f = _friday({})
    f.budget.hold("api says: done", "api")
    assert "api" in f.handle("what did I miss")["reply"]
    assert "Nothing since" in f.handle("what did I miss")["reply"]


def test_a_quiet_hour_with_somebody_stuck_says_the_stuck_part():
    rows = _fleet(("api", "Should I force-push?"))
    f = _friday(rows)
    r = f.handle("what did I miss")
    assert "api" in r["reply"] and "waiting" in r["reply"], r["reply"]


# ---- two sessions with the same name ---------------------------------------
def test_two_sessions_called_the_same_thing_is_a_question_not_a_guess():
    """Not rare: it is what happens the moment you open a second agent on the
    same project. Picking either is a message typed into the wrong running
    agent, reported as success."""
    rows = _fleet(("friday", ""), ("friday", ""))
    rows["s0"]["topic"], rows["s1"]["topic"] = "the tracker seam", "the soak test"
    f = _friday(rows)
    sent = []
    actions.send_to_session = lambda sid, text: sent.append(sid) or True
    r = f.handle("tell friday to run the tests")
    assert "2 sessions called friday" in r["reply"], r["reply"]
    assert not sent, "sent it anyway"
    assert "soak test" in r["reply"] and "tracker seam" in r["reply"], r["reply"]


def test_it_breaks_the_tie_when_only_one_is_blocked():
    """A narrow guess, and a defensible one: the blocked one is the one you are
    talking to. Still marked as a guess rather than as your confirmation."""
    rows = _fleet(("friday", ""), ("friday", "Should I bump the cap?"))
    f = _friday(rows)
    hit, how = f._find_how("friday")
    assert hit["sid"] == "s1" and how == "fuzzy", (hit["sid"], how)


def test_a_common_word_as_a_session_name_cannot_hijack_a_sentence():
    """A closed session called "test" matched the word "tests" at the end of
    "tell friday to run the tests" and quietly took the message away from
    friday. Directories get named after common words."""
    rows = _fleet(("friday", ""))
    f = _friday(rows)
    f._target_names = lambda: ["friday", "test", "agent", "Desktop"]
    name, msg, _want, _exact = f._resplit(
        "tell friday to run the tests", "friday", "run the tests", False)
    assert name == "friday", name
    assert msg == "run the tests", msg


def test_a_genuinely_bad_split_is_still_repaired():
    """This is why re-splitting exists: "tell voice bridge ..." cuts at the
    first space, and "voice" resolves close enough to voicebridge to be acted
    on."""
    rows = _fleet(("voicebridge", ""))
    f = _friday(rows)
    f._target_names = lambda: ["voicebridge"]
    name, _msg, _want, _exact = f._resplit(
        "tell voice bridge to run the tests", "voice",
        "bridge to run the tests", False)
    assert name == "voicebridge", name


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  crowd: one interruption, a queue that cannot go stale, no wrong sends")
