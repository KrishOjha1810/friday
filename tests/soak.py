"""Run Friday hard for a while and watch what grows.

Nobody has run Friday for a full day. Every other test here asks whether
something is correct once; this one asks whether it is still correct on hour
nine, which is a different question and the one with no evidence behind it.

Time is compressed rather than simulated: the loops run at their real code paths
with their periods shortened, and the fleet churns the way a real one does, with
new session ids appearing all day as you start new agents. That churn is the
point. Anything keyed by session id looks bounded until you notice a working day
produces dozens of ids and a month produces thousands.

    python3 tests/soak.py            about a minute
    python3 tests/soak.py 600        ten minutes, roughly a day compressed
"""

import gc
import json
import os
import resource
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

TMP = use_temp_config()

from friday import budget as budgets, conversation as C, engine, feeds  # noqa: E402
from friday.conversation import Friday  # noqa: E402

DIR = TMP / "soak"
DIR.mkdir(exist_ok=True)

# A working day: a few long-lived agents, and a new one every so often.
LIVE = {}
_n = [0]


def _say(sid, text):
    p = DIR / f"{sid}.jsonl"
    with open(p, "a") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": text}]}}) + "\n")
    return str(p)


def churn():
    """One tick of a fleet that is being used."""
    _n[0] += 1
    i = _n[0]
    # A new session every eight ticks, which over a compressed day is about the
    # rate a person actually opens agents.
    if i % 8 == 0 or not LIVE:
        sid = f"s{i}"
        LIVE[sid] = {"sid": sid, "label": f"job{i}", "status": "working",
                     "path": _say(sid, f"starting {i}"), "question": "",
                     "topic": f"task {i}", "mtime": time.time()}
    for sid, row in list(LIVE.items()):
        row["mtime"] = time.time()
        # Long replies on purpose: anything that keeps "the last thing it said"
        # per session is holding this, and the cost is invisible until it is
        # thousands of them.
        row["path"] = _say(sid, f"tick {i} " + ("detail " * 60))
        if i % 5 == 0:
            row["status"] = "needs"
            row["question"] = f"Should I do thing {i}?"
        else:
            row["status"] = "working"
            row["question"] = ""
    # Sessions end. They stop appearing in the snapshot, which is exactly how
    # a real one leaves, and is the case where a per-sid cache never gets told.
    for sid in list(LIVE)[:-6]:
        LIVE.pop(sid, None)


class _Fleet:
    @staticmethod
    def snapshot():
        return {k: dict(v) for k, v in LIVE.items()}


class _Attention:
    @staticmethod
    def is_quiet():
        return False

    @staticmethod
    def claim_supervisor(_w):
        pass


class _Brain:
    TIMEOUT_SLOW = 5

    @staticmethod
    def up():
        return False

    @staticmethod
    def model_ready():
        return False


def _rss_mb():
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS reports bytes.
    return kb / (1024 * 1024) if sys.platform == "darwin" else kb / 1024


def _fds():
    try:
        return len(os.listdir(f"/dev/fd"))
    except Exception:
        return -1


def sizes(f):
    return {
        "rss_mb": round(_rss_mb(), 1),
        "threads": threading.active_count(),
        "fds": _fds(),
        "history": len(f.history),
        "feeds.seen": len(f.feeds.seen),
        "watch.seen": len(f.watch.seen),
        "watch.last": len(f.watch.last),
        "watch.pending": len(f.watch.pending),
        # The one that actually costs something: every per-session dict here
        # holds TEXT, so the count understates it by three orders of magnitude.
        "watch.bytes": (sum(len(v) for v in f.watch.seen.values())
                        + sum(len(str(v)) for v in f.watch.last.values())
                        + sum(len(str(v)) for v in f.watch.pending.values())),
        "watch.muted": len(f.watch.muted),
        "watch.expecting": len(f.watch.expecting),
        "budget.held": f.budget.waiting(),
        "inbox.seen": len(getattr(f.inbox, "seen", {})),
        "objects": len(gc.get_objects()),
    }


# What each of these is allowed to reach, and where the ceiling is enforced.
# Anything without an entry has no ceiling at all, which is worth knowing.
CAPS = {
    "history": 400,             # conversation.add()
    "budget.held": 60,          # budget.Budget.hold()
    "feeds.seen": 10_000,       # feeds._Seen.MAX
    "watch.seen": 260,          # watchtower.MAX_TRACKED, plus a live fleet
    "watch.last": 260,
    "watch.pending": 260,
    "watch.expecting": 260,
    "threads": 12,
    "fds": 64,
}


def run(seconds=60.0, tick=0.05):
    C.engine.AVAILABLE = True
    C.engine.fleet = _Fleet
    C.engine.attention = _Attention
    C.engine.brain = _Brain
    from friday import fleetcache, watchtower, actions
    fleetcache.engine = C.engine
    fleetcache.TTL = 0.01
    watchtower.engine = C.engine
    actions.disarm()

    said = []
    f = Friday()
    f.announce = lambda text, **k: said.append(text)
    # Time is compressed consistently: the grace period before a departed
    # session is forgotten has to shrink with everything else, or the run ends
    # long before the first one expires and the graph looks like a leak.
    watchtower.POLL = 0.02
    watchtower.SETTLE = 0.05
    watchtower.FORGET_AFTER = 4.0
    f.watch.start()

    start = time.time()
    samples = []
    print(f"  {'t':>5}  {'rss':>6} {'thr':>4} {'fds':>4} {'hist':>5} "
          f"{'feeds':>6} {'watch':>6} {'wbytes':>8} {'held':>5} {'objs':>8}")
    while time.time() - start < seconds:
        churn()
        # Somebody using it, not just it running.
        if _n[0] % 20 == 0:
            f.handle("what's running?")
        if _n[0] % 37 == 0:
            f.handle("who needs me?")
        time.sleep(tick)
        if _n[0] % 100 == 0:
            s = sizes(f)
            samples.append(s)
            print(f"  {int(time.time() - start):>5}  {s['rss_mb']:>6} "
                  f"{s['threads']:>4} {s['fds']:>4} {s['history']:>5} "
                  f"{s['feeds.seen']:>6} {s['watch.seen']:>6} "
                  f"{s['watch.bytes']:>8} {s['budget.held']:>5} "
                  f"{s['objects']:>8}")
    f.watch.stop()
    last = sizes(f)
    samples.append(last)
    print(f"\n  ticks: {_n[0]}, announcements: {len(said)}")
    # Second half against first half, not first sample against last. Everything
    # here warms up, and comparing a cold start to a running system reports
    # every cache as a leak. A leak is something that is still climbing once
    # the system has settled.
    half = max(1, len(samples) // 2)
    early, late = samples[half - 1], samples[-1]
    print(f"  settled ({half} samples in) vs end:")
    over = []
    for k in sorted(last):
        a, b = early.get(k, 0), late.get(k, 0)
        if not isinstance(a, (int, float)):
            continue
        cap = CAPS.get(k)
        note = ""
        if cap is not None:
            # A cap it has not reached yet is not a leak, and a heuristic that
            # cannot tell the difference reports every warm-up as one. What
            # matters is only ever whether the ceiling holds.
            note = f"   (cap {cap})" + ("   OVER" if b > cap else "")
            if b > cap:
                over.append(f"{k} {b} > {cap}")
        elif b > a * 1.3 and b - a > 8:
            note = "   STILL CLIMBING, no cap"
            over.append(f"{k} {a} -> {b}, unbounded")
        print(f"    {k}: {a} -> {b}{note}")
    print("\n  " + ("BROKEN: " + "; ".join(over) if over
                    else "every bound held"))
    return {"last": last, "over": over}


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    run(secs)
