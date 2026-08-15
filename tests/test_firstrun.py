"""A machine where nothing is set up.

Every other suite here runs against a Friday that has something to work with.
This one runs against the state every user is in for their first thirty seconds,
which is the state most likely to decide whether there is a second thirty.

The bar is not "it works". Nothing works yet, by definition. The bar is that
nothing crashes and nothing reads as broken when it is merely unconfigured,
because those two look identical from the outside and only one of them is
worth waiting out.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import agents, connectors, engine, memory  # noqa: E402
from friday.conversation import Friday, classify  # noqa: E402

_EMPTY = Path(tempfile.mkdtemp())
memory.PROJECTS = _EMPTY
agents.Codex.ROOT = _EMPTY / "no-codex-here"
engine.AVAILABLE = False

# Everything a person might reasonably say before configuring anything.
OPENERS = [
    "help", "what can you do?", "hello", "what should I work on?",
    "what's on fire?", "who needs me?", "brief me", "what's connected?",
    "what did I miss?", "what are my tickets?", "what's running?",
    "file a ticket: the parser breaks on PDFs", "search slack for the deploy",
    "any new email?", "tell api to run the tests", "say more", "where is the plan",
    "what happened yesterday?", "connect sentry", "connect nonsense",
]


def _friday():
    f = Friday()
    f.announce = lambda *a, **k: None
    return f


def test_nothing_crashes():
    f = _friday()
    for said in OPENERS:
        try:
            r = f.handle(said)
        except Exception as e:
            raise AssertionError(f"{said!r} crashed: {type(e).__name__}: {e}")
        assert r.get("reply", "").strip(), f"{said!r} said nothing at all"


def test_help_works_without_the_model():
    """The likeliest first word. It used to fall through to the model, which on
    a fresh machine is not loaded, so the answer was "my brain isn't loaded
    yet" - which reads as broken rather than as unconfigured."""
    assert classify("help")[0] == "help"
    r = _friday().handle("help")
    assert "brain" not in r["reply"].lower(), r["reply"]
    assert "what should I work on" in r["reply"], r["reply"]


def test_everything_help_suggests_actually_answers():
    """A menu that lists things which do not work yet is worse than no menu."""
    f = _friday()
    for line in f.handle("help")["reply"].splitlines():
        line = line.strip()
        if not line.startswith(("what", "who", "brief", "tell")):
            continue
        cmd = line.split("  ")[0].strip()
        if "<" in cmd:                       # needs a real session name
            continue
        assert classify(cmd)[0] != "chat", f"help offers {cmd!r}, which is not a command"


def test_a_setup_hint_can_follow_a_sentence():
    """Every hint is written as an instruction and every caller pastes it after
    one, which read as "Sentry isn't connected yet. make a token at..."."""
    for name in ("sentry", "jira", "linear", "slack", "github"):
        h = connectors.hint(connectors.REGISTRY.get(name))
        if h:
            assert h[0].isupper() or h[0].isdigit(), f"{name}: {h[:40]!r}"


def test_being_unconfigured_is_said_as_unconfigured():
    """Not as an error, and not as a refusal. The difference is whether you
    try again."""
    f = _friday()
    for said, word in (("what's on fire?", "sentry"),
                       ("what are my tickets?", "jira"),
                       ("search slack for the deploy", "slack")):
        low = f.handle(said)["reply"].lower()
        assert word in low, f"{said!r} did not name what to connect"
        assert "connect" in low, f"{said!r} did not say it was a connection issue"


def test_it_offers_a_way_out_of_every_dead_end():
    """Naming the missing thing is only half of it; a hint you cannot act on
    leaves you where you were."""
    f = _friday()
    for said in ("what's on fire?", "what are my tickets?", "any new email?"):
        reply = f.handle(said)["reply"]
        assert ("http" in reply or "paste" in reply.lower()
                or "connect" in reply.lower()), f"{said!r}: {reply[:80]}"


def test_an_unknown_connector_does_not_pretend():
    r = _friday().handle("connect nonsense")
    assert "nonsense" in r["reply"], r["reply"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  first run: unconfigured never reads as broken")
