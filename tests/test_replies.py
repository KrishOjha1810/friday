"""The other half of talking to an agent: hearing back.

Friday could send an instruction and say "Sent it." The answer then appeared in
a terminal you were not looking at, so you still had to go to the window, which
is the thing Friday exists to save you from.
"""

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()   # never touch the real ~/.friday: a test once
                    # deleted a live Slack token this way

from friday import replies  # noqa: E402


def _write(path, msgs):
    with open(path, "w") as f:
        for role, text in msgs:
            f.write(json.dumps({"type": role, "timestamp": "t",
                                "message": {"content": [
                                    {"type": "text", "text": text}]}}) + "\n")


def test_an_answer_is_told_apart_from_what_was_said_before():
    """Without marking where the transcript ended, the agent's previous turn
    reads as its answer, and Friday reports something from ten minutes ago."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        _write(p, [("user", "hi"), ("assistant", "an older answer")])
        mark = replies.mark(str(p))
        assert mark.startswith("an older answer")
        got = replies.wait_for_reply(str(p), mark, timeout=1.2, settle=0.2)
        assert got == "", "reported an old message as the reply: " + got


def test_the_reply_comes_back_once_it_stops_changing():
    """An agent answers in stages: it says what it is about to do, does it, then
    reports. Returning the first stage gives you 'Let me look' instead of the
    answer."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        _write(p, [("assistant", "older")])
        mark = replies.mark(str(p))

        def later():
            time.sleep(0.3)
            _write(p, [("assistant", "older"), ("assistant", "Let me look")])
            time.sleep(0.5)
            _write(p, [("assistant", "older"), ("assistant", "Let me look"),
                       ("assistant", "Three files changed: a, b, c")])
        threading.Thread(target=later, daemon=True).start()
        got = replies.wait_for_reply(str(p), mark, timeout=8, settle=0.9)
        assert got == "Three files changed: a, b, c", got


def test_the_question_is_never_reported_as_the_answer():
    """Friday types a prompt INTO the session, so that prompt is the newest
    thing in the transcript. Reading the last message of any role reported it
    straight back: "voicebridge answered: what are the future plans of Friday?",
    which is the question."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        _write(p, [("assistant", "an earlier answer"),
                   ("user", "what are the future plans of Friday?")])
        assert replies.last_said(str(p)) == "an earlier answer"
        assert replies.mark(str(p)).startswith("an earlier answer")
        got = replies.wait_for_reply(str(p), replies.mark(str(p)),
                                     timeout=1.0, settle=0.2)
        assert got == "", "reported the prompt as a reply: " + got


def test_silence_is_reported_as_silence():
    """A session that never answers must not leave Friday waiting forever, and
    must not produce an invented summary either."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        _write(p, [("assistant", "older")])
        got = replies.wait_for_reply(str(p), replies.mark(str(p)),
                                     timeout=1.0, settle=0.2)
        assert got == "", got


def test_tool_noise_is_not_mistaken_for_an_answer():
    """Most of a turn is tool calls. Only what the agent actually says counts."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        with open(p, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
            ]}}) + "\n")
        assert replies.mark(str(p)) == "", "a tool call was read as speech"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  replies: answers come back, old ones do not")
