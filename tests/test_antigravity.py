"""Google Antigravity, and the two ways reading it could go wrong.

Antigravity is the third vendor and the first that Friday can read but cannot
conduct: it runs in its own app, not a terminal. That asymmetry is the
interesting part. The vendor seam was built on the assumption that sensing
differs per vendor and conducting does not, and this is the case that breaks
the second half of that assumption, so it had better break loudly.

The other risk is subtler. Antigravity writes its open questions to a plan file
and leaves them there after you answer them in the app. Reading that file
naively means reporting a question you settled last Tuesday as blocking, every
poll, forever, at the one urgency level exempt from the budget.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

TMP = use_temp_config()

from friday import agents  # noqa: E402
from friday.conversation import Friday  # noqa: E402

ROOT = TMP / "brain"
agents.Antigravity.ROOT = ROOT

PLAN = """# Hero Section Enhancements

I will integrate the new image and enrich the Hero section.

## Open Questions

> [!QUESTION]
> 1. Do you like the generated avatar, or would you prefer another style?
> 2. Should the heading animate, or stay static?

## Proposed Changes
Some prose about what it is going to do.
"""

TASK_PART = "- `[x]` Update config.\n- `[x]` Enhance Hero.\n- `[ ]` Run build.\n"
TASK_DONE = "- `[x]` Update config.\n- `[x]` Enhance Hero.\n- `[x]` Run build.\n"


def _convo(name, plan=PLAN, task=TASK_PART, walk="", summary="",
           plan_newest=True, age=0.0):
    d = ROOT / name
    d.mkdir(parents=True, exist_ok=True)
    now = time.time() - age
    (d / "task.md").write_text(task)
    if walk:
        (d / "walkthrough.md").write_text(walk)
    if summary:
        (d / "task.md.metadata.json").write_text(json.dumps(
            {"summary": summary, "userFacing": True}))
    (d / "implementation_plan.md").write_text(plan)
    # Whether the plan is the newest artifact is the entire question of whether
    # its open questions are still open.
    older, newer = now - 60, now
    for f, when in (("implementation_plan.md", newer if plan_newest else older),
                    ("task.md", older if plan_newest else newer)):
        import os
        os.utime(d / f, (when, when))
    if walk:
        import os
        w = newer if not plan_newest else older
        os.utime(d / "walkthrough.md", (w, w))
    return d


def _only(name):
    for d in list(ROOT.glob("*")):
        if d.name != name:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
    return next(r for r in agents.Antigravity().sessions() if r["sid"] == name)


# ---- the thing no other vendor can tell you --------------------------------
def test_it_knows_how_far_through_it_is():
    """A transcript tells you what an agent said. A checklist tells you how far
    through it is, which is what you meant when you asked what it was doing."""
    _convo("c1", summary="Hero section work")
    r = _only("c1")
    assert "2 of 3 done" in r["topic"], r["topic"]


def test_it_uses_the_name_the_thread_has():
    """The directory is a UUID, and a UUID is not something you can say out
    loud to a thing you talk to."""
    _convo("c2", summary="Hero section work")
    assert _only("c2")["label"] == "Hero section work"


def test_a_thread_with_no_summary_still_gets_a_name():
    _convo("c3")
    assert _only("c3")["label"] == "Hero Section Enhancements"


# ---- the stale-question trap ----------------------------------------------
def test_an_open_question_blocks():
    _convo("c4", plan_newest=True)
    r = _only("c4")
    assert r["status"] == "needs", r["status"]
    assert "avatar" in r["question"], r["question"]


def test_a_question_you_already_answered_does_not_block_forever():
    """The questions stay in the file after you answer them in the app. If the
    checklist has been written since the plan, work moved on and so did they."""
    _convo("c5", plan_newest=False)
    r = _only("c5")
    assert r["status"] != "needs", "reported a settled question as blocking"
    assert not r["question"], r["question"]


def test_a_finished_thread_asks_nothing():
    _convo("c6", task=TASK_DONE, plan_newest=False, walk="All done.")
    r = _only("c6")
    assert r["status"] == "idle" and not r["question"]


# ---- read, but not conducted ----------------------------------------------
def test_friday_admits_it_cannot_type_into_it():
    """The worst failure available to Friday is a confident "sent" for a message
    that went nowhere, found out hours later."""
    _convo("c7", summary="Portfolio work")
    row = _only("c7")
    assert agents.can_conduct(row) is False
    f = Friday()
    f.announce = lambda *a, **k: None
    f._find_how = lambda name: (row, "exact")
    reply = f.handle("tell portfolio work to run the tests")["reply"].lower()
    assert "can't type into it" in reply or "cannot type into it" in reply, reply
    assert "sent" not in reply, reply


def test_a_terminal_agent_is_still_conducted():
    """The gate must be about this vendor, not about every vendor."""
    assert agents.can_conduct({"vendor": "claude"}) is True
    assert agents.can_conduct({"vendor": "codex"}) is True


# ---- housekeeping ----------------------------------------------------------
def test_an_old_conversation_is_not_part_of_your_fleet():
    """Antigravity keeps every conversation forever. A fleet is what is live
    right now, not an archive."""
    _convo("c8", age=30 * 86400)
    assert not any(r["sid"] == "c8" for r in agents.Antigravity().sessions())


def test_it_reads_the_prose_and_not_the_furniture():
    """Headings, images and blockquote markers are layout, and reading layout
    aloud is how a summary becomes noise."""
    _convo("c9", walk="# Heading\n\n![img](x.png)\n\nI updated the hero.\n")
    said = agents.Antigravity().last_said(_only("c9"))
    assert said.startswith("I updated the hero"), said


def test_a_machine_without_antigravity_is_not_an_error():
    agents.Antigravity.ROOT = TMP / "definitely-not-here"
    try:
        assert agents.Antigravity().sessions() == []
        assert "antigravity" not in [v.name for v in agents.available()]
    finally:
        agents.Antigravity.ROOT = ROOT


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  antigravity: read honestly, and never pretended to be typed into")
