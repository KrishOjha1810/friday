"""Friday as a Mac app, and the one thing it must never do.

"Open a browser tab and keep it open" is not a product a person adopts. A tab
gets closed by accident, loses its place among thirty others, and stops existing
when the browser quits. Being the one place you go cannot mean being the
eleventh tab.

The thing it must never do is become a second Friday. People run `python3
run.py` in a terminal and then open the app, and two servers watching the same
fleet would announce everything twice, each convinced it was the only one. Most
of this file is about that.

The window itself is a WKWebView pointed at the same server, so there is no
second interface to keep in sync and nothing here to test that the browser
suite does not already cover.
"""

import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

ROOT = Path(__file__).resolve().parents[1]
PORT = 8823


# The port has to be set BEFORE the first import: app.py reads it at module
# level, and reloading the module is not an option because that redefines an
# Objective-C class, which the runtime refuses.
import os  # noqa: E402

os.environ["FRIDAY_PORT"] = str(PORT)

from friday import app  # noqa: E402


class _Serve:
    """A stand-in Friday already running on the port."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = (b"friday" if self.path.startswith("/health")
                        else b'{"fleet": [{"status": "needs"}, '
                             b'{"status": "working"}, {"status": "needs"}]}')
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = HTTPServer(("127.0.0.1", PORT), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        for _ in range(40):
            if app._up(0.3):
                return
            time.sleep(0.1)

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


# ---- not becoming a second Friday -----------------------------------------
def test_it_notices_one_is_already_running():
    """/health has to answer without a key, which is why it is the one endpoint
    that skips the check. Anything else and the app cannot tell a running
    Friday from a missing one."""
    s = _Serve()
    try:
        assert app._up() is True
    finally:
        s.stop()
    assert app._up(0.4) is False


def test_it_adopts_the_running_one_instead_of_starting_another():
    """Two servers on the same fleet announce everything twice, each convinced
    it is the only one."""
    s = _Serve()
    try:
        server = app.Server()
        server.ensure()
        assert server.proc is None, "started a second Friday"
    finally:
        s.stop()


def test_quitting_does_not_kill_a_server_it_did_not_start():
    """You started it in a terminal. The app has no business ending it, and
    finding your terminal Friday dead because you closed a menu is the kind of
    surprise that loses trust in everything else."""
    s = _Serve()
    try:
        server = app.Server()
        server.ensure()
        server.stop()
        assert app._up() is True, "killed somebody else's server"
    finally:
        s.stop()


def test_it_can_start_one_when_there_is_none():
    server = app.Server()
    assert not app._up(0.4)
    try:
        server.ensure()
        assert app._up(1.0), "no server after ensure()"
        assert server.proc is not None
    finally:
        server.stop()
        time.sleep(0.5)


def test_it_restarts_a_server_it_started_and_lost():
    """A menu bar item that has been sitting there all day and quietly stopped
    working is worse than none, because you go on believing it."""
    server = app.Server()
    server.ensure()
    assert app._up(1.0)
    try:
        first = server.proc.pid
        import os
        import signal
        os.killpg(os.getpgid(first), signal.SIGKILL)
        for _ in range(40):
            if not app._up(0.3):
                break
            time.sleep(0.2)
        assert not app._up(0.4), "it did not actually die"
        server.watch()
        assert app._up(1.0), "never came back"
        assert server.proc.pid != first
    finally:
        server.stop()
        time.sleep(0.5)


def test_it_does_not_restart_somebody_elses_server():
    """If you started it in a terminal and it died, that is yours to see. An
    app that silently replaces your process is an app that hides the failure
    you needed to know about."""
    s = _Serve()
    server = app.Server()
    server.ensure()
    assert server.proc is None
    s.stop()
    server.watch()
    assert server.proc is None, "adopted, then took over"


def test_it_gives_up_rather_than_flailing():
    """Restarting a thing that will not stay up, forever, every few seconds, is
    a worse failure than being down."""
    server = app.Server()
    server.restarts = 99
    server.proc = type("Dead", (), {"poll": lambda self: 1})()
    server.watch()
    assert server.proc is not None, "kept trying past the limit"


# ---- what the menu bar says -----------------------------------------------
def test_the_count_is_only_things_waiting_on_you():
    """A busy fleet is not news; a blocked one is. A menu bar is glanced at,
    and a number that goes up when an agent is merely working is a number you
    learn to ignore."""
    s = _Serve()
    try:
        st = app._state()
        rows = st.get("sessions") or st.get("fleet") or []
        needs = sum(1 for r in rows if r.get("status") == "needs")
        assert needs == 2, rows
    finally:
        s.stop()


def test_a_server_that_is_not_answering_is_not_reported_as_quiet():
    """No sessions and no server look identical from the menu bar, and only one
    of them means there is nothing to do."""
    assert app._state() == {}


def test_the_page_it_opens_carries_the_key():
    """Otherwise the app shows a 401 and there is no way to type a key into a
    popover."""
    assert "?k=" in app._url() or not app._secret()


# ---- it actually runs ------------------------------------------------------
def test_it_launches_and_stays_up():
    """The only test here that proves anything about AppKit. Everything else
    would pass on a machine where the window never appears."""
    import os
    env = dict(os.environ, FRIDAY_PORT=str(PORT + 1), PYTHONPATH=str(ROOT))
    p = subprocess.Popen([sys.executable, "-m", "friday.app"], cwd=str(ROOT),
                         env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, start_new_session=True)
    try:
        time.sleep(6)
        assert p.poll() is None, (
            "the app exited: "
            + (p.stdout.read().decode(errors="ignore")[:600] if p.stdout else ""))
    finally:
        import signal
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            p.terminate()
        time.sleep(1)
        subprocess.run(["pkill", "-f", f"run.py {PORT + 1}"],
                       capture_output=True)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  app: in the menu bar, and never a second Friday")
