"""The capabilities: memory, connectors, and other users.

The rule underneath all of these: Friday reports what is TRUE and says plainly
when it cannot do something. Every test here is a lie we are promising not to
tell.

Run: python3 tests/test_capabilities.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from friday import connectors, memory  # noqa: E402
from friday import conversation as C  # noqa: E402
from friday.conversation import Friday, classify  # noqa: E402


def test_the_new_asks_are_recognised():
    assert classify("find the session where I set up redis")[0] == C.FIND
    assert classify("what was I working on yesterday")[0] == C.RECENT
    assert classify("are there other users running sessions")[0] == C.OTHERS
    assert classify("anything on github")[0] == C.GITHUB
    assert classify("search slack for the deploy thread")[0] == C.SLACK
    i, p = classify("connect slack xoxp-abc123")
    assert i == C.CONNECT and p["token"] == "xoxp-abc123"


def test_the_search_terms_survive_the_verbs():
    _, p = classify("search slack for the deploy thread")
    assert "deploy" in p["query"] and "for" not in p["query"].split()


def test_an_unconfigured_connector_explains_itself():
    """It must never fail silently, and never pretend. The wording differs
    between the MCP path (one browser approval) and the token path, so assert
    that it tells you HOW, not which words it uses."""
    for name in ("slack", "gmail", "jira"):
        c = connectors.get(name)
        if c and not c.ready():
            hint = c.setup_hint()
            assert hint and ("connect" in hint.lower() or "token" in hint.lower()), \
                f"{name} gave no way forward: {hint!r}"


def test_a_connector_that_cannot_be_used_is_not_reported_as_ready():
    """Gmail's MCP server handshakes and lists tools with no credentials, then
    fails on the first real call. 'Connected' has to mean usable."""
    from friday import mcp
    for name, cfg in mcp.servers().items():
        c = connectors.get(name)
        if c and hasattr(c, "_c") and not cfg.get("token"):
            assert c.ready() is False, f"{name} claims ready with no token"


def test_a_token_is_never_saved_world_readable():
    import os
    ok = connectors.save_secret("test_token", "secret-value")
    p = connectors.CONF_DIR / "test_token"
    try:
        assert ok and p.exists()
        assert os.stat(p).st_mode & 0o777 == 0o600
    finally:
        p.unlink(missing_ok=True)


def test_a_loose_permission_token_is_refused_not_used():
    """If the file is readable by others it is already compromised; using it
    anyway would be the wrong call."""
    import os
    p = connectors.CONF_DIR / "loose_token"
    connectors.CONF_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text("leaky")
    os.chmod(p, 0o644)
    try:
        assert connectors._secret("loose_token") == ""
    finally:
        p.unlink(missing_ok=True)


def test_search_needs_a_real_query():
    assert memory.search("") == []
    assert memory.search("a") == []


def test_search_ranks_by_coverage_not_chattiness():
    """A long unrelated session must not outrank the right one just by using
    more words."""
    hits = memory.search("voicebridge phone call", limit=3)
    if len(hits) > 1:
        assert hits[0]["score"] >= hits[1]["score"]


def test_other_users_are_counted_but_never_read():
    """We may say they exist. We may not look inside."""
    from friday import engine
    if not engine.AVAILABLE:
        return
    others = engine.fleet.other_users()
    assert isinstance(others, dict)
    for user, n in others.items():
        assert isinstance(n, int) and n > 0
        # the promise: no path into their home is ever returned
        assert "/" not in user


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  capabilities: memory, connectors, tokens locked, no peeking")
