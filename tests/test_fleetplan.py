"""A plan that spans the fleet, which is the thing the product was pitched on.

The pitch was one sentence: do this now, meanwhile ask that, hold the rest until
a human reply comes back. What existed was a plan with a single target running
one step at a time against one agent, which is a very good intercom with a
queue. Running five agents and feeding them one at a time is the thing you were
already doing by hand.

Everything here is about one rule and its exception. The rule: one step at a
time PER AGENT, because five prompts at one session interleaves five half-done
jobs and Claude Code accepts all five. The exception: five prompts at five
sessions is not that, it is five agents working, which is the whole reason you
have five.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import plan as plans  # noqa: E402
from friday.conversation import Friday  # noqa: E402


def _plan(*steps, title="a plan", target="api"):
    return plans.get(plans.create(title, target, list(steps)))


# ---- what may run at once --------------------------------------------------
def test_one_step_at_a_time_for_each_agent():
    p = _plan({"text": "first", "target": "api"},
              {"text": "second", "target": "api"})
    got = plans.runnable(p)
    assert [s["text"] for s in got] == ["first"], got


def test_every_agent_moves_at_once():
    """The whole point. Three agents, three steps in flight."""
    p = _plan({"text": "migrate", "target": "api"},
              {"text": "rebuild", "target": "web"},
              {"text": "write it up", "target": "docs"})
    got = plans.runnable(p)
    assert {s["target"] for s in got} == {"api", "web", "docs"}, got


def test_a_question_stops_its_own_track_and_nothing_else():
    """Holding the whole plan meant a question about the docs stopped the
    migration, and the person who had to answer it was the one waiting on the
    migration."""
    p = _plan({"text": "migrate", "target": "api"},
              {"text": "write it up", "target": "docs"},
              {"text": "publish", "target": "docs"})
    docs = [s for s in p["steps"] if s["target"] == "docs"][0]
    plans.set_step(docs["id"], plans.HELD, "which version?")
    p = plans.get(p["id"])
    got = plans.runnable(p)
    assert "migrate" in [s["text"] for s in got], got
    assert "publish" not in [s["text"] for s in got], "ran past the question"


def test_a_held_step_is_offered_again_not_skipped():
    """The rule is stop, do not skip. The answer you give belongs to the step
    that stopped, and running the one after it applies your answer to the wrong
    work."""
    p = _plan({"text": "one", "target": "api"}, {"text": "two", "target": "api"})
    first = p["steps"][0]
    plans.set_step(first["id"], plans.HELD, "which branch?")
    got = plans.runnable(plans.get(p["id"]))
    assert [s["text"] for s in got] == ["one"], got


def test_a_running_step_does_not_get_a_second_one_on_top():
    p = _plan({"text": "one", "target": "api"}, {"text": "two", "target": "api"})
    plans.set_step(p["steps"][0]["id"], plans.RUNNING)
    assert plans.runnable(plans.get(p["id"])) == []


def test_a_plan_written_before_all_this_still_means_what_it_meant():
    """Plans on disk have no per-step target. They must keep running in order
    against the one agent they were written for."""
    p = _plan("one", "two", "three", target="api")
    assert all(s["target"] == "api" for s in p["steps"]), p["steps"]
    assert [s["text"] for s in plans.runnable(p)] == ["one"]


# ---- who a step is for -----------------------------------------------------
def _friday(sessions=("api", "web")):
    f = Friday()
    f.announce = lambda *a, **k: None
    rows = {n: {"sid": f"s-{n}", "label": n} for n in sessions}
    f._find_how = lambda name: ((rows[name.lower()], "exact")
                                if name.lower() in rows else (None, ""))
    return f


def test_a_name_and_a_colon_says_who():
    f = _friday()
    got = f._assign(f._steps_from("api: run the migration; web: rebuild"))
    assert [(s["target"], s["text"]) for s in got] == [
        ("api", "run the migration"), ("web", "rebuild")], got


def test_the_steps_under_a_name_belong_to_it():
    """A list under one heading is one person's list, and repeating the name on
    every line is not how anybody writes."""
    f = _friday()
    got = f._assign(f._steps_from(
        "api: run the migration, then check the logs; web: rebuild"))
    assert [s["target"] for s in got] == ["api", "api", "web"], got


def test_a_name_that_is_not_a_session_is_a_person():
    """You would not name something Friday cannot see unless you meant a
    colleague, and asking is better than guessing it was a typo."""
    f = _friday()
    got = f._assign(f._steps_from("api: deploy it; sam: confirm the copy"))
    assert got[-1]["kind"] == "person" and got[-1]["target"] == "sam", got
    assert got[0]["kind"] == "agent"


def test_an_ordinary_colon_is_not_a_target():
    """"note: this is fragile" is a step, not somebody called note."""
    f = _friday()
    got = f._assign(f._steps_from(
        "api: deploy it; make sure of one thing: the logs are clean"), "api")
    assert all(s["target"] == "api" for s in got), got


# ---- reading it back -------------------------------------------------------
def test_it_reads_the_plan_back_by_who_not_as_one_list():
    """With several agents the order of a numbered list is not the order things
    happen, so showing one sequence would describe something that will not
    occur."""
    f = _friday()
    r = f.handle("plan: api: run the migration; web: rebuild the bundle")
    reply = r["reply"]
    assert "api:" in reply and "web:" in reply, reply
    assert "at once" in reply, reply
    assert "Nothing has run yet" in reply, reply


def test_a_person_in_the_plan_is_flagged_as_one():
    f = _friday()
    r = f.handle("plan: api: deploy it; sam: sign off on the copy")
    assert "a person" in r["reply"], r["reply"]


def test_a_single_agent_plan_still_reads_as_a_sequence():
    f = _friday()
    r = f.handle("plan for api: run the migration, then check the logs")
    assert "one at a time" in r["reply"], r["reply"]


# ---- finishing -------------------------------------------------------------
def test_a_plan_waiting_only_on_a_person_says_so():
    """"All done" and "done except the bit a human owes you" are different
    facts, and only one of them means you can stop thinking about it."""
    said = []
    r = plans.Runner(announce=lambda t, **k: said.append(t),
                     send=lambda *a: True, look=lambda sid: {})
    p = _plan({"text": "deploy", "target": "api"},
              {"text": "sign off", "target": "sam", "kind": "person"})
    plans.set_step(p["steps"][0]["id"], plans.DONE)
    plans.set_step(p["steps"][1]["id"], plans.HELD, "waiting on sam")
    r._finish(plans.get(p["id"]))
    out = " ".join(said)
    assert "waiting on sam" in out, out
    assert "All" not in out or "done" in out.lower(), out
    assert plans.get(p["id"])["state"] == plans.HELD


def test_everything_done_is_reported_as_done():
    said = []
    r = plans.Runner(announce=lambda t, **k: said.append(t),
                     send=lambda *a: True, look=lambda sid: {})
    p = _plan({"text": "a", "target": "api"}, {"text": "b", "target": "web"})
    for st in p["steps"]:
        plans.set_step(st["id"], plans.DONE)
    r._finish(plans.get(p["id"]))
    assert "Plan finished" in " ".join(said), said
    assert plans.get(p["id"])["state"] == plans.DONE


def test_a_person_step_is_never_sent_without_the_posting_switch():
    """A plan that quietly messages your colleagues because a step said so is
    exactly what makes people turn all of it off. A step is further from your
    hands than a dictated message: you approved a list, once, hours ago."""
    from friday import connectors
    connectors.allow_write(False)
    f = Friday()
    f.announce = lambda *a, **k: None
    assert f._tell_person("sam", "hello") is False


# ---- it actually runs them at the same time --------------------------------
def test_the_agents_really_do_work_at_the_same_time():
    """Everything above is about which steps are ELIGIBLE. This is the one that
    checks they are actually in flight together, which is the difference between
    a conductor and a queue with good manners."""
    import json
    import threading

    tmp = Path(__file__).resolve().parent
    from sandbox import use_temp_config as _u
    room = _u()
    paths, sent, lock = {}, [], threading.Lock()
    for sid in ("s-api", "s-web", "s-docs"):
        f = room / f"{sid}.jsonl"
        f.write_text("")
        paths[sid] = str(f)

    def send(sid, text):
        with lock:
            sent.append((time.time(), sid))

        def answer():
            time.sleep(0.6)
            with open(paths[sid], "a") as fh:
                fh.write(json.dumps({"type": "assistant", "message": {
                    "content": [{"type": "text",
                                 "text": f"done: {text}"}]}}) + "\n")
        threading.Thread(target=answer, daemon=True).start()
        return True

    r = plans.Runner(announce=lambda *a, **k: None, send=send,
                     look=lambda sid: {"status": "idle", "question": "",
                                       "path": paths.get(sid, "")})
    r.POLL, r.SETTLE = 0.05, 0.15
    pid = plans.create("ship it", "fleet", [
        {"text": "run the migration", "target": "api", "sid": "s-api"},
        {"text": "check the logs", "target": "api", "sid": "s-api"},
        {"text": "rebuild the bundle", "target": "web", "sid": "s-web"},
        {"text": "write the notes", "target": "docs", "sid": "s-docs"}])
    started = time.time()
    r.start(pid)
    while r.running and time.time() - started < 25:
        time.sleep(0.1)

    got = plans.get(pid)
    assert got["state"] == plans.DONE, got["state"]
    assert all(s["state"] == plans.DONE for s in got["steps"]), got["steps"]

    first = sorted(w for w, _ in sent)[:3]
    assert len(sent) == 4, sent
    assert first[-1] - first[0] < 0.4, f"not concurrent: {first}"
    # api's second step must NOT be one of the first three: one at a time per
    # agent is the rule that parallelism is not allowed to break.
    api = sorted(w for w, sid in sent if sid == "s-api")
    assert api[1] - api[0] > 0.3, f"piled two onto one agent: {api}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  fleet plans: several agents at once, and a person is one of them")
