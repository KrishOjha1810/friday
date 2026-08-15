"""A real Friday server, with a fake world behind it, for browser tests.

Everything above the engine seam is the actual product: the real HTTP handlers,
the real page, the real conversation layer. Only the things that would reach out
of the machine are replaced, so a browser test exercises what ships rather than a
mock of it.

Prints one line, `READY <url>`, then serves until killed.
"""

import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

TMP = use_temp_config()

from friday import conversation as C, fleetcache, watchtower  # noqa: E402

# ---- a fleet that does not exist ------------------------------------------
_DIR = Path(tempfile.mkdtemp(prefix="friday-e2e-"))


def _transcript(sid: str, *lines) -> str:
    p = _DIR / f"{sid}.jsonl"
    with open(p, "w") as f:
        for text in lines:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": text}]}}) + "\n")
    return str(p)


STATE = {
    "s1": {"sid": "s1", "label": "api", "status": "needs",
           "path": _transcript("s1", "earlier work"),
           "question": "Should I force-push the rebase?",
           "topic": "the rebase", "mtime": time.time() - 300},
    "s2": {"sid": "s2", "label": "jobhunt", "status": "working",
           "path": _transcript("s2", "still going"),
           "question": "", "topic": "scoring new postings",
           "mtime": time.time() - 60},
    "s3": {"sid": "s3", "label": "voicebridge", "status": "idle",
           "path": _transcript("s3", "all tests pass"),
           "question": "", "topic": "the kokoro fix",
           "mtime": time.time() - 900},
}


class _Fleet:
    @staticmethod
    def snapshot():
        return dict(STATE)


class _Attention:
    _quiet = False

    @classmethod
    def is_quiet(cls):
        return cls._quiet

    @classmethod
    def hush(cls):
        cls._quiet = True

    @classmethod
    def unhush(cls):
        cls._quiet = False

    @staticmethod
    def claim_supervisor(_who):
        pass


class _Brain:
    @staticmethod
    def up():
        return False

    @staticmethod
    def model_ready():
        return False

    TIMEOUT_SLOW = 5


SENT = []


def _install():
    C.engine.AVAILABLE = True
    C.engine.fleet = _Fleet
    C.engine.attention = _Attention
    C.engine.brain = _Brain
    fleetcache.engine = C.engine
    fleetcache.TTL = 0.5
    watchtower.engine = C.engine
    # Nothing may touch the machine, even though this is a "real" server.
    C.actions.send_to_session = lambda sid, text: SENT.append((sid, text)) or True
    C.actions.focus_session = lambda sid: True
    C.actions.interrupt_session = lambda sid: True


def _announce_a_slack_message(friday):
    """One message from a person, so the browser can exercise the panel that
    turns a message into work."""
    import threading as _t

    def later():
        time.sleep(2.5)
        friday.watch.announce(
            "Sam in #eng: can you send the overview by Thursday?",
            items=[{"sid": "", "label": "#eng", "kind": "slack"}])
    _t.Thread(target=later, daemon=True).start()


def main():
    _install()
    from friday import server
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server.LOCAL_ONLY = True
    threading.Thread(target=lambda: server.run(port), daemon=True).start()
    time.sleep(1.5)
    _announce_a_slack_message(server._friday)
    print(f"READY http://127.0.0.1:{port}/?k={server.SECRET}", flush=True)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
