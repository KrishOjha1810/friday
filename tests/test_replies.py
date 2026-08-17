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
        # The marker carries the position as well as the words now: an agent
        # that answers "Done." twice was invisible the second time, and a short
        # confirmation is the most common reply there is.
        assert "an older answer" in mark
        assert mark.split("|")[0].isdigit(), mark
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
        assert "an earlier answer" in replies.mark(str(p))
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
        # The marker carries a position now, so "nothing was said" is an empty
        # text half rather than an empty string.
        assert replies.last_said(str(p)) == "", "a tool call was read as speech"
        assert replies.mark(str(p)).endswith("|"), replies.mark(str(p))


def test_the_same_words_twice_is_still_a_new_reply():
    """"Done.", "Yes.", "All tests pass." Comparing text meant the second one
    was reported as silence, after waiting the full timeout."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        _write(p, [("assistant", "Done.")])
        mark = replies.mark(str(p))
        _write(p, [("assistant", "Done."), ("assistant", "Done.")])
        got = replies.wait_for_reply(str(p), mark, timeout=4, settle=0.2)
        assert got == "Done.", repr(got)


def test_fridays_own_prompt_is_not_the_agent_answering():
    """The count was taken over a 200KB tail, so appending Friday's prompt
    pushed an old message out of the window and the count went DOWN. Any check
    for "the count changed" then fired on Friday's own writing, and a whole
    multi-step plan could complete against an agent that never worked."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "big.jsonl"
        # Deliberately past the tail window, whatever it is set to: the point
        # of the count is that it does not depend on a window at all.
        with open(p, "w") as fh:
            i = 0
            while fh.tell() < replies.TAIL_BYTES + 50_000:
                fh.write(json.dumps({"type": "assistant", "message": {
                    "content": [{"type": "text",
                                 "text": f"reply {i} " + "x" * 600}]}}) + "\n")
                i += 1
        assert p.stat().st_size > replies.TAIL_BYTES, "not past the tail window"
        before = replies.tally(str(p))
        assert before > 300, before
        with open(p, "a") as fh:
            fh.write(json.dumps({"type": "user", "message": {
                "content": "do the thing"}}) + "\n")
        assert replies.tally(str(p)) == before, "Friday's own prompt moved it"
        with open(p, "a") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": "Done."}]}}) + "\n")
        assert replies.tally(str(p)) == before + 1


def test_an_old_style_marker_that_looks_like_a_number():
    """A pre-fix marker is plain text. One whose text happened to be digits
    parsed as a count, so the reply already on screen came back as the answer
    to the new question."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        _write(p, [("assistant", "2")])
        got = replies.wait_for_reply(str(p), "2", timeout=1.0, settle=0.2)
        assert got == "", repr(got)


def test_an_agent_quoting_a_transcript_is_not_two_turns():
    """The count is a byte scan, and coding agents explain file formats all
    day. It holds because JSON escapes the inner quotes, which is load-bearing
    rather than lucky: a looser pattern would count every explanation of the
    transcript format as a turn the agent took."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        body = ('Here is the shape: {"type": "assistant", "message": {}} '
                'and also {"role":"assistant"}')
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "user",
                                 "message": {"content": body}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": body}]}}) + "\n")
        assert replies.tally(str(p)) == 1, replies.tally(str(p))


def test_a_reply_buried_under_a_huge_tool_result_is_still_found():
    """A large tool result following the answer pushed it out of the read
    window, so the count went DOWN and the text came back empty. A count that
    falls is indistinguishable from one that never rose, so the step timed out
    and Friday reported silence about work that was finished."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"

        def said(t):
            return json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": t}]}})

        p.write_text(said("The migration is complete.") + "\n")
        before = replies.spoke(str(p))
        with open(p, "a") as fh:
            fh.write(said("Second answer.") + "\n")
            fh.write(json.dumps({"type": "user", "message": {
                "content": "tool result " + "z" * (replies.TAIL_BYTES + 500_000)
            }}) + "\n")
        assert replies.spoke(str(p)) == before + 1, replies.spoke(str(p))
        assert replies.last_said(str(p)) == "Second answer."


def test_a_tool_call_is_not_a_spoken_turn():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": "Done."}]}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Bash",
                             "input": {}}]}}) + "\n")
        assert replies.spoke(str(p)) == 1, replies.spoke(str(p))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  replies: answers come back, old ones do not")
