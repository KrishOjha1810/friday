"""Friday's memory of your past work.

Running sessions are only the present tense. Most of what you want to ask is
about the past: "which session was I in when I set up the Redis thing", "find
the conversation about the login bug", "what was I doing yesterday".

Every Claude Code session leaves a transcript on disk, so the material is
already there. This module makes it searchable without loading gigabytes into
memory: it walks the transcript files newest-first, reads a bounded amount of
each, and scores matches. No index to build, no daemon to keep fresh, and it
degrades to "found nothing" rather than failing.

It only ever reads THIS user's transcripts. Another account's sessions are
visible as a count (see fleet.other_users) and never opened.
"""

import json
import os
import re
import time
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
MAX_BYTES_PER_FILE = 400_000      # bounded read: transcripts reach many MB
MAX_FILES = 200                   # newest-first, so old noise falls off the end


def _iter_transcripts(max_age_days: float = 60.0):
    """Transcript paths, newest first, bounded."""
    try:
        files = []
        cutoff = time.time() - max_age_days * 86400
        for p in PROJECTS.glob("*/*.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m >= cutoff:
                files.append((m, p))
        files.sort(reverse=True)
        return [p for _, p in files[:MAX_FILES]]
    except Exception:
        return []


def _texts(path: Path, limit: int = MAX_BYTES_PER_FILE):
    """Human and assistant text from a transcript, bounded. Yields (role, text)."""
    try:
        size = path.stat().st_size
        with open(path, "r", errors="ignore") as f:
            # For a big file read the HEAD (what the session is about) and the
            # TAIL (what it ended up doing); the middle is rarely what you are
            # searching for and is where all the bulk lives.
            if size > limit:
                head = f.read(limit // 2)
                f.seek(max(0, size - limit // 2))
                body = head + "\n" + f.read()
            else:
                body = f.read()
    except Exception:
        return
    for line in body.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
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
            text = " ".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
        text = " ".join(text.split())
        if text and not text.startswith("<"):
            yield role, text


def search(query: str, limit: int = 5) -> list:
    """Sessions matching `query`, best first.

    Scoring is deliberately simple and explainable: every query word that
    appears counts, words in what YOU said count double (you remember your own
    phrasing, not the assistant's), and a whole-phrase hit counts most. A
    mysterious relevance score would be worse than useless here, because the
    answer has to be defensible when Friday says "it was this one"."""
    q = (query or "").strip().lower()
    if len(q) < 3:
        return []
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9.\-]{2,}", q)
             if w not in _STOP]
    if not words:
        return []
    hits = []
    for path in _iter_transcripts():
        # COVERAGE, not frequency. Counting every occurrence let a long
        # unrelated session outrank the right one just by being wordy; what
        # actually matters is how many of your DISTINCT terms appear, and
        # whether they appear in your own words.
        seen_user, seen_any, phrase = set(), set(), False
        first_user, best_line = "", ""
        for role, text in _texts(path):
            low = text.lower()
            if not first_user and role == "user" and len(text) > 12:
                first_user = text[:110]
            if q in low:
                phrase = True
                if not best_line:
                    best_line = text[:160]
            for w in words:
                if w in low:
                    seen_any.add(w)
                    if role == "user":
                        seen_user.add(w)
                    if not best_line:
                        best_line = text[:160]
        # every distinct term found, doubled when it was YOUR phrasing, plus a
        # large bonus for the whole phrase, plus a bonus for covering it all
        score = len(seen_any) + len(seen_user) * 2 + (14 if phrase else 0)
        if seen_any and len(seen_any) == len(words):
            score += 6
        if score:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0
            hits.append({"sid": path.stem, "path": str(path), "score": score,
                         "when": mtime, "about": first_user,
                         "snippet": best_line})
    hits.sort(key=lambda h: (-h["score"], -h["when"]))
    return hits[:limit]


def recent(limit: int = 8) -> list:
    """The sessions you worked in most recently, with what each was about."""
    out = []
    for path in _iter_transcripts()[:limit]:
        about = ""
        for role, text in _texts(path, limit=60_000):
            if role == "user" and len(text) > 12 and not text.startswith("/"):
                about = text[:110]
                break
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        out.append({"sid": path.stem, "path": str(path),
                    "when": mtime, "about": about})
    return out


def ago(ts: float) -> str:
    d = max(0, time.time() - (ts or 0))
    if d < 3600:
        return f"{int(d // 60)} minutes ago"
    if d < 86400:
        h = int(d // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    days = int(d // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


_STOP = {"the", "and", "for", "with", "was", "were", "that", "this", "you",
         "your", "our", "can", "did", "does", "what", "when", "where", "which",
         "session", "sessions", "about", "find", "search", "made", "make",
         "have", "has", "had", "get", "got", "there", "were", "from", "into"}
