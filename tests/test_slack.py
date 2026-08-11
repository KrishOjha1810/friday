"""Regressions from one real failure: three separate bugs, each of which made
Slack look "not connected" for a different reason, and none of which said so.

The user pasted a working token three times and got the same unhelpful sentence
every time. Every test here exists because a wrong answer was indistinguishable
from a right one from the outside.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import use_temp_config  # noqa: E402

use_temp_config()   # never touch the real ~/.friday: a test once
                    # deleted a live Slack token this way

from friday import connectors
from friday import conversation as C
from friday.conversation import classify


# The SHAPE of a rotating Slack token, synthetic. It carries nothing from a
# real credential: what matters here is the xoxe. prefix and the dot.
REAL_SHAPE = ("xoxe.xoxp-1-AAAAAAAAAAAAAAAAAAAABBBBBBBBBBBBBBBBBBBB-CCCCCCCCCCCCCCCCCC"
              "DDDDDDDDDDDDDDDDDDDDDDDD")


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
    for tok in (REAL_SHAPE, "xoxe-1-EEEEEEEEEEEEEEEEEEEE",
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


def test_a_misheard_channel_name_still_finds_the_channel():
    """Speech recognition mangles proper nouns, and every channel name is one.
    'moonshot' came back as 'moon shot', 'moon shot' and 'neither of
    mine' on consecutive tries, and substring matching rescued none of them, so
    Friday failed three times on a channel sitting in a list it already had."""
    rows = [{"id": "C1", "name": "moonshot"}, {"id": "C2", "name": "general"},
            {"id": "C3", "name": "random"}]
    sl = connectors.Slack()
    for heard in ("moon shot", "moon shot", "moon of shot", "Moonshot"):
        got = sl.closest_channel(heard, rows)
        assert got.get("name") == "moonshot", f"{heard} -> {got}"


def test_a_name_that_matches_nothing_is_not_forced_onto_a_channel():
    """Reading the wrong channel is worse than asking which one. A weak or
    ambiguous match must return nothing so the caller can offer a choice."""
    rows = [{"id": "C1", "name": "alpha"}, {"id": "C2", "name": "alphb"}]
    sl = connectors.Slack()
    assert sl.closest_channel("zzzzzz", rows) == {}, "matched unrelated words"
    # equally close to both: reading one of them would be a coin toss
    assert sl.closest_channel("alphc", rows) == {}, "acted on a near-tie"


def test_slack_markup_never_reaches_you_raw():
    """Slack sends <@U08MBL3RPL2>, not a name. Left alone it lands in the
    summary and then the speaker, which reads a user id out one character at a
    time."""
    sl = connectors.Slack()
    sl._user_names = lambda ids: {"U08MBL3RPL2": "Krish Ojha"}
    rows = [{"text": "Hey <@U08MBL3RPL2> see <#C1ABC|general> "
                     "and <https://x.com|the doc> &amp; reply"}]
    out = sl._unmarkup(rows)[0]["text"]
    assert "U08MBL3RPL2" not in out, out
    assert "@Krish Ojha" in out and "#general" in out and "the doc" in out, out
    assert "&amp;" not in out, out


def test_a_named_question_is_answered_not_just_summarised():
    """Asked what one person said, returning the same general summary is a way
    of not listening. The question has to reach the summariser."""
    from friday.conversation import Friday
    seen = {}
    f = Friday()
    f._summarise_thread = lambda convo, question="": seen.setdefault(
        "q", question) or "ok"
    sl = connectors.get("slack")
    if not sl.ready():
        return                      # nothing connected in this environment
    f._read_channel_named(sl.channel_names(1)[0], "what did sam say")
    assert "sam" in (seen.get("q") or "").lower(), seen


def test_the_suite_cannot_touch_your_real_credentials():
    """A test wrote a fake token over ~/.friday/slack_token and deleted it in
    cleanup, so every run of the suite destroyed a live Slack credential that
    took two rounds of setup to get. It read as a mysterious disappearance for
    hours. No test may write anywhere a person would miss."""
    real = Path.home() / ".friday"
    assert connectors.CONF_DIR != real, "tests are pointed at the real config dir"
    from friday import mcp
    assert mcp.SERVERS_FILE.parent != real, "mcp config is pointed at the real dir"


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
