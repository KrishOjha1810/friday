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
import math
import os
import re
import time
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
# Transcripts are read in FULL for searching. The old 400KB head-and-tail read
# covered 5.4% of the corpus: 26 of 49 sessions were over the cap and held 229
# of the 231 MB. It also quietly broke length normalisation, because every large
# session reported the same truncated length and so took the same penalty, which
# is exactly where the penalty needed to differ. A full pass costs about a
# second and is cached per file on its mtime.
PEEK_BYTES = 60_000               # only for "what is this session about"
MAX_FILES = 400                   # newest-first, so old noise falls off the end


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


def _texts(path: Path, limit: int = 0):
    """Human and assistant text from a transcript, bounded. Yields (role, text)."""
    try:
        with open(path, "r", errors="ignore") as f:
            # `limit` is only for callers that want a peek (what is this
            # session about). Searching reads everything.
            body = f.read(limit) if limit else f.read()
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


def projects() -> list:
    """[(name, directory)] for every project you have worked in.

    Claude stores a session under a directory named for its working directory,
    so the project name is on disk whether or not anything is running. This is
    how a closed session can be found by name at all: "ask promptguard to ..."
    used to fail simply because promptguard was not running, even though every
    conversation you ever had with it is right here."""
    out = []
    try:
        for d in PROJECTS.iterdir():
            if not d.is_dir():
                continue
            # "-Users-krishojha-Desktop-promptguard" is the cwd with slashes
            # turned into dashes. The last part is the name you would use.
            name = d.name.rstrip("-").rsplit("-", 1)[-1]
            if name:
                out.append((name, d))
    except Exception:
        return []
    return out


def project_names(with_sessions: bool = False) -> list:
    """Project names. With `with_sessions`, only those that have transcripts.

    The distinction matters: a directory can exist with nothing in it, and
    telling somebody "promptguard has no conversations" is a different answer
    from "there is no promptguard"."""
    out = []
    for name, d in projects():
        if with_sessions:
            try:
                if not any(d.glob("*.jsonl")):
                    continue
            except Exception:
                continue
        out.append(name)
    return out


def by_project(name: str, limit: int = 5) -> list:
    """Sessions from the project whose name matches, newest first."""
    from . import nearest
    rows = projects()
    if not rows:
        return []
    # Reopening the wrong project is a window full of somebody else's work, so
    # this needs a clear match rather than the closest one.
    picked = nearest.pick(name, [n for n, _d in rows], act=0.72)
    if not picked:
        return []
    out = []
    for n, d in rows:
        if n != picked:
            continue
        for f in d.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            about = ""
            for role, text in _texts(f, limit=60_000):
                if role == "user" and len(text) > 12 and not text.startswith("/"):
                    about = text[:110]
                    break
            out.append({"sid": f.stem, "path": str(f), "when": mtime,
                        "project": n, "cwd": _cwd_of(d), "about": about})
    out.sort(key=lambda h: -h["when"])
    return out[:limit]


def _cwd_of(d) -> str:
    """The working directory a project directory was named for."""
    name = d.name
    if not name.startswith("-"):
        return ""
    return "/" + name[1:].replace("-", "/")


# BM25, and the one number that decides everything.
#
# The old scoring counted how many distinct query words appeared, doubled for
# words in your own messages. Measured on 1,200 known-item queries built from
# these actual transcripts (pick a session, take a few words from something you
# really said, see where that session ranks) it scored 0.302 MRR. BM25 at b=1.0
# scores 0.380, about 26% better, and the gap is outside the confidence
# interval.
#
# b is length normalisation and it is the whole story. The published defaults
# are WRONG for this corpus: b=0.75 (Lucene) scores 0.334 and b=0.4 (Anserini)
# scores 0.287, which is worse than the thing it replaces. Sessions here differ
# in length by a factor of 685, so a very long one contains nearly every term by
# coincidence and has to be penalised in full. k1 barely matters: 0.5, 0.9 and
# 1.2 land within 0.003 of each other.
K1 = 0.9
B = 1.0
USER_WEIGHT = 2.0     # your own phrasing counts double

_index = {}           # path -> (mtime, size, {term: (yours, theirs)}, length)


def _terms(text: str) -> list:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9.\-/_]+", (text or "").lower())
            if w not in _STOP]


def _doc(path) -> tuple:
    """({term: (in_your_words, elsewhere)}, weighted length), cached on mtime.

    Transcripts only ever grow, so a cached parse stays good until the file
    changes size. Cold, the whole corpus costs about a second; warm, only what
    changed is reparsed."""
    try:
        st = path.stat()
    except OSError:
        return {}, 0.0
    hit = _index.get(str(path))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2], hit[3]
    counts, yours, theirs = {}, 0, 0
    for role, text in _texts(path):
        toks = _terms(text)
        mine = role == "user"
        if mine:
            yours += len(toks)
        else:
            theirs += len(toks)
        for t in toks:
            u, o = counts.get(t, (0, 0))
            counts[t] = (u + 1, o) if mine else (u, o + 1)
    dl = theirs + USER_WEIGHT * yours
    _index[str(path)] = (st.st_mtime, st.st_size, counts, dl)
    return counts, dl


def search(query: str, limit: int = 5) -> list:
    """Sessions matching `query`, best first, scored with BM25.

    Still explainable: which terms matched comes back with the hit, because the
    answer has to be defensible when Friday says "it was this one"."""
    q = (query or "").strip().lower()
    if len(q) < 3:
        return []
    words = _terms(q)
    if not words:
        return []
    docs = []
    for path in _iter_transcripts():
        counts, dl = _doc(path)
        if counts:
            docs.append((path, counts, dl))
    if not docs:
        return []
    n = len(docs)
    avgdl = (sum(d[2] for d in docs) / n) or 1.0
    df = {w: sum(1 for _p, c, _d in docs if w in c) for w in words}

    hits = []
    for path, counts, dl in docs:
        score, seen_any, seen_user = 0.0, set(), set()
        for w in words:
            u, o = counts.get(w, (0, 0))
            if not (u or o):
                continue
            seen_any.add(w)
            if u:
                seen_user.add(w)
            f = o + USER_WEIGHT * u
            # Lucene's IDF, which never goes negative. With 46 sessions the
            # textbook form turns negative for any term in more than half of
            # them, so a common word would count AGAINST a document.
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            score += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * dl / avgdl))
        if not score:
            continue
        first_user, best_line, phrase = "", "", False
        for role, text in _texts(path, limit=PEEK_BYTES):
            low = text.lower()
            if not first_user and role == "user" and len(text) > 12:
                first_user = text[:110]
            if not phrase and q in low:
                phrase, best_line = True, text[:160]
            elif not best_line and any(w in low for w in words):
                best_line = text[:160]
        if phrase:
            score *= 1.5          # you quoted it, which is a strong signal
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        hits.append({"sid": path.stem, "path": str(path), "score": score,
                     "when": mtime, "about": first_user, "snippet": best_line,
                     "matched": sorted(seen_any), "phrase": phrase,
                     "terms": len(words)})
    # Recency is a TIEBREAK, never a multiplier. As a multiplier it measured
    # WORSE at every half-life tried, because relevance already carries it.
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
