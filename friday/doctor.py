"""What is missing, and exactly what to type to fix it.

Friday is written so that nothing is fatal: no cryptography means no phone
alerts, no EventKit means no calendar, no voicebridge means no voice and no
session sensing. Every one of those degrades quietly and correctly, which is the
right behaviour and also the problem. On somebody else's machine "quietly
correct" is indistinguishable from "half of it does not work and I do not know
why", and the second reading is the one people act on.

So this says the whole truth in one screen: what works, what does not, what each
missing piece actually costs you, and the command that fixes it. One line per
thing, ordered by how much of Friday you get back.

    python3 run.py --check
"""

import shutil
import subprocess
import sys
from pathlib import Path

# name, what you lose without it, how to get it, how to test for it
CHECKS = []


def check(name, costs, fix):
    def wrap(fn):
        CHECKS.append((name, costs, fix, fn))
        return fn
    return wrap


@check("python 3.9+", "nothing runs at all",
       "install a newer python from python.org")
def _python():
    return sys.version_info >= (3, 9)


@check("macOS", "session sensing, the app, the calendar and speech",
       "Friday reads and types into Mac terminal windows; Linux is not "
       "supported yet")
def _macos():
    return sys.platform == "darwin"


@check("voicebridge", "voice, and seeing your Claude sessions at all",
       "install it from github.com/cc-vb/voicebridge")
def _voicebridge():
    from . import engine
    return bool(engine.AVAILABLE)


@check("a local model", "summaries and free conversation; commands still work",
       "voicebridge downloads one on first run: `vb doctor`")
def _model():
    from . import engine
    try:
        return bool(engine.AVAILABLE and engine.brain.model_ready())
    except Exception:
        return False


@check("cryptography", "alerts on your phone when the page is closed",
       "pip3 install --user cryptography")
def _crypto():
    try:
        import cryptography  # noqa: F401
        return True
    except Exception:
        return False


@check("pyobjc EventKit", "reading your calendar and warning before meetings",
       "pip3 install --user pyobjc-framework-EventKit")
def _eventkit():
    try:
        import EventKit  # noqa: F401
        return True
    except Exception:
        return False


@check("pyobjc AppKit", "the menu bar app; the browser page still works",
       "pip3 install --user pyobjc-framework-Cocoa pyobjc-framework-WebKit")
def _appkit():
    try:
        import AppKit  # noqa: F401
        return True
    except Exception:
        return False


@check("jellyfish", "matching misheard session names; exact names still work",
       "pip3 install --user jellyfish")
def _jellyfish():
    try:
        import jellyfish  # noqa: F401
        return True
    except Exception:
        return False


@check("git", "noticing uncommitted and unpushed work",
       "install the Xcode command line tools: xcode-select --install")
def _git():
    return bool(shutil.which("git"))


@check("gh", "GitHub: broken builds, review requests, issues",
       "brew install gh, then gh auth login")
def _gh():
    if not shutil.which("gh"):
        return False
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True,
                           timeout=8)
        return r.returncode == 0
    except Exception:
        return False


@check("an agent to conduct", "the entire point: Friday conducts coding agents",
       "install Claude Code, Codex or Antigravity")
def _agents():
    from . import agents
    try:
        return bool(agents.available())
    except Exception:
        return False


def report() -> tuple:
    """(lines, missing_count). Never raises: a broken check is a failed check,
    not a crash in the thing that tells you what is broken."""
    lines, missing = [], 0
    for name, costs, fix, fn in CHECKS:
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        if ok:
            lines.append(f"  yes  {name}")
        else:
            missing += 1
            lines.append(f"  NO   {name}  ({costs})\n       fix: {fix}")
    return lines, missing


def main() -> int:
    lines, missing = report()
    print("Friday, what it can see on this machine:\n")
    print("\n".join(lines))
    if not missing:
        print("\nEverything is here. `python3 run.py --app` and you are done.")
        return 0
    # Never a failure exit code for missing optional pieces: Friday genuinely
    # works without most of these, and a non-zero exit tells a person, and any
    # script wrapping this, that setup failed when it did not.
    # Always end with the command, missing pieces or not. Somebody reading a
    # list of what they do not have needs telling that they can still start it,
    # or the list reads as a checklist to finish before beginning.
    print(f"\n{missing} thing{'s' if missing > 1 else ''} missing, and each line "
          f"says what it costs. None of them stops you: "
          f"`python3 run.py` works now, and `python3 run.py --check` again "
          f"after you fix any of them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
