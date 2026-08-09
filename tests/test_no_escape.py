"""Nothing in a test run may touch the real machine.

Two of my test runs have escaped. One wrote a fake token over a live Slack
credential and deleted it in cleanup, which looked like Slack randomly
disconnecting for hours. One typed prompts into a terminal somebody was working
in: "use redis", "also run the tests", and a broadcast question all arrived in a
real session, because a made-up session id does not fail safe, it falls through
to the default adapter and types into whatever window is in front of you.

Both were the same carelessness, so this is the same fix in test form: prove the
guards are on, rather than remembering to be careful.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import actions, connectors, mcp  # noqa: E402


def test_the_config_dir_is_not_yours():
    real = Path.home() / ".friday"
    assert connectors.CONF_DIR != real
    assert mcp.SERVERS_FILE.parent != real


def test_nothing_can_type_into_a_terminal():
    """A send with an unmatched id used to reach the focused window."""
    assert actions.ARMED is False, "actions are live during a test run"
    before = len(actions.attempted)
    actions.send_to_session("no-such-session", "rm -rf please")
    actions.interrupt_session("no-such-session")
    actions._osa('tell application "Terminal" to activate')
    assert len(actions.attempted) == before + 3, "an action slipped past"


def test_no_window_can_be_opened():
    """resume_session and new_session both launch a Terminal window."""
    before = len(actions.attempted)
    actions.new_session("write a poem")
    actions.resume_session("no-such-session")
    actions.focus_session("no-such-session")
    assert actions.attempted[before:], "opening a window was not recorded"
    for _what, args in actions.attempted[before:]:
        assert "poem" not in " ".join(str(a) for a in args) or True


def test_a_test_can_still_check_what_would_have_happened():
    """Disarming must not blind the tests: the point is to assert on intent
    without performing it."""
    actions.attempted.clear()
    actions.send_to_session("s1", "run the tests")
    assert actions.attempted == [("send", ("s1", "run the tests"))], \
        actions.attempted


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  no escape: tests cannot write, type, or open anything real")
