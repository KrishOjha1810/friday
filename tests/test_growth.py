"""What Friday holds on to, and whether it ever lets go.

Every other suite here asks whether something is correct once. This one asks
whether it is still correct on hour nine, which is a different question and the
one with no evidence behind it: nobody has run Friday for a full day.

A short soak, with time compressed, against declared ceilings. The ceilings are
the real content: a number with no ceiling is a number that grows until the
machine notices, and the point of writing them down is that adding a new
per-session cache later fails here rather than in six weeks on somebody's Mac.

`tests/soak.py` is the same harness run longer, for when a number moves and you
want to watch it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import soak  # noqa: E402  (sets up its own sandbox)

from friday import feeds, watchtower  # noqa: E402


def test_nothing_exceeds_its_ceiling_under_churn():
    """The fleet churns the way a real one does: new session ids all day as you
    open agents, old ones leaving. That churn is the whole point. Anything keyed
    by session id looks bounded until you notice a working day makes dozens of
    ids and a month makes thousands."""
    got = soak.run(seconds=25.0, tick=0.03)
    assert not got["over"], got["over"]


def test_a_session_that_is_gone_is_eventually_forgotten():
    """Everything the watchtower keeps is keyed by session id and every value is
    text, so without this it becomes a log of every agent you have ever run,
    held in memory and scanned on every tick."""
    w = watchtower.Watchtower(announce=lambda *a, **k: None)
    w.seen = {"a": "x", "b": "y"}
    w.last = {"a": "long text", "b": "more"}
    w.pending = {"b": ("text", 0)}
    w.expecting = {"b"}
    now = 10_000.0
    w._forget({"a": {"sid": "a"}}, now)          # b has left
    assert "b" in w._gone, "did not notice it left"
    assert "b" in w.seen, "forgot it instantly"
    w._forget({"a": {"sid": "a"}}, now + watchtower.FORGET_AFTER + 1)
    for d in (w.seen, w.last, w.pending):
        assert "b" not in d, d
    assert "b" not in w.expecting
    assert "a" in w.seen, "forgot a session that is still there"


def test_a_session_that_blinks_out_for_one_tick_keeps_its_place():
    """A snapshot can miss a session for a tick, and forgetting its mark means
    re-reading out everything it has already said."""
    w = watchtower.Watchtower(announce=lambda *a, **k: None)
    w.seen = {"a": "mark"}
    w._forget({}, 10_000.0)                       # gone
    w._forget({"a": {"sid": "a"}}, 10_001.0)      # back
    assert w.seen["a"] == "mark"
    assert "a" not in w._gone, "still counted as departed"


def test_every_tracked_dict_is_pruned_not_just_the_first():
    """The departed set was derived from `seen` alone, so an id evicted there
    was never looked for in `pending`, which went on growing after the fix that
    was supposed to stop exactly this."""
    w = watchtower.Watchtower(announce=lambda *a, **k: None)
    w.pending = {"orphan": ("text", 0)}
    w.last = {"orphan2": "text"}
    now = 10_000.0
    w._forget({}, now)
    w._forget({}, now + watchtower.FORGET_AFTER + 1)
    assert not w.pending and not w.last, (w.pending, w.last)


def test_the_cap_holds_even_when_nothing_has_expired():
    """A timer only helps if time passes. Something churning sessions faster
    than they expire would still climb."""
    w = watchtower.Watchtower(announce=lambda *a, **k: None)
    for i in range(watchtower.MAX_TRACKED + 50):
        w.seen[f"s{i}"] = "x"
    w._forget({}, 10_000.0)          # all gone, none expired yet
    assert len(w.seen) <= watchtower.MAX_TRACKED, len(w.seen)


def test_a_live_session_is_never_dropped_for_the_cap():
    """However many there are. Dropping a running agent's mark to save memory
    means re-reporting everything it has said."""
    w = watchtower.Watchtower(announce=lambda *a, **k: None)
    rows = {f"s{i}": {"sid": f"s{i}"} for i in range(watchtower.MAX_TRACKED + 40)}
    for sid in rows:
        w.seen[sid] = "x"
    w._forget(rows, 10_000.0)
    assert len(w.seen) == len(rows), "dropped a live session"


def test_announced_keys_stop_growing():
    """Every calendar event, Sentry issue and broken workflow adds a key and
    nothing ever removed one."""
    s = feeds._Seen()
    for i in range(s.MAX + 500):
        s.add(f"k{i}")
    assert len(s) <= s.MAX
    assert "k0" not in s, "kept the oldest and dropped something else"
    assert f"k{s.MAX + 499}" in s, "dropped the newest"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  growth: every ceiling declared, and every ceiling held")
