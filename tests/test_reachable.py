"""What the server accepts, and from whom.

The whole local mode rested on one sentence in a docstring: "local-only needs no
key, nothing else can reach it". That is false, and it was the only thing
standing between any website you have open and a supervisor that types into your
running agents. Most of this file is about that sentence.

Nothing here touches the network or a real terminal: the sandbox redirects the
config directory and disarms both actions and connectors.
"""

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import actions, fleetcache, push, server  # noqa: E402

PORT = 8951
SENT = []
fleetcache.snapshot = lambda: {
    "s1": {"sid": "s1", "label": "api", "question": "", "status": "idle",
           "path": "", "mtime": time.time()}}
actions.send_to_session = lambda sid, t: SENT.append((sid, t)) or True

server.LOCAL_ONLY = True
threading.Thread(target=lambda: server.run(PORT), daemon=True).start()
for _ in range(60):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
        break
    except Exception:
        time.sleep(0.25)


def _post(path, body, **headers):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "text/plain", **headers})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def _raw(request: bytes) -> bytes:
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    try:
        s.sendall(request)
        return s.recv(64).split(b"\r\n")[0]
    except Exception as e:
        return f"ERR {e}".encode()
    finally:
        s.close()


# ---- the page next door ----------------------------------------------------
def test_another_website_cannot_make_friday_act():
    """A fetch with a text/plain body is a CORS simple request, so it is SENT
    with no preflight. The attacker not being able to read the reply does not
    matter when the point was the side effect: a page on any origin could make
    Friday type into a running agent."""
    SENT.clear()
    code, _ = _post("/say", {"text": "tell api yes"},
                    Origin="https://evil.example")
    assert code == 401, code
    assert not SENT, SENT


def test_a_referer_from_elsewhere_is_refused_too():
    """Some browsers omit Origin on a form post and send only a Referer."""
    SENT.clear()
    code, _ = _post("/say", {"text": "tell api yes"},
                    Referer="https://evil.example/page")
    assert code == 401, code
    assert not SENT, SENT


def test_a_rebound_hostname_cannot_read_your_history():
    """A domain that resolves to 127.0.0.1 reaches the same socket, and /state
    carries the whole conversation."""
    got = _raw(b"GET /state HTTP/1.1\r\nHost: friday.evil.example\r\n\r\n")
    assert b"401" in got, got


def test_the_real_page_still_works():
    """The guard is worthless if it costs the product. Same origin, and no
    Origin at all, which is what a plain address-bar request looks like."""
    code, body = _post("/say", {"text": "hello"},
                       Origin=f"http://127.0.0.1:{PORT}")
    assert code == 200, code
    assert json.loads(body).get("reply") is not None
    assert _post("/say", {"text": "hello"})[0] == 200


# ---- malformed requests ----------------------------------------------------
def test_a_negative_content_length_does_not_hold_a_thread():
    """"-1" is truthy and read(-1) blocks until the client feels like closing,
    so a handful of sockets pinned threads indefinitely with no timeout."""
    assert b"400" in _raw(
        b"POST /say HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: -1\r\n\r\n")


def test_a_nonsense_content_length_answers_rather_than_crashing():
    assert b"400" in _raw(
        b"POST /say HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: abc\r\n\r\n")


def test_an_enormous_body_is_refused_before_it_is_read():
    assert b"400" in _raw(
        b"POST /say HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Length: 99999999999\r\n\r\n")


def test_a_body_of_the_wrong_shape_gets_an_answer():
    """These reached .get and .strip on a list, an int and a dict, and the
    client got a dropped connection while a traceback went across the terminal
    that IS the interface."""
    for body in ([1, 2, 3], {"text": 12345}, {"text": {"a": 1}}, "a string",
                 None, 7):
        code, _ = _post("/say", body)
        assert code == 200, (body, code)


# ---- alerts ----------------------------------------------------------------
def test_a_subscription_cannot_point_anywhere_it_likes():
    """Anything accepted here receives every future alert, encrypted to a key
    the registrant chose, and it survives a restart."""
    keys = {"p256dh": "x" * 20, "auth": "y" * 16}
    for bad in ("http://127.0.0.1:9/relay", "https://evil.example/relay",
                "https://fcm.googleapis.com.evil.test/x"):
        assert push.subscribe({"endpoint": bad, "keys": keys}) is False, bad


# ---- what the phone needs --------------------------------------------------
def test_the_things_the_page_links_without_a_key_are_reachable():
    """The page links its manifest and icons from <head> with no key on them,
    so on a phone they 401: the app cannot install, the icon is blank, and a
    tapped notification lands on unauthorized. They carry no data."""
    for path in ("/manifest.json", "/icon-192.png"):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
            assert r.status == 200, path
            assert r.read(), path


def test_health_says_only_that_friday_is_here():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health",
                                timeout=5) as r:
        assert r.read() == b"friday"


# ---- the stream ------------------------------------------------------------
def test_a_closed_tab_is_noticed_even_when_nothing_is_happening():
    """It only ever wrote when the fleet changed, so a dead socket was never
    touched: overnight, every dropped tab kept a thread and a queue that went
    on collecting events for a reader that had gone."""
    before = len(server._QUEUES)
    socks = []
    for _ in range(6):
        s = socket.create_connection(("127.0.0.1", PORT))
        s.sendall(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        socks.append(s)
    for _ in range(40):
        if len(server._QUEUES) >= before + 6:
            break
        time.sleep(0.1)
    assert len(server._QUEUES) >= before + 6, server._QUEUES
    for s in socks:
        s.close()
    deadline = time.time() + server.HEARTBEAT + 15
    while len(server._QUEUES) > before and time.time() < deadline:
        time.sleep(0.5)
    assert len(server._QUEUES) <= before, "dead streams were never reaped"


def test_a_tab_that_stops_reading_does_not_grow_forever():
    q = []
    server._QUEUES["probe"] = q
    try:
        for i in range(server.MAX_QUEUED + 200):
            server._push({"kind": "message", "n": i})
        assert len(q) <= server.MAX_QUEUED, len(q)
        assert q[-1]["n"] == server.MAX_QUEUED + 199, "kept the oldest instead"
    finally:
        server._QUEUES.pop("probe", None)


def test_broadcast_actually_reaches_a_tab():
    """It iterated id() integers and called .append on them inside a bare
    except, so it had been a silent no-op since the day it was written."""
    q = []
    server._QUEUES["probe"] = q
    try:
        server.broadcast("fleet", {"rows": []})
        assert q and q[0]["kind"] == "fleet", q
    finally:
        server._QUEUES.pop("probe", None)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  reachable: only from Friday's own page, and never a wedged thread")
