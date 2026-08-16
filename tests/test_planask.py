"""Asking the agent for the plan, which is the part that needed the codebase.

Friday could already run a plan, and already had an opinion about what to start
on. The steps in between had to be typed by you. That is exactly backwards: the
one part of the job that needs to know what is actually in the repository was
the part left to the person who had delegated the work.

Two things are load-bearing here and both are about restraint. The agent is
asked to plan and told not to start, and the steps come back for approval before
one of them is sent. An agent that plans and executes in the same breath is an
agent you cannot say no to.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

TMP = use_temp_config()

from friday import actions, plan as plans, replies  # noqa: E402
from friday.conversation import Friday, classify  # noqa: E402
from friday.plan import steps_from_answer  # noqa: E402

ANSWER = """Sure, here is how I would approach it.

1. **Add** the `oauth` module with a token store
2. Wire the callback route into the router
3. Write tests for the refresh path

Caveats: this assumes the router already exists.
"""


# ---- pulling steps out of prose -------------------------------------------
def test_it_takes_the_list_and_leaves_the_prose():
    assert steps_from_answer(ANSWER) == [
        "Add the oauth module with a token store",
        "Wire the callback route into the router",
        "Write tests for the refresh path"]


def test_a_flattened_list_is_still_a_list():
    """The answer does not arrive with its line breaks: the transcript readers
    normalise whitespace, so a real numbered plan is one long line. The parser
    read line by line, so it would have found nothing in production."""
    flat = ("Sure, here is how I would approach it. 1. Add the oauth module "
            "with a token store 2. Wire the callback route into the router "
            "3. Write tests for the refresh path")
    assert steps_from_answer(flat) == steps_from_answer(ANSWER)


def test_the_trailing_caveat_is_not_part_of_the_last_step():
    """Flattened, the closing paragraph glues itself to the final step, and the
    final step is the thing that gets sent to an agent verbatim."""
    got = steps_from_answer(
        "1. Add the oauth module with a token store 2. Write tests for the "
        "refresh path Caveats: this assumes the router already exists.")
    assert got[-1] == "Write tests for the refresh path", got


def test_a_number_in_prose_is_not_a_plan():
    """"Python 3. 11" and "it takes 2. 5 seconds" both look like enumerators
    once the line breaks are gone. The guard is that a real list counts up."""
    assert steps_from_answer("We use Python 3. 11 here and it works well.") == []
    assert steps_from_answer("It takes 2. 5 seconds and version 1. 2 is out.") == []


def test_a_question_in_the_list_is_not_a_step():
    """It is the agent asking YOU something. Sending it back as an instruction
    is a loop."""
    got = steps_from_answer("1. Do the thing\n2. Should I use PKCE or implicit?\n"
                            "3. Then write the tests for it")
    assert len(got) == 2 and not any(s.endswith("?") for s in got), got


def test_code_is_not_a_plan():
    """A plan made of half a diff, sent back to an agent as a prompt, is worse
    than no plan."""
    got = steps_from_answer(
        "1. Add the handler function to the router\n```python\n"
        "1. this is a comment in code\n2. so is this one here\n```\n"
        "2. Then register it in the app factory")
    assert len(got) == 2, got


def test_prose_with_no_list_yields_nothing():
    """A missed step costs you one line of typing. An invented step means an
    agent is told to do something nobody asked for, and it will do it."""
    assert steps_from_answer(
        "I would start by adding the module. Then wire up the route. "
        "Finally write some tests.") == []


def test_it_does_not_run_away_with_a_huge_list():
    got = steps_from_answer("\n".join(f"{i}. Do the thing number {i} properly"
                                      for i in range(1, 40)))
    assert len(got) == plans.MAX_STEPS


def test_the_same_step_twice_is_one_step():
    got = steps_from_answer("1. Wire the callback route in\n"
                            "2. wire the callback route in\n"
                            "3. Write the tests for the refresh path")
    assert len(got) == 2, got


# ---- the words ------------------------------------------------------------
def test_asking_is_told_apart_from_dictating():
    """"plan: a, then b" is you writing the steps. "work out a plan for x" is
    you naming the goal. They must not collide."""
    assert classify("work out a plan for adding OAuth")[0] == "plan_ask"
    assert classify("ask api for a plan to migrate the db")[0] == "plan_ask"
    assert classify("how should we handle retries")[0] == "plan_ask"
    assert classify("plan: first do a, then do b")[0] == "plan"


def test_the_named_session_survives_classification():
    _i, p = classify("ask api for a plan to migrate the db")
    assert p["target"] == "api" and "migrate" in p["goal"], p


# ---- the flow -------------------------------------------------------------
class _Fake:
    """One session, one transcript file, answering when asked."""

    def __init__(self, answer=ANSWER):
        self.path = TMP / "fake.jsonl"
        self.path.write_text("")
        self.sent = []
        self.answer = answer

    def row(self):
        return {"sid": "s1", "label": "api", "path": str(self.path),
                "vendor": "claude", "mtime": time.time()}

    def send(self, sid, text):
        self.sent.append(text)
        import json
        with open(self.path, "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": self.answer}]}}) + "\n")
        return True


def _friday(fake):
    f = Friday()
    said = []
    f.announce = lambda text, **k: said.append(text)
    f.said = said
    f._find_how = lambda name: (fake.row(), "exact")
    # A real conversation has either named the session or been talking to it.
    # Without that, the flow correctly stops to ask which one, which is tested
    # on its own below.
    f.target = "api"
    actions.send_to_session = fake.send
    return f


def _settle(f, n=1):
    for _ in range(120):
        if len(f.said) >= n:
            return
        time.sleep(0.05)


def test_it_tells_the_agent_not_to_start():
    """The entire safety story is the ordering: plan, then approve, then run."""
    fake = _Fake()
    f = _friday(fake)
    f.handle("work out a plan for adding OAuth")
    _settle(f)
    assert fake.sent, "nothing was sent"
    assert "not start" in fake.sent[0].lower(), fake.sent[0]
    assert "OAuth" in fake.sent[0], fake.sent[0]


def test_the_steps_come_back_for_approval_and_nothing_runs():
    fake = _Fake()
    f = _friday(fake)
    f.handle("work out a plan for adding OAuth")
    _settle(f)
    out = "\n".join(f.said)
    assert "Nothing has run yet" in out, out
    assert "token store" in out, out
    assert len(fake.sent) == 1, f"it sent a step without asking: {fake.sent}"


def test_the_plan_is_written_down_so_it_survives():
    fake = _Fake()
    f = _friday(fake)
    f.handle("work out a plan for adding OAuth")
    _settle(f)
    p = plans.latest()
    assert p and len(p["steps"]) == 3, p
    assert all(s["state"] == plans.PENDING for s in p["steps"]), p["steps"]


def test_an_answer_with_no_plan_is_shown_not_invented():
    fake = _Fake(answer="I'd rather not, that module is a mess right now.")
    f = _friday(fake)
    before = plans.latest()
    f.handle("work out a plan for adding OAuth")
    _settle(f)
    out = "\n".join(f.said)
    assert "mess right now" in out, out
    assert "haven't written anything down" in out, out
    assert plans.latest() == before, "wrote a plan out of prose"


def test_it_asks_which_session_rather_than_picking_one():
    """A plan sent to the wrong agent is a plan the wrong agent carries out."""
    fake = _Fake()
    f = _friday(fake)
    f.target = ""
    r = f.handle("work out a plan for adding OAuth")
    assert "which session" in r["reply"].lower(), r["reply"]
    assert not fake.sent, "guessed"


def test_it_will_not_start_a_second_plan_over_a_running_one():
    """Two live plans against the same fleet interleave, and "run the plan"
    stops meaning one thing."""
    fake = _Fake()
    f = _friday(fake)
    real = f.plans
    f.plans = type("Busy", (), {"running": True})()
    try:
        r = f.handle("work out a plan for adding OAuth")
    finally:
        f.plans = real
    assert "already running" in r["reply"], r["reply"]
    assert not fake.sent


def test_an_agent_it_cannot_type_into_is_refused():
    fake = _Fake()
    f = _friday(fake)
    row = dict(fake.row(), vendor="antigravity")
    f._find_how = lambda name: (row, "exact")
    r = f.handle("work out a plan for adding OAuth")
    assert "own app" in r["reply"], r["reply"]
    assert not fake.sent, "sent to something it cannot reach"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  plan_ask: the agent writes the steps, you approve them")
