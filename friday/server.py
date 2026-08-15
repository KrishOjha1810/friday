"""Friday's server: one page, one conversation, live.

Deliberately small and dependency-free (stdlib only, like voicebridge), and it
binds to localhost so nothing is exposed to the network. The UI is served as a
plain page you open in a browser: no app to install, no code signing, and the
same page works on the phone through voicebridge's existing tunnel.

Endpoints:
  GET  /            the chat
  GET  /events      SSE: proactive messages, fleet changes, brain state
  POST /say         you said something -> Friday's reply
  GET  /state       fleet + status, for the strip at the top
  POST /quiet       silence / resume
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import engine, fleetcache
from .conversation import Friday

HOST, PORT = "127.0.0.1", 8765
STATIC = Path(__file__).resolve().parent.parent / "static"

# Friday can drive the machine: open windows, type into running agents. The
# moment it is reachable from anything but this computer, that has to be behind
# a secret. Generated once, stored with owner-only permissions, and required on
# every request when the server is not bound to localhost.
SECRET_FILE = Path.home() / ".friday" / "secret"


def secret() -> str:
    try:
        return SECRET_FILE.read_text().strip()
    except Exception:
        pass
    import secrets as _s
    tok = "fr-" + _s.token_hex(16)
    try:
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(tok)
        SECRET_FILE.chmod(0o600)
    except Exception:
        pass
    return tok


SECRET = secret()
LOCAL_ONLY = True        # flipped by run(expose=True)

_friday = Friday()
_subs = set()
_subs_lock = threading.Lock()


def broadcast(kind: str, payload: dict) -> None:
    """Push to every open tab. Dead subscribers are dropped, never retried."""
    ev = {"kind": kind, **payload}
    with _subs_lock:
        for q in list(_subs):
            try:
                q.append(ev)
            except Exception:
                _subs.discard(q)


def _fleet_rows() -> list:
    if not engine.AVAILABLE:
        return []
    try:
        rows = []
        for r in fleetcache.snapshot().values():
            need = r.get("question") or r.get("permission") or ""
            rows.append({"sid": r.get("sid", ""), "label": r.get("label", ""),
                         "status": ("needs" if need else r.get("status", "idle")),
                         "about": (r.get("topic") or "")[:120],
                         "needs": need[:140]})
        rows.sort(key=lambda x: (x["status"] != "needs", x["label"]))
        return rows
    except Exception:
        return []


def _transcribe(raw: bytes, ctype: str) -> str:
    """Browser audio -> text, locally. Returns '' on any failure: a failed
    transcription must look like 'I did not catch that', never like a crash."""
    if not raw or not engine.AVAILABLE:
        return ""
    import subprocess
    import tempfile
    ext = ".webm"
    if "mp4" in ctype or "aac" in ctype:
        ext = ".mp4"          # iOS records mp4, not webm
    src = wav = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(raw)
            src = f.name
        wav = src + ".wav"
        ff = "/opt/homebrew/bin/ffmpeg"
        import os as _os
        if not _os.path.exists(ff):
            ff = "/usr/local/bin/ffmpeg"
        r = subprocess.run([ff, "-y", "-i", src, "-ar", "16000", "-ac", "1", wav],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            return ""
        from vb import stt as _stt
        return engine.core.cleanup_transcript(_stt.transcribe(wav)) or ""
    except Exception as e:
        try:
            engine.log(f"friday stt: {e}")
        except Exception:
            pass
        return ""
    finally:
        import os as _os
        for pth in (src, wav):
            try:
                if pth:
                    _os.remove(pth)
            except OSError:
                pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _authed(self) -> bool:
        """Local-only needs no key (nothing else can reach it). Exposed to the
        network, every request must carry the secret."""
        if LOCAL_ONLY:
            return True
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        if (q.get("k") or [""])[0] == SECRET:
            return True
        return self.headers.get("X-Friday-Key", "") == SECRET

    def log_message(self, *a):
        pass                      # the terminal is for Friday, not for logs

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0]
        if not self._authed():
            self._send(401, b"unauthorized", "text/plain")
            return
        if path == "/":
            try:
                html = (STATIC / "index.html").read_bytes()
            except Exception as e:
                self._send(500, f"UI missing: {e}".encode(), "text/plain")
                return
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/sw.js":
            # Served from the root, not /static/, because a worker can only
            # control pages at or below its own path. From /static/sw.js it
            # would control nothing and push would silently never arrive.
            try:
                body = (STATIC / "sw.js").read_bytes()
            except Exception as e:
                self._send(500, f"sw missing: {e}".encode(), "text/plain")
                return
            self._send(200, body, "application/javascript")
            return
        if path == "/push/key":
            from . import push as _push
            self._json({"key": _push.public_key(),
                        "subscribed": len(_push.subscriptions())})
            return
        if path == "/state":
            self._json({"fleet": _fleet_rows(), "engine": engine.status(),
                        "quiet": (engine.attention.is_quiet()
                                  if engine.AVAILABLE else False),
                        # Who a bare message would reach. The page had no way
                        # to show this, so routing was invisible and you found
                        # out where your words went by watching them arrive.
                        "target": getattr(_friday, "target", ""),
                        "history": _friday.history[-60:]})
            return
        if path == "/events":
            self._stream()
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._authed():
            self._send(401, b"unauthorized", "text/plain")
            return
        n = int(self.headers.get("Content-Length", "0") or 0)
        # Read the body ONCE, as bytes. Parsing it as JSON up front ate binary
        # uploads (audio arrived, then /stt found an empty stream and the
        # request hung), so the raw bytes are kept and JSON is parsed lazily by
        # the handlers that actually want it.
        raw = self.rfile.read(n) if n else b""

        def as_json():
            try:
                return json.loads(raw or b"{}")
            except Exception:
                return {}
        if path == "/say":
            text = (as_json().get("text") or "").strip()
            if not text:
                self._json({"reply": "", "needs_confirm": False})
                return
            out = _friday.handle(text)
            broadcast("fleet", {"rows": _fleet_rows()})
            self._json(out)
            return
        if path == "/stt":
            # Voice in. The browser records, we transcribe LOCALLY with the
            # same whisper voicebridge uses, so speaking and typing are the
            # same conversation and nothing leaves the machine.
            self._json({"text": _transcribe(raw, self.headers.get("Content-Type", ""))})
            return

        if path == "/speak":
            # Voice out, on request. The UI decides when Friday should be heard
            # rather than read; the thread is identical either way.
            if engine.AVAILABLE:
                j = as_json()
                txt = (j.get("text") or "").strip()
                if txt and j.get("wait"):
                    # A hands-free call has to know when Friday stopped talking,
                    # otherwise it starts listening to its own voice. Blocking
                    # here is the simplest honest signal.
                    engine.core.speak(txt, blocking=True)
                elif txt:
                    threading.Thread(target=lambda: engine.core.speak(txt),
                                     daemon=True).start()
            self._json({"ok": True})
            return

        if path == "/push/subscribe":
            from . import push as _push
            ok = _push.subscribe(as_json().get("subscription") or {})
            self._json({"ok": bool(ok)})
            return
        if path == "/push/unsubscribe":
            from . import push as _push
            _push.unsubscribe((as_json().get("endpoint") or ""))
            self._json({"ok": True})
            return
        if path == "/push/test":
            from . import push as _push
            n = _push.send("Friday", "Push is working. This is what an alert "
                                     "looks like.", tag="test")
            self._json({"sent": n, "subscribers": len(_push.subscriptions())})
            return
        if path == "/here":
            # The page says whether you are looking at it. Pushing something to
            # your phone that is already on the screen in front of you is how a
            # notification permission gets revoked.
            _friday.watching = bool(as_json().get("visible"))
            _friday.watching_at = time.time()
            self._json({"ok": True})
            return
        if path == "/quiet":
            on = bool(as_json().get("on"))
            if engine.AVAILABLE:
                engine.attention.hush() if on else engine.attention.unhush()
            self._json({"quiet": on})
            return
        self._send(404, b"not found", "text/plain")

    def _stream(self):
        """SSE. Each tab gets its own queue; a slow tab cannot block the rest."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = []
        with _subs_lock:
            _subs.add(id(q))
            _QUEUES[id(q)] = q
        last_fleet = None
        try:
            while True:
                while q:
                    ev = q.pop(0)
                    self.wfile.write(
                        f"data: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()
                rows = _fleet_rows()
                if rows != last_fleet:
                    last_fleet = rows
                    self.wfile.write(
                        f"data: {json.dumps({'kind': 'fleet', 'rows': rows})}\n\n"
                        .encode())
                    self.wfile.flush()
                time.sleep(1.5)
        except Exception:
            pass
        finally:
            with _subs_lock:
                _subs.discard(id(q))
                _QUEUES.pop(id(q), None)


_QUEUES = {}


def _push(ev: dict):
    with _subs_lock:
        for q in _QUEUES.values():
            q.append(ev)


def supervisor_loop():
    """Friday's own attention: watch the fleet and bring things up unprompted.
    The judgment (whether something is worth saying at all) is voicebridge's
    attention engine; this only delivers the result into the conversation."""
    if not engine.AVAILABLE:
        return
    from vb import supervisor as sup
    state = sup.new_state()
    while True:
        try:
            # Claim alert delivery each tick, so voicebridge's own daemon stays
            # quiet while Friday is up (otherwise every event is announced
            # twice). The claim expires on its own, so if Friday dies the
            # daemon takes over again rather than everything going silent.
            engine.attention.claim_supervisor("friday")
            said = sup.tick(state)
            for text in said:
                # Hand the underlying events along, so an unqualified reply in
                # the thread can reach whichever agent actually asked.
                msg = _friday.announce(text, items=state.get("_last_spoken"))
                _push({"kind": "message", "message": msg})
        except Exception as e:
            engine.log(f"friday supervisor: {e}")
        time.sleep(3)


def run(port: int = PORT, expose: bool = False):
    global LOCAL_ONLY
    LOCAL_ONLY = not expose
    host = "0.0.0.0" if expose else HOST
    # Keep the fleet reading warm so no request ever waits on the CLI.
    fleetcache.refresh_forever()
    threading.Thread(target=supervisor_loop, daemon=True).start()
    # Watch what every session SAYS, not just when one needs you. Without this,
    # an agent finishes something and you only find out by opening its window.
    # It must PUSH as well as record: an announcement that only lands in history
    # is one you see the next time you reload, which is not being told.
    _friday.watch.announce = lambda text, items=None: _push(
        {"kind": "message", "message": _friday.announce(text, items)})
    _friday.watch.start()
    _friday.inbox.announce = _friday.watch.announce
    _friday.inbox.start()
    _friday.feeds.announce = _friday.watch.announce
    _friday.feeds.start()
    # The plan runner too. Each of these holds the callback it was CONSTRUCTED
    # with, so a rebind here is not optional decoration: without it a plan's
    # step-by-step progress lands in history and never reaches the browser, and
    # you would see it on your next reload rather than as it happened.
    _friday.plans.announce = _friday.watch.announce
    # Tell the transcriber the names it is about to hear. Friday is the only
    # process that knows them, and it was keeping them to itself while speech
    # recognition mangled every one.
    from . import vocab as _vocab
    _vocab.keep_fresh()
    # Say what was left unfinished. "Survives a restart" was true of the data
    # and not of the execution: a plan left running sat running forever and
    # nobody was ever told.
    try:
        from . import plan as _plan
        _plan.sweep_on_start(_friday.watch.announce)
    except Exception as e:
        engine.log(f"friday plan sweep: {e}")
    srv = ThreadingHTTPServer((host, port), Handler)
    if expose:
        print(f"Friday is listening on http://{host}:{port}?k={SECRET}")
        print("  reachable from your phone. The key is required; keep the URL private.")
    else:
        print(f"Friday is listening on http://{HOST}:{port}")
    st = engine.status()
    if not st["voicebridge"]:
        print(f"  note: {st['reason']}")
    else:
        print(f"  watching {st['agents']} agents"
              + ("" if st["brain"] else "  (brain not loaded: replies will be brief)"))
    srv.serve_forever()
