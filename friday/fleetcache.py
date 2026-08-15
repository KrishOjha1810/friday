"""One look at the fleet, shared by everyone who asks.

Reading the fleet means shelling out to `claude agents --json` and parsing it,
which measured at 3.7 seconds. Three things want that answer constantly: the
status strip polls it, the watchtower polls it, and every question about a
session name looks a name up in it. Uncached, a single page refresh cost over
seven seconds and the strip was permanently stale, while the watchtower spent
most of its life waiting on a subprocess it had already run.

So: at most one real read per TTL, handed to everyone. The number is small
enough that nothing looks frozen and large enough that a burst of questions
costs one subprocess rather than ten.
"""

import threading
import time

from . import engine

TTL = 6.0             # how stale a reading may be before a caller
                      # waits for a fresh one. The refresher keeps
                      # it well under this, so in practice nobody
                      # ever blocks.

_lock = threading.Lock()
_at = 0.0
_rows = {}
_failed = ""
_busy = False        # a background refresh is already in flight


def snapshot(max_age: float = None) -> dict:
    """The fleet, possibly from a moment ago. Never raises.

    Callers used to hit the raw sensor, so a failure there became a 500 and the
    question was answered with nothing at all. An empty fleet and an unreadable
    fleet are different, and `error()` tells them apart."""
    global _at, _rows, _failed, _busy
    age = TTL if max_age is None else max_age
    now = time.time()
    with _lock:
        if _rows and now - _at < age:
            return _rows
        # Stale but usable. Serve it and refresh behind you: blocking a request
        # for three seconds to avoid showing a three-second-old reading is a bad
        # trade, and it is the trade that made the status strip feel dead.
        if _rows and age > 0:
            if not _busy:
                _busy = True
                threading.Thread(target=_refresh_once, daemon=True).start()
            return _rows
    try:
        # Every vendor, not just Claude. Friday conducted one agent on a machine
        # that had two, and the plan always said vendor-neutral.
        from . import agents
        rows = agents.sessions()
        err = ""
    except Exception as e:
        rows, err = None, f"{type(e).__name__}: {e}"
    with _lock:
        if rows is None:
            _failed = err
            # Keep serving the last good reading rather than pretending the
            # fleet is empty: "nothing is running" is a lie that reads as fact.
            return _rows
        _at, _rows, _failed = now, rows, ""
        return _rows


def _refresh_once() -> None:
    global _busy
    try:
        snapshot(max_age=0)
    except Exception:
        pass
    finally:
        _busy = False


def error() -> str:
    """Why the last read failed, if it did."""
    return _failed


def refresh_forever(period: float = 3.0) -> None:
    """Keep the reading warm in the background, so nobody ever waits for it.

    A read costs 3.2 seconds, which is the CLI's own startup and not something
    Friday can make faster. What it CAN do is never make you wait for it: one
    thread refreshes, every caller gets the last answer immediately. Polling the
    status strip becomes free, and the watchtower stops spending most of each
    tick blocked on a subprocess it just ran."""
    def _loop():
        while True:
            try:
                snapshot(max_age=0)
            except Exception:
                pass
            time.sleep(period)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def bust() -> None:
    """Force the next read to be real. Used after Friday changes something."""
    global _at
    with _lock:
        _at = 0.0
