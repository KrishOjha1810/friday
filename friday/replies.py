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
import re
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


# Marks an assistant turn, in either transcript dialect, without parsing.
#
# Safe against an agent QUOTING a transcript, which coding agents do constantly,
# because JSON escapes the inner quotes: a body containing {"type": "assistant"}
# is stored as {\"type\": \"assistant\"} and the pattern below does not match
# it. That is load-bearing. Loosening this to match bare `assistant` would
# count every explanation of the file format as a turn the agent took.
_SPOKE = re.compile(rb'"(?:type|role)"\s*:\s*"assistant"')


def tally(path: str) -> int:
    """How many times the agent has spoken, over the WHOLE file.

    Position, not content. "Has it said something new" was answered by
    comparing text, so an agent replying "Done." twice looked silent the second
    time and the plan waited fifteen minutes for a reply it already had.

    The whole file, not the tail, and this is the part that matters. Counting
    within a 200KB tail gives a number that goes DOWN when the file grows:
    Friday appends its own prompt, an old message falls out of the window, and
    the count drops. Any check for "the count changed" then fires immediately,
    on Friday's own writing, and a whole multi-step plan completed against an
    agent that never worked. On a long-lived session that is most sends.

    A byte scan rather than a parse, because this runs on every poll and the
    files run to many megabytes: counting a marker is milliseconds where
    decoding the JSON is not."""
    try:
        n = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    return n
                # Read whole lines, so a marker split across two chunks is not
                # missed and not counted twice.
                tail = b""
                if not chunk.endswith(b"\n"):
                    extra = f.readline()
                    chunk += extra
                n += len(_SPOKE.findall(chunk + tail))
    except Exception:
        return 0


def mark(path: str) -> str:
    """Where the transcript is now, so a later reply can be told apart from an
    older one. Returns an opaque marker.

    Position AND text. It was the first 200 characters of the last reply, so an
    agent that answered "Done." twice looked silent the second time: the most
    common reply there is was the case Friday reported as no reply at all. The
    count says something new arrived even when the words are identical; the
    text still matters because a file can be rewritten to the same length."""
    return f"{tally(path)}|{last_said(path)[:200]}"


def wait_for_reply(path: str, marker: str, timeout: float = 120,
                   settle: float = 2.5) -> str:
    """The next thing the agent says after `marker`, or "" if it stays quiet.

    Waits for the text to STOP changing before returning: an agent answers in
    several turns (it says what it is about to do, then does it, then reports),
    and returning the first of those gives you 'Let me look' instead of the
    answer."""
    deadline = time.time() + timeout
    was, sep, was_text = (marker or "").partition("|")
    # By the separator, not by whether the left half parses as a number. A
    # pre-fix marker whose text happened to be digits became a bogus count, and
    # the reply already on screen was handed back as the answer.
    if sep:
        try:
            before = int(was)
        except ValueError:
            before, was_text = -1, marker or ""
    else:
        before, was_text = -1, marker or ""
    last, last_seen_at = "", 0.0
    while time.time() < deadline:
        msgs = [m for m in _messages(path) if m[1] == "assistant"]
        newest = msgs[-1][2] if msgs else ""
        # GREATER, not different. A count that falls means the file was
        # rotated, and the words are the discriminator there; treating a fall
        # as a new reply handed back the previous one.
        moved = tally(path) > before if before >= 0 else False
        if newest and (moved or newest[:200] != was_text):
            if newest != last:
                last, last_seen_at = newest, time.time()
            elif time.time() - last_seen_at >= settle:
                return last
        time.sleep(0.8)
    return last
