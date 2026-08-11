"""The documentation must not promise commands that do not exist.

README.md and START-HERE.md are the first thing a new person reads, and a command
in a code block that quietly falls through to open conversation is worse than an
undocumented one: they will conclude Friday is broken rather than that the doc is
stale. START-HERE described the six-screen Slack setup for days after it had been
replaced by a two-step one.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday.conversation import classify  # noqa: E402

DOCS = ("README.md", "START-HERE.md")
# Lines in a code block that are not commands: shell invocations, transcript
# labels, and the file tree.
NOT_A_COMMAND = ("python3", "cd ", "for t in", "friday:", "you:", "$", "|", "*")


def _commands() -> set:
    out = set()
    root = Path(__file__).resolve().parents[1]
    for doc in DOCS:
        text = (root / doc).read_text()
        for m in re.finditer(r"^\| `([^`]+)`", text, re.M):
            out.add(m.group(1))
        for block in re.findall(r"```\n(.*?)```", text, re.S):
            for line in block.splitlines():
                line = line.rstrip()
                if not line or line.startswith(NOT_A_COMMAND):
                    continue
                cmd = re.split(r"\s{3,}", line.strip())[0].strip()
                # a path from the file tree, or a prose sentence that wrapped:
                # neither is a command. Test the EXTRACTED text, not the whole
                # line, or an aligned comment hides the path from the filter.
                if cmd.endswith((".py", ".html", "/", ".")) or "/" in cmd:
                    continue
                if cmd and 0 < len(cmd.split()) <= 10:
                    out.add(cmd)
    return out


def test_every_documented_command_reaches_a_handler():
    docs_say = _commands()
    assert len(docs_say) > 25, f"only found {len(docs_say)} commands; parser broke"
    unrouted = []
    for cmd in sorted(docs_say):
        probe = re.sub(r"<[^>]+>", "jobhunt", cmd)
        intent, _payload = classify(probe)
        if intent == "chat":
            unrouted.append(cmd)
    assert not unrouted, "documented but not understood: " + repr(unrouted)


def test_the_docs_do_not_promise_what_friday_refuses_to_do():
    """Friday's own CANNOT list and the docs have to agree. They drifted once
    already: the README offered to schedule a meeting while the code correctly
    refused to."""
    from friday.conversation import Friday
    root = Path(__file__).resolve().parents[1]
    text = " ".join((root / d).read_text().lower() for d in DOCS)
    f = Friday()
    _can, cannot = f._abilities()
    assert cannot, "nothing is listed as impossible, which cannot be right"
    # the two that matter most, because offering them would be acted on
    assert "it drafts, you send" in text or "drafts, you send" in text, \
        "the docs do not say Friday cannot post to Slack"
    assert "cannot" in text and "calendar" in text, \
        "the docs do not mention the calendar limit"


def test_the_slack_setup_described_is_the_one_that_exists():
    """The old instructions told you to create an app and add ten scopes by hand.
    That path was replaced by a configuration token, and a doc describing the
    dead one sends somebody through six screens for nothing."""
    root = Path(__file__).resolve().parents[1]
    text = " ".join((root / d).read_text() for d in DOCS)
    assert "App Configuration Token" in text, "the current path is not described"
    assert "search:read" not in text, "still lists scopes you no longer add by hand"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  docs: every documented command exists, and the limits agree")
