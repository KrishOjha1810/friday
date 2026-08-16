"""Wait for an agent to answer, and bring the answer back.

Friday could already send an instruction into a session, and then said "Sent
it." That is only half a conversation: you asked for a summary of the changes,
Friday delivered the question, and the answer appeared in a terminal you were
not looking at. To find out what it said you had to go to the window, which is
the thing Friday exists to save you from.

Every session writes its transcript to disk as it goes, one JSON object per
line, so the answer is readable without attaching to the process, without a
hook, and without the agent cooperating. This module notes where a transcript
ends, and reports the next thing the agent actually says.

It reads only the transcript path the fleet already reported for a session Friday
was asked to talk to. It never goes looking through anyone else's.
"""

import json
import os
import time

# How much of the tail to read. Transcripts run to many megabytes, and the only
# part that matters is the end.
TAIL_BYTES = 200_000


def _tail(path: str, limit: int = TAIL_BYTES) -> list:
    try:
        size = os.path.getsize(path)
        with open(path, "r", errors="ignore") as f:
            if size > limit:
                f.seek(size - limit)
                f.readline()          # discard the half line seeking landed in
            return f.read().splitlines()
    except Exception:
        return []


def _messages(path: str) -> list:
    """[(timestamp, role, text)] from the end of a transcript, in order."""
    out = []
    for line in _tail(path):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        role = rec.get("type")
        if role not in ("user", "assistant"):
            continue
        content = (rec.get("message") or {}).get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Only the spoken part. A turn is mostly tool calls, and reporting
            # "I'll check that" while the real answer is three tool results
            # later would be worse than saying nothing.
            text = " ".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
        text = " ".join(text.split())
        if text and not text.startswith("<"):
            out.append((rec.get("timestamp") or "", role, text))
    return out


def last_said(path: str) -> str:
    """The last thing the AGENT said, ignoring what was said TO it.

    Taking the last message of any role meant the prompt Friday had just typed
    in came straight back as the answer: "voicebridge answered: what are the
    future plans of Friday?", which is the question. Only the agent's own words
    can ever be a reply."""
    msgs = [m for m in _messages(path) if m[1] == "assistant"]
    return msgs[-1][2] if msgs else ""


def tally(path: str) -> int:
    """How many assistant messages the transcript holds.

    Position, not content. "Has it said something new" was answered by
    comparing text, so an agent replying "Done." twice looked silent the second
    time and the plan waited fifteen minutes for a reply it already had."""
    try:
        return len(_messages(path))
    except Exception:
        return 0


def mark(path: str) -> str:
    """Where the transcript is now, so a later reply can be told apart from an
    older one. Returns an opaque marker."""
    return last_said(path)[:200]


def wait_for_reply(path: str, marker: str, timeout: float = 120,
                   settle: float = 2.5) -> str:
    """The next thing the agent says after `marker`, or "" if it stays quiet.

    Waits for the text to STOP changing before returning: an agent answers in
    several turns (it says what it is about to do, then does it, then reports),
    and returning the first of those gives you 'Let me look' instead of the
    answer."""
    deadline = time.time() + timeout
    last, last_seen_at = "", 0.0
    while time.time() < deadline:
        msgs = [m for m in _messages(path) if m[1] == "assistant"]
        newest = msgs[-1][2] if msgs else ""
        if newest and newest[:200] != marker:
            if newest != last:
                last, last_seen_at = newest, time.time()
            elif time.time() - last_seen_at >= settle:
                return last
        time.sleep(0.8)
    return last
