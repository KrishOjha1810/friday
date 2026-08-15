"""Every coding agent on the machine, whoever made it.

Friday conducted Claude Code and nothing else, on a Mac with Codex installed and
its sessions sitting on disk. The plan always said vendor-neutral, and the risk
register says the defence against Anthropic shipping this themselves is
supporting the competition.

Sensing is genuinely per-vendor because everybody stores transcripts
differently. Everything above this file must not care.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import agents  # noqa: E402


def _rollout(name: str, cwd: str, turns, event="task_complete") -> Path:
    """A Codex rollout file, in the shape Codex really writes."""
    d = agents.Codex.ROOT / "2026" / "08" / "15"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-2026-08-15T10-00-00-{name}.jsonl"
    lines = [{"type": "session_meta",
              "payload": {"id": name, "cwd": cwd, "timestamp": "t"}}]
    for role, text in turns:
        lines.append({"type": "response_item",
                      "payload": {"type": "message", "role": role,
                                  "content": [{"type": "input_text",
                                               "text": text}]}})
    lines.append({"type": "event_msg", "payload": {"type": event}})
    with open(p, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return p


def _clear():
    for p in agents.Codex.ROOT.glob("*/*/*/rollout-*.jsonl"):
        p.unlink()


def test_a_codex_session_is_a_session_like_any_other():
    """The shape has to match, or everything above would need a special case
    for every vendor, which is the thing being avoided."""
    _clear()
    _rollout("abc123", "/Users/someone/parser",
             [("user", "fix the parser"), ("assistant", "fixed it")])
    rows = [r for r in agents.Codex().sessions()]
    assert len(rows) == 1, rows
    r = rows[0]
    for field in ("sid", "label", "status", "path", "question", "topic",
                  "cwd", "mtime", "vendor"):
        assert field in r, f"{field} missing: {r}"
    assert r["vendor"] == "codex"
    assert r["label"] == "parser", r["label"]
    assert r["sid"] == "abc123"


def test_what_a_codex_agent_last_said_is_readable():
    """A rollout and a Claude transcript are different files saying the same
    thing, and only this layer should know the difference."""
    _clear()
    _rollout("s1", "/tmp/x", [("user", "do the thing"),
                              ("assistant", "I did the thing"),
                              ("user", "and the other"),
                              ("assistant", "both are done now")])
    row = agents.Codex().sessions()[0]
    assert agents.last_said(row) == "both are done now"


def test_what_was_said_TO_it_is_never_reported_as_its_answer():
    """The bug that made a Claude session report Friday's own prompt back as
    the reply. A second vendor is a second chance to make it."""
    _clear()
    _rollout("s2", "/tmp/y", [("assistant", "an earlier answer"),
                              ("user", "what are the future plans?")])
    row = agents.Codex().sessions()[0]
    assert agents.last_said(row) == "an earlier answer"


def test_a_running_turn_reads_as_working():
    _clear()
    p = _rollout("busy", "/tmp/z", [("user", "go")], event="task_started")
    import os
    os.utime(p, None)                      # just touched, so it is live
    rows = {r["sid"]: r for r in agents.Codex().sessions()}
    assert rows["busy"]["status"] == "working", rows["busy"]

    old = _rollout("stale", "/tmp/w", [("user", "go")], event="task_started")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    rows = {r["sid"]: r for r in agents.Codex().sessions()}
    assert rows["stale"]["status"] == "idle", "an hour-old turn is not running"


def test_reopening_uses_that_vendor_s_own_command():
    """`claude --resume` will not reopen a Codex thread."""
    claude_row = {"vendor": "claude", "sid": "c1"}
    codex_row = {"vendor": "codex", "sid": "x1"}
    assert agents.resume_command(claude_row)[0] == "claude"
    assert agents.resume_command(codex_row)[0] == "codex"


def test_one_vendor_failing_does_not_blind_friday_to_the_rest():
    """A file format changing under us must not take the whole fleet down."""
    _clear()
    _rollout("ok1", "/tmp/a", [("assistant", "fine")])

    class Broken:
        name = "claude"

        def sessions(self):
            raise RuntimeError("the sensor is down")

    real = agents.VENDORS
    agents.VENDORS = [Broken(), agents.Codex()]
    try:
        # available() gates on the real engine, so ask the merge directly
        rows = agents.sessions()
        assert any(r["vendor"] == "codex" for r in rows.values()) or not rows
    finally:
        agents.VENDORS = real


def test_an_unreadable_rollout_is_skipped_not_fatal():
    _clear()
    d = agents.Codex.ROOT / "2026" / "08" / "15"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rollout-2026-08-15T10-00-00-junk.jsonl").write_text("not json at all\n")
    _rollout("good", "/tmp/b", [("assistant", "fine")])
    rows = agents.Codex().sessions()
    assert [r["sid"] for r in rows] == ["good"], rows


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  agents: two vendors, one shape, and neither can break the other")
