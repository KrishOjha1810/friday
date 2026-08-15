"""MCP: one protocol, and it must not lie about being connected.

Run: python3 tests/test_mcp.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import use_temp_config  # noqa: E402

use_temp_config()   # never touch the real ~/.friday: a test once
                    # deleted a live Slack token this way
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


# ---- the 2026-07-28 protocol, and the holes found bringing it up to date ----

def test_every_request_carries_its_own_version_and_identity():
    """MCP became stateless: there is no handshake to establish who you are,
    so it travels with each request or the server has no idea."""
    from friday import mcp as M
    c = M.Client("x", "https://example.test/mcp")
    seen = {}

    def fake(url, data=None, headers=None, method=None):
        seen["body"] = json.loads(data)
        seen["headers"] = headers or {}
        raise RuntimeError("stop here, the request is what matters")

    real = M.urllib.request.Request
    M.urllib.request.Request = fake
    try:
        c._rpc("tools/list")
    except Exception:
        pass
    finally:
        M.urllib.request.Request = real
    meta = seen["body"]["params"]["_meta"]
    assert meta[M.META_VERSION] == M.PROTOCOL, meta
    assert meta[M.META_INFO]["name"] == "friday", meta
    assert M.META_CAPS in meta, meta
    # required on POST so a proxy can route without reading the body
    assert seen["headers"].get("Mcp-Method") == "tools/list", seen["headers"]
    assert seen["headers"].get("Mcp-Name"), seen["headers"]


def test_an_interim_result_is_not_reported_as_an_answer():
    """A server can come back asking for something before it can answer.
    Treating that as the result reports an empty success for work that never
    happened."""
    from friday import mcp as M
    c = M.Client("x", "https://example.test/mcp")
    c._rpc = lambda *a, **k: {"result": {
        "resultType": "input_required",
        "inputRequests": [{"method": "elicitation/create"}]}}
    out = c.call("search", {})
    assert "error" in out, out
    assert "needs something" in out["error"], out


def test_a_result_without_the_field_is_treated_as_finished():
    """Older servers omit resultType, and the spec says treat that as
    complete. Getting this backwards would break every existing server."""
    from friday import mcp as M
    c = M.Client("x", "https://example.test/mcp")
    c._rpc = lambda *a, **k: {"result": {"content": [
        {"type": "text", "text": "done"}]}}
    out = c.call("search", {})
    assert "result" in out and "done" in out["result"], out


def test_a_callback_that_does_not_match_the_request_is_refused():
    """The loopback callback accepted ANY code, which is the textbook CSRF
    against an OAuth client: anything that can open a URL in your browser could
    hand Friday a code."""
    from friday import mcp as M
    M._Catcher.state = "the-real-one"
    M._Catcher.code = "untouched"

    class Fake(M._Catcher):
        def __init__(self, path):
            self.path = path

        def send_response(self, *a):
            pass

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass

        @property
        def wfile(self):
            class W:
                def write(self, *_a):
                    pass
            return W()

    Fake("/cb?code=stolen&state=someone-elses").do_GET()
    assert M._Catcher.code is None, "accepted a code from a mismatched state"
    Fake("/cb?code=good&state=the-real-one&iss=https://auth.example").do_GET()
    assert M._Catcher.code == "good"
    assert M._Catcher.issuer == "https://auth.example"


def test_registration_declares_a_native_client():
    """Without application_type an OpenID provider assumes a web client and
    rejects the loopback redirect, failing with a message about redirect URIs
    that never mentions the real cause."""
    from friday import mcp as M
    sent = {}

    def fake(url, data=None, headers=None):
        sent.update(json.loads(data))
        raise RuntimeError("enough")

    real = M.urllib.request.Request
    M.urllib.request.Request = fake
    try:
        M._register({"registration_endpoint": "https://auth.example/reg"}, "http://127.0.0.1:1/cb")
    except Exception:
        pass
    finally:
        M.urllib.request.Request = real
    assert sent.get("application_type") == "native", sent
