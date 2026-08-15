"""A plan that survives a restart and advances only on evidence.

Every instruction before this was a single shot: you said a thing, an agent did
a thing, and nothing remembered what you were in the middle of. The failure
modes of the fix are worse than the gap, though, which is why each of these
exists: firing every step at once, calling a step done because it was sent, or
carrying on past a question so the rest of the plan runs on an assumption
nobody made.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import plan as plans, replies  # noqa: E402


def _transcript(d, text):
    p = Path(d) / "s.jsonl"
    with open(p, "a") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": text}]}}) + "\n")
    return str(p)


def test_a_plan_is_written_down_before_anything_runs():
    pid = plans.create("three things", "api",
                       ["run the tests", "fix what fails", "commit"])
    assert pid
    p = plans.get(pid)
    assert p["state"] == plans.PENDING, "a new plan must not be running"
    assert [s["state"] for s in p["steps"]] == [plans.PENDING] * 3
    assert [s["seq"] for s in p["steps"]] == [0, 1, 2], "order was not kept"


def test_a_plan_outlives_the_process():
    """The point of SQLite here: you close the tab, the Mac sleeps, and the
    plan is still where you left it."""
    pid = plans.create("survivor", "api", ["one", "two"])
    plans.set_step(plans.get(pid)["steps"][0]["id"], plans.DONE, "did it")
    again = plans.get(pid)          # a fresh read, new connection
    assert again["steps"][0]["state"] == plans.DONE
    assert again["steps"][0]["note"] == "did it"


def test_steps_run_one_at_a_time_and_in_order():
    """Firing five prompts at a session interleaves five half-done jobs, and
    Claude Code will happily accept all five."""
    with tempfile.TemporaryDirectory() as d:
        path = _transcript(d, "older")
        sent, state = [], {"status": "idle", "question": "", "path": path}

        def send(sid, text):
            sent.append(text)
            # the agent answers, as one would
            _transcript(d, f"finished: {text}")
            return True

        pid = plans.create("ordered", "api", ["one", "two", "three"], sid="s")
        r = plans.Runner(announce=lambda *a, **k: None, send=send,
                         look=lambda sid: state)
        r.POLL, r.SETTLE = 0.05, 0.1
        r.start(pid)
        for _ in range(100):
            if plans.get(pid)["state"] == plans.DONE:
                break
            time.sleep(0.05)
        assert sent == ["one", "two", "three"], sent
        assert plans.get(pid)["state"] == plans.DONE


def test_a_step_is_not_done_because_it_was_sent():
    """"Sent" is not "done". Advancing on the send means the second prompt
    lands while the first is still being worked on."""
    with tempfile.TemporaryDirectory() as d:
        path = _transcript(d, "older")
        state = {"status": "working", "question": "", "path": path}
        sent = []
        pid = plans.create("slow", "api", ["one", "two"], sid="s")
        r = plans.Runner(announce=lambda *a, **k: None,
                         send=lambda sid, t: sent.append(t) or True,
                         look=lambda sid: state)
        r.POLL, r.SETTLE, r.STEP_TIMEOUT = 0.05, 0.1, 1.0
        r.start(pid)
        time.sleep(0.5)
        assert sent == ["one"], "moved on before the agent answered"
        r.stop()


def test_a_question_stops_the_plan_rather_than_being_ignored():
    """This is the one that matters. Running the next step past a question is
    answering it by ignoring it, and everything after runs on an assumption
    nobody made."""
    with tempfile.TemporaryDirectory() as d:
        path = _transcript(d, "older")
        state = {"status": "idle", "question": "", "path": path}
        said, sent = [], []

        def send(sid, text):
            sent.append(text)
            state["question"] = "Should I force-push?"
            return True

        pid = plans.create("asks", "api", ["one", "two", "three"], sid="s")
        r = plans.Runner(announce=lambda t, items=None: said.append(t),
                         send=send, look=lambda sid: state)
        r.POLL, r.SETTLE = 0.05, 0.1
        r.start(pid)
        time.sleep(0.6)
        assert sent == ["one"], f"kept going past a question: {sent}"
        p = plans.get(pid)
        assert p["state"] == plans.HELD, p["state"]
        assert p["steps"][0]["state"] == plans.HELD
        assert any("force-push" in s for s in said), said


def test_an_unreachable_session_pauses_instead_of_pretending():
    with tempfile.TemporaryDirectory() as d:
        state = {"status": "idle", "question": "", "path": _transcript(d, "x")}
        said = []
        pid = plans.create("dead", "api", ["one", "two"], sid="s")
        r = plans.Runner(announce=lambda t, items=None: said.append(t),
                         send=lambda sid, t: False, look=lambda sid: state)
        r.POLL, r.SETTLE = 0.05, 0.1
        r.start(pid)
        time.sleep(0.4)
        assert plans.get(pid)["state"] == plans.HELD
        assert any("couldn't reach" in s for s in said), said


def test_a_silent_agent_holds_the_plan_and_says_so():
    """Fifteen minutes with no reply is not a reason to send the next thing."""
    with tempfile.TemporaryDirectory() as d:
        state = {"status": "working", "question": "",
                 "path": _transcript(d, "older")}
        said = []
        pid = plans.create("quiet", "api", ["one", "two"], sid="s")
        r = plans.Runner(announce=lambda t, items=None: said.append(t),
                         send=lambda sid, t: True, look=lambda sid: state)
        r.POLL, r.SETTLE, r.STEP_TIMEOUT = 0.05, 0.1, 0.3
        r.start(pid)
        time.sleep(0.9)
        assert plans.get(pid)["state"] == plans.HELD
        assert any("fifteen minutes" in s for s in said), said


def test_it_reads_like_something_a_person_wrote():
    pid = plans.create("readable", "api", ["one", "two"])
    p = plans.get(pid)
    plans.set_step(p["steps"][0]["id"], plans.DONE)
    text = plans.describe(plans.get(pid))
    assert "1 of 2 done" in text, text
    assert "1. one" in text and "done" in text, text


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  plan: in order, one at a time, and it stops when asked a question")


# ---- the three that a reading of the code found, none of which had a test ----

def test_resuming_a_held_plan_re_enters_the_step_that_held():
    """The module's first rule is "stop, do not skip", and the resume path did
    exactly what the rule forbids: it returned the step AFTER the held one, so
    you answered the question and the answer went nowhere."""
    pid = plans.create("held", "api", ["one", "two", "three"], sid="s")
    p = plans.get(pid)
    plans.set_step(p["steps"][0]["id"], plans.DONE)
    plans.set_step(p["steps"][1]["id"], plans.HELD, "Should I force-push?")
    nxt = plans.next_step(plans.get(pid))
    assert nxt["seq"] == 1, f"resumed at step {nxt['seq'] + 1}, skipping the held one"
    assert nxt["text"] == "two"


def test_a_step_caught_by_a_crash_is_not_counted_as_done():
    """A step left RUNNING when the process died is of unknown outcome. The old
    code skipped it and then announced the plan finished, which is a claim about
    work that may never have happened."""
    pid = plans.create("crashed", "api", ["one", "two"], sid="s")
    p = plans.get(pid)
    plans.set_step(p["steps"][0]["id"], plans.RUNNING)
    plans.set_step(p["steps"][1]["id"], plans.DONE)
    stuck = plans.unfinished(plans.get(pid))
    assert [s["seq"] for s in stuck] == [0], stuck

    said = []
    r = plans.Runner(announce=lambda t, items=None: said.append(t),
                     send=lambda sid, t: True,
                     look=lambda sid: {"status": "idle", "question": "",
                                       "path": ""})
    r.POLL = 0.05
    r.start(pid)
    time.sleep(0.5)
    assert plans.get(pid)["state"] != plans.DONE, "claimed a crashed plan finished"
    assert any("don't know whether it" in s for s in said), said


def test_a_step_is_not_finished_by_the_agent_clearing_its_throat():
    """An agent answers in stages. Without a settle, "Let me look at that"
    completes the step and the next prompt lands on work still in progress. The
    codebase had already learned this twice elsewhere and not here."""
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "s.jsonl"

        def append(text):
            with open(path, "a") as f:
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": text}]}}) + "\n")

        append("older")
        state = {"status": "idle", "question": "", "path": str(path)}
        sent = []
        pid = plans.create("staged", "api", ["one", "two"], sid="s")

        def send(sid, text):
            sent.append(text)
            append("Let me look at that")
            return True

        r = plans.Runner(announce=lambda *a, **k: None, send=send,
                         look=lambda sid: state)
        r.POLL, r.SETTLE = 0.05, 1.5
        r.start(pid)
        time.sleep(0.6)
        assert sent == ["one"], f"moved on while it was still talking: {sent}"
        r.stop()
