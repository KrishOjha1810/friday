"""Reporting what every session says, without becoming noise.

The failure mode of a thing like this is not missing a message, it is saying too
much: read out everything already on screen at startup, or repeat the same reply
every three seconds, or interrupt with "Let me look at that" before the answer
exists. Any one of those and the feature gets muted, which is worse than not
having it.
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

from friday import fleetcache, watchtower  # noqa: E402

fleetcache.TTL = 0            # tests change the fleet between ticks

watchtower.SETTLE = 0.4


class _Fleet:
    rows = {}

    @classmethod
    def snapshot(cls):
        return dict(cls.rows)


class _Att:
    quiet = False

    @classmethod
    def is_quiet(cls):
        return cls.quiet


class _Engine:
    AVAILABLE = True
    fleet = _Fleet
    attention = _Att

    class brain:
        @staticmethod
        def up():
            return False


watchtower.engine = None      # replaced below; never the real one in tests
fleetcache.engine = None


def _session(d, sid, label, text, question=""):
    p = Path(d) / (sid + ".jsonl")
    with open(p, "w") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": text}]}}) + "\n")
    _Fleet.rows[sid] = {"sid": sid, "label": label, "path": str(p),
                        "status": "working", "question": question}
    return p


def _tower():
    watchtower.engine = _Engine
    fleetcache.engine = _Engine
    said = []
    w = watchtower.Watchtower(lambda text, items=None: said.append((text, items)))
    return w, said


def test_what_was_already_on_screen_is_not_read_out():
    """Starting Friday must not recite the last thing every session happened to
    say. That is a wall of text about work you already know about."""
    _Fleet.rows.clear()
    with tempfile.TemporaryDirectory() as d:
        _session(d, "s1", "api", "something from before")
        w, said = _tower()
        w.prime()
        w._tick()
        assert said == [], said


def test_a_new_reply_is_reported_exactly_once():
    """Polling means seeing the same message over and over. Saying it each time
    is how a useful feature becomes one you turn off."""
    _Fleet.rows.clear()
    with tempfile.TemporaryDirectory() as d:
        p = _session(d, "s1", "api", "old")
        w, said = _tower()
        w.prime()
        with open(p, "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "the migration is done"}]}}) + "\n")
        w._tick()                      # noticed, not settled yet
        assert said == [], "spoke before the agent had finished"
        time.sleep(watchtower.SETTLE + 0.1)
        w._tick()
        assert len(said) == 1, said
        assert "the migration is done" in said[0][0]
        w._tick()
        w._tick()
        assert len(said) == 1, "said the same thing twice"


def test_an_agent_still_talking_is_not_interrupted():
    """An answer arrives in stages. Reporting the first one means reporting 'Let
    me look at that' instead of the answer."""
    _Fleet.rows.clear()
    with tempfile.TemporaryDirectory() as d:
        p = _session(d, "s1", "api", "old")
        w, said = _tower()
        w.prime()
        for chunk in ("Let me look", "Let me look. Checking the tests"):
            with open(p, "w") as f:
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": chunk}]}}) + "\n")
            w._tick()
            time.sleep(watchtower.SETTLE * 0.6)
            w._tick()
        assert said == [], "reported a half-finished answer: " + str(said)


def test_a_session_waiting_on_you_is_reported_first():
    """Two agents speak at once: the one that cannot continue without you
    matters more than the one that merely finished a thought."""
    _Fleet.rows.clear()
    with tempfile.TemporaryDirectory() as d:
        p1 = _session(d, "s1", "api", "old")
        p2 = _session(d, "s2", "jobhunt", "old", question="Drop the parser?")
        w, said = _tower()
        w.prime()
        for p, text in ((p1, "finished the refactor"), (p2, "two paths disagree")):
            with open(p, "w") as f:
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": text}]}}) + "\n")
        w._tick()
        time.sleep(watchtower.SETTLE + 0.1)
        w._tick()
        assert len(said) == 2, said
        assert said[0][0].startswith("jobhunt"), [s[0] for s in said]
        assert "Drop the parser?" in said[0][0], said[0][0]


def test_quiet_means_quiet():
    """"Quiet" has to cover this too, or the one command for making it stop
    stops working."""
    _Fleet.rows.clear()
    with tempfile.TemporaryDirectory() as d:
        p = _session(d, "s1", "api", "old")
        w, said = _tower()
        w.prime()
        with open(p, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "done"}]}}) + "\n")
        w._tick()
        _Att.quiet = True
        try:
            time.sleep(watchtower.SETTLE + 0.1)
            w._tick()
            assert said == [], said
        finally:
            _Att.quiet = False


def test_a_reply_carries_which_session_it_came_from():
    """A report you cannot act on is half a report: answering it has to reach
    the agent that said it."""
    _Fleet.rows.clear()
    with tempfile.TemporaryDirectory() as d:
        p = _session(d, "s7", "api", "old", question="Use redis?")
        w, said = _tower()
        w.prime()
        with open(p, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "cache is the bottleneck"}]}}) + "\n")
        w._tick()                          # notices it
        time.sleep(watchtower.SETTLE + 0.1)
        w._tick()                          # settled, so now it speaks
        assert said, "nothing reported"
        items = said[0][1]
        assert items and items[0]["sid"] == "s7", items
        assert items[0]["kind"] == "blocked", items


def test_a_detail_the_agent_never_mentioned_is_thrown_away():
    """Asked to compress "the parser broke on page 3 of the PDF", a small model
    produced "retry with the file named report_2024_q3.pdf ... error code
    PDF_PARSE_003". Both invented, and both exactly the kind of detail you would
    act on. Fabricated specifics are worse than no summary."""
    class Brain:
        TIMEOUT_SLOW = 5

        @staticmethod
        def up():
            return True

        @staticmethod
        def _chat(*a, **k):
            return ("Parser failed on page 3. Retry with report_2024_q3.pdf "
                    "and quote error code PDF_PARSE_003.")

        @staticmethod
        def _clean(t):
            return t

    class E:
        AVAILABLE = True
        brain = Brain

        class core:
            @staticmethod
            def log(*a):
                pass

    old, watchtower.engine = watchtower.engine, E
    try:
        src = "The parser broke on page 3 of the PDF. " * 20
        out = watchtower.summarise(src)
        assert "report_2024_q3.pdf" not in out, out
        assert "PDF_PARSE_003" not in out, out
        assert "parser broke on page 3" in out.lower(), out
    finally:
        watchtower.engine = old


def test_a_grounded_summary_is_kept():
    """The guard must not throw away a good summary: specifics that really are
    in the message are the whole point."""
    class Brain:
        TIMEOUT_SLOW = 5

        @staticmethod
        def up():
            return True

        @staticmethod
        def _chat(*a, **k):
            return "Fixed the crash in vb/core.py at line 812. Both tests pass."

        @staticmethod
        def _clean(t):
            return t

    class E:
        AVAILABLE = True
        brain = Brain

        class core:
            @staticmethod
            def log(*a):
                pass

    old, watchtower.engine = watchtower.engine, E
    try:
        src = ("I fixed the crash in vb/core.py at line 812 and both tests "
               "pass now. " * 6)
        out = watchtower.summarise(src)
        assert "vb/core.py" in out and "812" in out, out
    finally:
        watchtower.engine = old


def test_a_short_reply_is_passed_through_untouched():
    """Summarising 'All tests pass' can only make it worse."""
    watchtower.engine = _Engine
    assert watchtower.summarise("All tests pass.") == "All tests pass."


def test_a_long_reply_is_never_silently_swallowed():
    """With no model available it must still say something, and that something
    must come from the reply rather than being invented."""
    watchtower.engine = _Engine
    long = "The parser broke on page 3 of the PDF. " * 20
    out = watchtower.summarise(long)
    assert out and out != long
    assert "parser broke on page 3" in out, out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  watchtower: says it once, says it late enough, urgent first")
