"""A session that is not running is not a session that does not exist.

Asked to open promptguard and give it a task, Friday said "I don't have a session
called prompt" while holding, on disk, every conversation with that project. Two
separate failures: only running sessions counted as targets, so the name could
not even be parsed and was cut at the space, and a closed project was reported
as unknown rather than as reopenable.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import actions, conversation as C, fleetcache, memory  # noqa: E402
from friday.conversation import Friday  # noqa: E402


def _project(name: str, sessions: int = 1, cwd: str = None) -> str:
    """A project directory on disk, named the way Claude names them."""
    cwd = cwd or f"/Users/someone/{name}"
    d = memory.PROJECTS / cwd.replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    for i in range(sessions):
        p = d / f"sid{name}{i}.jsonl"
        p.write_text(json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": f"working on {name} things"}]}}) + "\n")
    return f"sid{name}0"


class _Fleet:
    rows = {}

    @classmethod
    def snapshot(cls):
        return dict(cls.rows)


def _friday(running=None):
    _Fleet.rows = running or {}
    C.engine.AVAILABLE = True
    C.engine.fleet = _Fleet
    fleetcache.engine = C.engine
    fleetcache.TTL = 0
    fleetcache.bust()
    f = Friday()
    f.announce = lambda text, items=None: None
    return f


def test_a_multi_word_project_name_is_recognised_at_all():
    """"prompt guard" is what dictation produces for promptguard. Only running
    sessions were candidates, so the name was cut at the space and "prompt" was
    looked up instead."""
    _project("promptguard")
    f = _friday()
    assert "promptguard" in f._target_names()
    name, msg, _w, _e = f._resplit("ask prompt guard to fix my resume",
                                   "prompt", "guard to fix my resume", True)
    assert name == "promptguard", name
    assert msg == "fix my resume", msg


def test_a_closed_project_is_offered_for_reopening_with_your_message():
    """It is on disk and `claude --resume` brings it back, so "I don't have a
    session called that" is withholding the answer."""
    _project("promptguard")
    f = _friday()
    r = f.handle("ask promptguard to look at my resume")
    low = r["reply"].lower()
    assert "isn't running" in low and "reopen" in low, r["reply"]
    assert f._offered, "no offer was remembered, so yes would mean nothing"


def test_saying_yes_reopens_it_and_waits_before_typing():
    """The window takes seconds to exist. Typing immediately types into nothing
    and reports success."""
    sid = _project("promptguard")
    f = _friday()
    f.handle("ask promptguard to look at my resume")
    told = []
    f.announce = lambda text, items=None: told.append(text)
    actions.attempted.clear()
    r = f.handle("yes")
    assert "reopening" in r["reply"].lower(), r["reply"]
    kinds = [a for a, _args in actions.attempted]
    assert "resume" in kinds, f"never tried to reopen it: {kinds}"
    # nothing is sent while the session is absent from the fleet
    time.sleep(1.4)
    assert not [a for a, _args in actions.attempted if a == "send"], \
        "typed into a session that was not up yet"


def test_a_project_folder_with_no_conversations_says_exactly_that():
    """A directory can exist with nothing in it. "promptguard has no
    conversations" is a different answer from "there is no promptguard", and only
    one of them tells you what to do next."""
    d = memory.PROJECTS / "-Users-someone-wibble"
    d.mkdir(parents=True, exist_ok=True)
    _project("jobhunt", sessions=3)
    f = _friday()
    r = f.handle("ask wibble to look at my resume")
    low = r["reply"].lower()
    assert "no conversations" in low, r["reply"]
    # jobhunt is not a plausible reading of "wibble", so it must not be
    # offered as one. Naming what DOES have history is the useful answer.
    assert "did you mean" not in low, r["reply"]
    assert "jobhunt" in low, "did not say which projects it does have"


def test_a_running_session_still_wins_over_an_old_project():
    """If it is up, talk to it: reopening a second copy of the same work is the
    wrong answer."""
    _project("api", sessions=2)
    f = _friday({"s1": {"sid": "s1", "label": "api", "status": "idle",
                        "path": "", "question": "", "topic": ""}})
    actions.attempted.clear()
    r = f.handle("tell api to run the tests")
    assert "reopen" not in r["reply"].lower(), r["reply"]
    assert [a for a, _ in actions.attempted if a == "send"], actions.attempted


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  closed sessions: findable, reopenable, and never typed into early")
