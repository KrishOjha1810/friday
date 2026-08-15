"""The server: proactive messages must actually reach the thread, and the app
must never be reachable without a key once it is exposed.

The first is the whole product (Friday speaking first is the differentiator);
the second is the difference between a helpful assistant and a remote control
for your machine that anyone on the network can pick up.

Run: python3 tests/test_server.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import use_temp_config  # noqa: E402

use_temp_config()   # never touch the real ~/.friday: a test once
                    # deleted a live Slack token this way
from friday import server  # noqa: E402


def test_a_proactive_message_reaches_every_open_tab():
    """supervisor -> Friday.announce -> SSE queue. If this breaks, Friday goes
    silent and looks like it is working fine."""
    q = []
    server._QUEUES[id(q)] = q
    try:
        msg = server._friday.announce("jobhunt finished.")
        server._push({"kind": "message", "message": msg})
        assert len(q) == 1
        assert q[0]["message"]["text"] == "jobhunt finished."
        assert q[0]["message"]["kind"] == "proactive"   # styled as unprompted
    finally:
        server._QUEUES.pop(id(q), None)


def test_a_dead_tab_cannot_break_delivery_to_the_others():
    good = []
    class Exploding(list):
        def append(self, x):
            raise RuntimeError("this tab is gone")
    bad = Exploding()
    server._QUEUES[id(good)] = good
    server._QUEUES[id(bad)] = bad
    try:
        try:
            server._push({"kind": "message", "message": {"text": "hi"}})
        except Exception:
            pass
        assert len(good) == 1, "one dead tab silenced the others"
    finally:
        server._QUEUES.pop(id(good), None)
        server._QUEUES.pop(id(bad), None)


def test_the_fleet_never_crashes_the_page():
    """Whatever the sensor does, /state must still render."""
    rows = server._fleet_rows()
    assert isinstance(rows, list)
    for r in rows:
        assert {"sid", "label", "status"} <= set(r)


def test_local_only_needs_no_key_but_exposed_demands_one():
    """The security boundary, asserted rather than assumed."""
    class FakeReq:
        def __init__(self, path, hdr=None):
            self.path = path
            self.headers = hdr or {}
    h = server.Handler.__new__(server.Handler)

    server.LOCAL_ONLY = True
    h.path, h.headers = "/", {}
    assert h._authed() is True                      # nothing else can reach us

    server.LOCAL_ONLY = False
    h.path, h.headers = "/", {}
    assert h._authed() is False                     # no key: refused
    h.path = "/?k=wrong"
    assert h._authed() is False                     # wrong key: refused
    h.path = "/?k=" + server.SECRET
    assert h._authed() is True                      # right key
    h.path, h.headers = "/", {"X-Friday-Key": server.SECRET}
    assert h._authed() is True                      # header works too
    server.LOCAL_ONLY = True                        # leave it safe


def test_the_secret_is_not_world_readable():
    import os
    if server.SECRET_FILE.exists():
        mode = os.stat(server.SECRET_FILE).st_mode & 0o777
        assert mode == 0o600, f"secret is {oct(mode)}, must be 0600"


def test_every_background_voice_reaches_the_browser():
    """Each watcher keeps the callback it was constructed with, so anything
    started in run() must have its announce rebound to the pushing one. Miss it
    and that source's news lands in history only: you see it on your next
    reload rather than when it happened. This has now been missed twice."""
    src = (Path(__file__).resolve().parents[1] / "friday" / "server.py").read_text()
    run = src[src.index("def run("):]
    started = {line.split(".")[1] for line in run.splitlines()
               if ".start()" in line and "_friday." in line}
    for name in started:
        assert f"_friday.{name}.announce =" in run, \
            f"{name} is started but its announce was never rebound"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  friday server: proactive delivery, resilient tabs, real auth")
