"""The bridge to voicebridge's engine.

Friday is its own product, but it does not reimplement what voicebridge already
does well: sensing every agent, deciding when something is worth interrupting
you, running a small local model, and routing an answer back to the agent that
asked. Those live in voicebridge's `vb` package and Friday consumes them.

This module is the single place that knows where voicebridge is, so if it moves
(or becomes a proper installed dependency) exactly one file changes. Everything
else in Friday imports from here.

If voicebridge is missing, Friday still starts: it says so plainly and runs with
whatever it can, rather than dying at import time with a stack trace.
"""

import os
import sys
from pathlib import Path

# Where voicebridge lives. An env var wins, then the usual spot, then anywhere
# alongside this project.
_CANDIDATES = [
    os.environ.get("VOICEBRIDGE_HOME", ""),
    str(Path.home() / "voicebridge"),
    str(Path(__file__).resolve().parent.parent.parent / "voicebridge"),
]

AVAILABLE = False
MISSING_REASON = ""

for _c in _CANDIDATES:
    # vb is a namespace package (no __init__.py), so look for a module
    # that is definitely part of it rather than the package marker.
    if _c and (Path(_c) / "vb" / "core.py").exists():
        if _c not in sys.path:
            sys.path.insert(0, _c)
        AVAILABLE = True
        VOICEBRIDGE_HOME = _c
        break
else:
    VOICEBRIDGE_HOME = ""
    MISSING_REASON = ("voicebridge not found. Set VOICEBRIDGE_HOME, or clone it "
                      "to ~/voicebridge.")

# Re-exported pieces. Imported lazily-ish so a partial install degrades to a
# clear message instead of an import explosion.
fleet = attention = brain = routing = core = sessions = adapters = None
if AVAILABLE:
    try:
        from vb import fleet, attention, brain, routing, core, sessions, adapters  # noqa
    except Exception as e:                     # pragma: no cover
        AVAILABLE = False
        MISSING_REASON = f"voicebridge found but not importable: {e}"


def status() -> dict:
    """What Friday can actually do right now, for the UI and for diagnosis."""
    out = {"voicebridge": AVAILABLE, "home": VOICEBRIDGE_HOME,
           "reason": MISSING_REASON, "brain": False, "agents": 0}
    if not AVAILABLE:
        return out
    try:
        out["brain"] = bool(brain.model_ready())
    except Exception:
        pass
    try:
        out["agents"] = len(fleet.snapshot())
    except Exception:
        pass
    return out
