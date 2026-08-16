"""What you actually care about, learned from what you do about it.

The pitch said the product is the attention model, and knowing when NOT to speak
is the whole difficulty. What was built is a good rule table: urgency tiers, a
token bucket, a settle delay. Rules are the right default and they are also
fixed. Friday interrupts you about a repo you stopped caring about in March
exactly as eagerly on day ninety as on day one, and the only lever you have is
muting the whole source, which is a sledgehammer.

So: one number per thing, moved by what you did after being told about it.

    saw(key)      Friday said something about this
    acted(key)    you did something about it, soon after
    score(key)    -1 you never act on this, +1 you always do
    why(key)      the sentence explaining it, in counts

Three properties matter more than the accuracy of the number.

**It can defer, never delete.** A low score costs an item its place in the
budget, so it waits for "what did I miss" instead of interrupting. Nothing is
ever dropped. A learned model that silently discards is one you cannot trust
after the first thing you did not hear about.

**It cannot touch urgency 0.** An agent that cannot continue without you is not
a preference. If Friday ever learns its way out of telling you about a blocked
agent, the feature has eaten the product.

**It can always explain itself.** `why()` returns counts, not a vibe, because
the answer to "why didn't you tell me" has to be something you can disagree
with. Scores are counts of two things, which is also why this is not a model:
you can read the whole state in a text file and delete a line.
"""

import json
import threading
import time

from . import connectors

FILE = "attention.json"
# How much history counts. Older than a fortnight and you have probably changed
# what you are working on, which is exactly when a learned preference goes from
# helpful to baffling.
HALF_LIFE = 14 * 86400
# Below this many tellings, there is no opinion. Two ignored notifications is
# not evidence, it is Tuesday.
MIN_SEEN = 4
# How far a score may move an item. One tier: something boring becomes something
# you read later. It can never make an item urgent, and never silence one.
MAX_SHIFT = 1

_lock = threading.Lock()
_cache = {"at": 0.0, "data": None}


def _path():
    return connectors.CONF_DIR / FILE


def _load() -> dict:
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < 2.0:
        return _cache["data"]
    try:
        data = json.loads(_path().read_text())
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    _cache["at"], _cache["data"] = now, data
    return data


def _save(data: dict) -> None:
    _cache["at"], _cache["data"] = time.time(), data
    try:
        connectors.CONF_DIR.mkdir(parents=True, exist_ok=True)
        p = _path()
        p.write_text(json.dumps(data, indent=1, sort_keys=True))
        p.chmod(0o600)
    except Exception:
        pass


def _decay(row: dict, now: float) -> dict:
    """Age both counts together, so the ratio survives and the weight fades."""
    since = now - (row.get("at") or now)
    if since <= 0:
        return row
    keep = 0.5 ** (since / HALF_LIFE)
    return {"saw": (row.get("saw", 0.0)) * keep,
            "acted": (row.get("acted", 0.0)) * keep,
            "at": now}


def key_for(item: dict) -> str:
    """The thing a score belongs to.

    The label rather than the session id, on purpose: session ids change every
    time you restart an agent, and what you care about is the project, not the
    process. A source name for feeds, a channel for Slack, a person for a
    message from one."""
    if not isinstance(item, dict):
        return ""
    for field in ("label", "source", "who", "channel"):
        got = (item.get(field) or "").strip()
        if got:
            return f"{item.get('kind') or 'thing'}:{got.lower()}"[:80]
    return ""


def _bump(key: str, field: str, by: float = 1.0) -> None:
    if not key:
        return
    with _lock:
        data = _load()
        now = time.time()
        row = _decay(data.get(key) or {"saw": 0.0, "acted": 0.0, "at": now}, now)
        row[field] = row.get(field, 0.0) + by
        data[key] = row
        _save(data)


def saw(key: str) -> None:
    """Friday told you about this."""
    _bump(key, "saw")


def acted(key: str) -> None:
    """You did something about it. Worth more than one telling is worth against
    it, because acting is a deliberate signal and not acting is mostly noise:
    you were in a meeting, the screen was off, you saw it and it was fine."""
    _bump(key, "acted", 2.0)


def never_again(key: str) -> None:
    """You muted it. The strongest signal available and the only one you give on
    purpose, so it counts for more than a run of ignored notifications."""
    _bump(key, "saw", float(MIN_SEEN) * 2)


def score(key: str) -> float:
    """-1 you never act on these, +1 you always do, 0 no opinion yet."""
    if not key:
        return 0.0
    row = _load().get(key)
    if not row:
        return 0.0
    row = _decay(row, time.time())
    seen = row.get("saw", 0.0)
    if seen < MIN_SEEN:
        return 0.0
    rate = min(1.0, row.get("acted", 0.0) / max(seen, 1.0))
    # Half is the neutral point: acting on half of what you are told is
    # neither a complaint nor an endorsement.
    return max(-1.0, min(1.0, (rate - 0.5) * 2.0))


def adjust(urgency: int, key: str) -> int:
    """What urgency this should really be, given what you do about it.

    Only ever downward, and only by one tier. Learning something UP would mean
    Friday deciding on its own to interrupt more, which is the direction nobody
    wants a mistake in. Urgency 0 is untouchable: an agent that cannot continue
    without you is not a preference."""
    if urgency <= 0:
        return urgency
    if score(key) > -0.5:
        return urgency
    return min(urgency + MAX_SHIFT, 2)


def why(key: str) -> str:
    """The sentence behind the number, in counts you can argue with."""
    row = _load().get(key)
    if not row:
        return ""
    row = _decay(row, time.time())
    seen, did = int(round(row.get("saw", 0.0))), int(round(row.get("acted", 0.0)))
    if seen < MIN_SEEN:
        return f"I've only mentioned it {seen} times, so I have no opinion yet."
    return (f"I've mentioned it about {seen} times and you did something about "
            f"it about {did // 2} of those.")


def forget(key: str = "") -> None:
    """Undo the learning, for one thing or all of it.

    Present because a learned preference you cannot clear is a bug you cannot
    fix: the day Friday decides wrongly that you do not care about something,
    the only acceptable answer is a way to say otherwise."""
    with _lock:
        if not key:
            _save({})
            return
        data = _load()
        data.pop(key, None)
        _save(data)


def summary(limit: int = 6) -> list:
    """What Friday thinks it has learned, strongest opinions first."""
    now = time.time()
    rows = []
    for key, row in (_load() or {}).items():
        row = _decay(row, now)
        if row.get("saw", 0.0) < MIN_SEEN:
            continue
        rows.append((abs(score(key)), key, score(key), row))
    rows.sort(reverse=True)
    return [{"key": k, "score": round(sc, 2), "why": why(k)}
            for _w, k, sc, _r in rows[:limit]]
