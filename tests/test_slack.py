"""Regressions from one real failure: three separate bugs, each of which made
Slack look "not connected" for a different reason, and none of which said so.

The user pasted a working token three times and got the same unhelpful sentence
every time. Every test here exists because a wrong answer was indistinguishable
from a right one from the outside.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from friday import connectors
from friday import conversation as C
from friday.conversation import classify


REAL_SHAPE = ("xoxe.xoxp-1-Mi0yLTg2NTY4OTMzNzc2MzktODcyNTY4Mzg3MzY4"
              "Mi0xMTc3NzIyOTkwODk3OS0xMTc2ODA5MjYyODc3NQ")


def test_a_rotating_token_is_captured_whole():
    """Slack issues xoxe.xoxp- when token rotation is on. Matching xoxp- with a
    word boundary grabbed only the half after the dot and saved a token that
    could never work, so 'saved it' was followed forever by 'not connected'."""
    i, p = classify(REAL_SHAPE)
    assert i == C.CONNECT, i
    assert p["which"] == "slack", p["which"]
    assert p["token"] == REAL_SHAPE, "token was truncated: " + p["token"][:24]


def test_every_slack_token_shape_is_recognised_as_slack():
    """xoxe. tokens fell through the prefix check, so a pasted token silently
    turned into 'here is what is connected' instead of connecting anything."""
    for tok in (REAL_SHAPE, "xoxe-1-Mi0yLTg2NTY4OTMz",
                "xoxp-123456789012", "xoxb-123456789012"):
        i, p = classify(tok)
        assert i == C.CONNECT and p["which"] == "slack", tok[:12]


def test_an_unauthorized_mcp_server_cannot_shadow_a_working_token():
    """'MCP wins on a name collision' meant an added-but-never-authorized Slack
    MCP server hid a live token: connecting reported success while every
    question reported failure, with nothing to explain the disagreement."""
    live, dead = _Fake(True), _Fake(False)
    reg, servers = dict(connectors.REGISTRY), connectors.mcp_servers
    try:
        connectors.REGISTRY["zzz"] = live
        connectors.mcp_servers = lambda: {"zzz": dead}
        assert connectors.get("zzz") is live, "dead MCP shadowed a live token"
        connectors.REGISTRY["zzz"] = dead
        connectors.mcp_servers = lambda: {"zzz": live}
        assert connectors.get("zzz") is live, "MCP should win when it works"
    finally:
        connectors.REGISTRY.clear()
        connectors.REGISTRY.update(reg)
        connectors.mcp_servers = servers


def test_connected_means_it_can_read_not_just_that_a_token_exists():
    """auth.test needs no scopes at all, so it passes for a token that can read
    nothing. Trusting it reported 'slack connected' and then failed every
    single question."""
    sl, calls = connectors.Slack(), []

    def fake(method, **params):
        calls.append(method)
        if method == "auth.test":
            return {"ok": True, "user": "kojha"}
        return {"ok": False, "error": "missing_scope"}

    sl._call, sl.token = fake, (lambda: "xoxp-" + "0" * 20)
    sl._checked = {}
    assert sl.ready() is False, "a scopeless token must not count as connected"
    assert len(calls) > 1, "ready() only asked auth.test, which proves nothing"


def test_a_refusal_is_reported_as_a_refusal_not_as_an_empty_result():
    """'Nothing in Slack about X' for a permission error is the bug that sent
    the same fix-it loop round three times: an empty list and a flat refusal
    looked identical from the outside."""
    sl = connectors.Slack()
    sl._err = "missing_scope"
    why = sl.last_error()
    assert why, "a refusal must be explainable"
    assert "channels:history" in why, "say WHICH scopes to add: " + why
    sl._err = ""
    assert sl.last_error() == "", "no error means no complaint"


def test_the_fix_it_message_says_what_to_change_not_to_start_over():
    """Handing someone with an installed app the 'create an app' steps is how
    the same failure repeats. With a token saved, the hint must describe the
    change from where they actually are."""
    sl = connectors.Slack()
    sl.token = lambda: "xoxp-" + "0" * 20
    sl.token_problem = lambda: "your Slack app has no read permission yet"
    hint = sl.setup_hint()
    assert "new_app=1" not in hint, "told an existing app to be recreated"
    assert "read permission" in hint, hint


def test_a_missed_click_can_be_retried_with_nothing_new():
    """The Allow click is the one step Friday cannot do for you, so missing it
    must cost nothing. It used to mean generating another configuration token
    and creating a second app, which is how a workspace ends up with four apps
    all called Friday."""
    saved = connectors._secret
    try:
        connectors._secret = lambda n: (
            '{"app_id": "A1", "client_id": "c", "client_secret": "s"}'
            if n == "slack_app" else "")
        assert connectors.can_resume() is True
        connectors._secret = lambda n: '{"app_id": "A1", "client_id": "c"}'
        assert connectors.can_resume() is False, "no secret means no retry"
    finally:
        connectors._secret = saved


def test_the_instructions_describe_the_short_path():
    """Friday used to hand out the six-screen version (create an app, add ten
    scopes by hand, install, copy a token) after it had become able to do all of
    that itself."""
    hint = connectors.Slack().setup_hint()
    assert "Generate Token" in hint, hint
    assert "scopes" not in hint.lower(), "still telling you to add scopes"


class _Fake:
    def __init__(self, ok):
        self._ok = ok

    def ready(self):
        return self._ok

    def setup_hint(self):
        return ""


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  slack: token shapes, no shadowing, honest connection state")
