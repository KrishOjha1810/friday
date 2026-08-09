"""MCP: one protocol, and it must not lie about being connected.

Run: python3 tests/test_mcp.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from friday import mcp  # noqa: E402


def test_write_tools_are_hidden_unless_explicitly_allowed():
    """A misheard sentence must never be able to post to your team's Slack."""
    for name in ("send_message", "create_issue", "delete_file",
                 "post_comment", "update_ticket", "merge_pull_request"):
        assert mcp._is_write(name), name
    for name in ("search_messages", "get_thread", "list_channels",
                 "read_file", "search_issues"):
        assert not mcp._is_write(name), name


def test_a_read_only_server_refuses_a_write_call_without_asking_anyone():
    c = mcp.Client("t", "http://127.0.0.1:1/mcp", token="x", writable=False)
    out = c.call("send_message", {"text": "oops"})
    assert "error" in out and "read-only" in out["error"]


def test_a_writable_server_is_allowed_but_must_be_opted_into():
    c = mcp.Client("t", "http://127.0.0.1:1/mcp", token="x", writable=True)
    out = c.call("send_message", {"text": "fine"})
    # it will fail to connect (port 1), but NOT because we blocked it
    assert "read-only" not in str(out)


def test_both_json_and_sse_answers_are_understood():
    assert mcp._parse_body('{"result":{"a":1}}')["result"]["a"] == 1
    assert mcp._parse_body('event: message\ndata: {"result":{"a":2}}\n\n'
                           )["result"]["a"] == 2
    assert mcp._parse_body("") == {}
    assert mcp._parse_body("garbage") == {}


def test_tool_output_becomes_plain_text():
    out = mcp._flatten({"content": [{"type": "text", "text": "hello"},
                                    {"type": "text", "text": "world"}]})
    assert "hello" in out and "world" in out


def test_an_unreachable_server_fails_soft_not_loud():
    c = mcp.Client("t", "http://127.0.0.1:1/mcp")
    err = c.connect()
    assert "error" in err            # reported
    assert c.tools() == []           # and never raises


def test_config_is_stored_owner_only():
    import os
    mcp.add_server("__test__", "https://example.com/mcp")
    try:
        assert "__test__" in mcp.servers()
        mode = os.stat(mcp.SERVERS_FILE).st_mode & 0o777
        assert mode == 0o600, f"config is {oct(mode)}"
    finally:
        mcp.remove_server("__test__")
    assert "__test__" not in mcp.servers()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  mcp: writes gated, transports parsed, fails soft, config locked")
