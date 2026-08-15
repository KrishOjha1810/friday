"""Tell the transcriber the names it is about to hear.

Every name Friday deals with is a proper noun, and proper nouns are what speech
recognition gets wrong: a channel called moonshot came back as "Munsheer",
"moon shot" and "moon of shot" on consecutive tries. `nearest.py` cleans that up
afterwards, and cleaning up afterwards is strictly worse than not mangling it in
the first place.

whisper takes an initial prompt that biases decoding toward a vocabulary, and
voicebridge already reads one from ~/.voicebridge/vocab. Nothing has ever
written that file. Friday is the one process that knows the actual names, your
sessions, your projects, your Slack channels, and it was keeping them to itself.

Two things decide whether this helps or hurts:

  Short. whisper's prompt window is about 224 tokens and overflow is dropped
  from the FRONT, so a long list silently truncates the very terms you added,
  and a list of unlikely words measurably degrades recognition of everything
  else. Twenty or thirty currently-plausible names beat a hundred stale ones.

  Current. A name that is not going to be said is a name competing with one
  that is, so this is rewritten from what is live rather than accumulated.
"""

import time
from pathlib import Path

from . import connectors, engine, fleetcache, memory

VOCAB = Path.home() / ".voicebridge" / "vocab"
MAX_NAMES = 24        # what fits under the token cliff beside the base prompt

# Names that are ordinary English carry no information for the decoder and cost
# a slot that a distinctive one needs. whisper already knows "general" and
# "test"; it does not know "voicebridge" or "promptguard", and those are the
# words it actually gets wrong.
_ORDINARY = {
    "general", "random", "test", "tests", "agent", "agents", "desktop",
    "documents", "downloads", "work", "personal", "main", "dev", "temp",
    "scratch", "misc", "notes", "todo", "inbox", "team", "help", "support",
    "announcements", "social", "new", "old", "backup", "archive", "public",
    "private", "home", "user", "users", "admin", "data", "code", "project",
    "projects", "session", "sessions", "chat", "channel", "channels",
}
MAX_CHARS = 420
REWRITE_EVERY = 120   # seconds; names do not change faster than this

_last = 0.0


def names() -> list:
    """The names most likely to be spoken next, best first.

    Ordered by how likely they are to come out of your mouth in the next
    minute: what is running now, then what you have worked in, then the
    channels you read. The list is cut at the end, so the order is the
    priority."""
    out, seen = [], set()

    def add(name):
        n = (name or "").strip()
        low = n.lower()
        # A one-word name that is ordinary English helps nothing and costs a
        # slot. A compound like "it-and-network" is fine: the decoder does not
        # know that one.
        if not n or low in seen or len(n) < 3:
            return
        if low in _ORDINARY:
            return
        # Slack's internal names for group DMs ("mpdm-a--b--c-1") are never
        # said out loud and would burn several slots each.
        if low.startswith(("mpdm-", "dm-")):
            return
        seen.add(low)
        out.append(n)

    try:
        for row in fleetcache.snapshot().values():
            add(row.get("label"))
    except Exception:
        pass
    try:
        for project in memory.project_names(with_sessions=True):
            add(project)
    except Exception:
        pass
    try:
        sl = connectors.get("slack")
        if sl and getattr(sl, "ready", lambda: False)():
            for ch in sl.channel_names(30):
                add(ch)
    except Exception:
        pass
    return out[:MAX_NAMES]


def line(items=None) -> str:
    """The sentence appended to whisper's prompt.

    A sentence, not a bare list: the research is consistent that natural
    phrasing biases better than a comma-separated dump, and voicebridge's own
    base prompt is already written that way."""
    items = names() if items is None else items
    if not items:
        return ""
    text = "Names that may be said: " + ", ".join(items) + "."
    while len(text) > MAX_CHARS and items:
        items = items[:-1]
        text = "Names that may be said: " + ", ".join(items) + "."
    return text


def write(force: bool = False) -> str:
    """Refresh the vocabulary file. Returns what was written, or "".

    Cheap enough to call often: it does nothing unless the names have actually
    changed, so a rewrite does not invalidate whisper's prompt cache for no
    reason."""
    global _last
    now = time.time()
    if not force and now - _last < REWRITE_EVERY:
        return ""
    _last = now
    text = line()
    if not text:
        return ""
    try:
        VOCAB.parent.mkdir(parents=True, exist_ok=True)
        if VOCAB.exists() and VOCAB.read_text().strip() == text:
            return text            # unchanged, so leave the file alone
        VOCAB.write_text(text + "\n")
        engine.log(f"friday: vocabulary for speech updated ({len(text)} chars)")
        return text
    except Exception as e:
        engine.log(f"friday vocab: {e}")
        return ""


def keep_fresh(period: float = REWRITE_EVERY) -> None:
    """Keep it current in the background, without ever blocking anything."""
    import threading

    def _loop():
        while True:
            try:
                write(force=True)
            except Exception:
                pass
            time.sleep(period)
    threading.Thread(target=_loop, daemon=True).start()
