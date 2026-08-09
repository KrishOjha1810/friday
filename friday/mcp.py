"""Friday as an MCP client: connect a tool once, use it forever.

Hand-writing a connector per service does not scale, and it puts the setup
burden on you: go make a token, give it these scopes, paste it here. MCP fixes
both. One protocol implementation, and every server anyone has written works,
Slack, Jira, Notion, Linear, whatever comes next, with no new code in Friday.

Auth is a browser round trip, not a token hunt: Friday discovers the server's
authorization endpoint, registers itself, opens your browser, you approve, and
the token lands in a file only you can read. That is the "connect once" you
wanted.

Two deliberate constraints:

  * The token is FRIDAY'S, obtained in your name. It is never taken from
    another application's storage. Credentials issued to Claude belong to
    Claude; borrowing them would work and would still be wrong.
  * Read-only until you say otherwise. Tools whose names say they write are
    hidden unless explicitly allowed, because a misheard sentence should never
    post to your team's Slack.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CONF = Path.home() / ".friday"
SERVERS_FILE = CONF / "mcp_servers.json"
CALLBACK_PORT = 8799
TIMEOUT = 20.0

# Tools whose names announce that they change something. Hidden unless the
# server is explicitly marked writable, so "search my slack" can never become
# "post to my slack" through a misheard word.
_WRITE_HINTS = ("create", "update", "delete", "send", "post", "write", "add",
                "remove", "edit", "merge", "close", "archive", "invite",
                "transition", "assign", "comment", "upload", "move", "set")


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _write_json(p: Path, data) -> None:
    CONF.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    try:
        p.chmod(0o600)
    except Exception:
        pass


def servers() -> dict:
    """{name: {url, token, writable}}"""
    return _read_json(SERVERS_FILE, {})


def add_server(name: str, url: str, writable: bool = False) -> None:
    s = servers()
    s[name] = {**s.get(name, {}), "url": url, "writable": writable}
    _write_json(SERVERS_FILE, s)


def remove_server(name: str) -> bool:
    s = servers()
    if name in s:
        s.pop(name)
        _write_json(SERVERS_FILE, s)
        return True
    return False


# ------------------------------------------------------------ the protocol --
class Client:
    """One MCP server, over streamable HTTP.

    Only the three calls Friday needs: initialize (handshake), tools/list
    (what can this do), tools/call (do it). Notifications, prompts and
    resources are deliberately not implemented; they are not needed to make a
    tool usable, and every line here is a line that can break."""

    def __init__(self, name: str, url: str, token: str = "", writable=False):
        self.name, self.url, self.token = name, url, token
        self.writable = writable
        self.session_id = ""
        self._id = 0

    def _rpc(self, method: str, params: dict = None, timeout: float = TIMEOUT):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id,
                           "method": method, "params": params or {}}).encode()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                raw = r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"error": {"code": e.code, "message": "unauthorized"}}
            return {"error": {"code": e.code, "message": str(e)[:150]}}
        except Exception as e:
            return {"error": {"code": -1, "message": str(e)[:150]}}
        return _parse_body(raw)

    def connect(self) -> dict:
        """Handshake. Returns {} on success, or {'error': …} to be shown."""
        d = self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "friday", "version": "0.1"}})
        if d.get("error"):
            return d
        # the spec wants this notification after initialize
        try:
            self._notify("notifications/initialized")
        except Exception:
            pass
        return {}

    def _notify(self, method: str):
        body = json.dumps({"jsonrpc": "2.0", "method": method}).encode()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            urllib.request.urlopen(
                urllib.request.Request(self.url, data=body, headers=headers),
                timeout=8)
        except Exception:
            pass

    def tools(self) -> list:
        d = self._rpc("tools/list")
        if d.get("error"):
            return []
        rows = (d.get("result") or {}).get("tools") or []
        if self.writable:
            return rows
        return [t for t in rows if not _is_write(t.get("name", ""))]

    def call(self, tool: str, args: dict) -> dict:
        if not self.writable and _is_write(tool):
            return {"error": f"{tool} changes things, and {self.name} is "
                             f"connected read-only."}
        d = self._rpc("tools/call", {"name": tool, "arguments": args or {}},
                      timeout=45)
        if d.get("error"):
            return {"error": d["error"].get("message", "call failed")}
        return {"result": _flatten(d.get("result") or {})}


def _is_write(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _WRITE_HINTS)


def _parse_body(raw: str) -> dict:
    """MCP may answer as JSON or as SSE; accept either."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    out = {}
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                out = json.loads(line[5:].strip())
            except Exception:
                pass
    return out


def _flatten(result: dict) -> str:
    """Tool output as plain text Friday can read out."""
    parts = []
    for c in (result.get("content") or []):
        if isinstance(c, dict):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "resource":
                parts.append(json.dumps(c.get("resource", {}))[:400])
    if not parts and result:
        parts.append(json.dumps(result)[:800])
    return "\n".join(p for p in parts if p)[:4000]


# ----------------------------------------------------------------- OAuth ----
def _discover(url: str) -> dict:
    """Find the server's authorization metadata (RFC 9728 / 8414)."""
    base = urllib.parse.urlsplit(url)
    root = f"{base.scheme}://{base.netloc}"
    for probe in (f"{root}/.well-known/oauth-protected-resource",
                  f"{root}/.well-known/oauth-authorization-server"):
        try:
            with urllib.request.urlopen(probe, timeout=8) as r:
                d = json.loads(r.read())
            if "authorization_servers" in d:
                srv = d["authorization_servers"][0]
                try:
                    with urllib.request.urlopen(
                            srv.rstrip("/") + "/.well-known/oauth-authorization-server",
                            timeout=8) as r2:
                        return json.loads(r2.read())
                except Exception:
                    continue
            if "authorization_endpoint" in d:
                return d
        except Exception:
            continue
    return {}


class _Catcher(BaseHTTPRequestHandler):
    code = None

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        _Catcher.code = (q.get("code") or [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body style='font:16px system-ui;padding:40px'>"
                         b"<h2>Connected.</h2><p>You can close this tab and go "
                         b"back to Friday.</p></body></html>")

    def log_message(self, *a):
        pass


def can_authorize(url: str) -> bool:
    """True only if a browser flow can actually SUCCEED here.

    Advertising an authorization endpoint is not enough: without dynamic client
    registration there is no client id to send, and the server will reject us.
    Checking this first is what stops Friday from opening a browser tab that
    can only ever end in an error."""
    meta = _discover(url)
    return bool(meta.get("authorization_endpoint")
                and meta.get("registration_endpoint"))


def authorize(name: str, url: str, timeout: float = 180) -> dict:
    """The one-time browser approval. Returns {'ok': True} or {'error': …}.

    Standard authorization-code flow with PKCE, and a throwaway local server to
    catch the redirect. Nothing is typed, nothing is pasted.

    BLOCKS until you approve, so callers on a request path must run it in the
    background: a three-minute HTTP request is indistinguishable from a hang."""
    meta = _discover(url)
    if not meta.get("authorization_endpoint"):
        return {"error": "this server does not advertise OAuth; it may need a "
                         "token instead"}
    redirect = f"http://127.0.0.1:{CALLBACK_PORT}/callback"

    client_id = _register(meta, redirect)
    if not client_id:
        return {"error": "this server needs an app registered by hand; it does "
                         "not support automatic registration"}
    verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)

    params = {"response_type": "code", "client_id": client_id,
              "redirect_uri": redirect, "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    if meta.get("scopes_supported"):
        params["scope"] = " ".join(meta["scopes_supported"][:8])

    _Catcher.code = None
    srv = HTTPServer(("127.0.0.1", CALLBACK_PORT), _Catcher)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    webbrowser.open(meta["authorization_endpoint"] + "?" +
                    urllib.parse.urlencode(params))

    t0 = time.time()
    while time.time() - t0 < timeout and not _Catcher.code:
        time.sleep(0.4)
    srv.shutdown()
    if not _Catcher.code:
        return {"error": "no approval came back in time"}

    tok = _exchange(meta, client_id, _Catcher.code, verifier, redirect)
    if not tok:
        return {"error": "the server refused the code exchange"}
    s = servers()
    s.setdefault(name, {})["url"] = url
    s[name]["token"] = tok
    _write_json(SERVERS_FILE, s)
    return {"ok": True}


def _register(meta: dict, redirect: str) -> str:
    """Dynamic client registration, when the server offers it."""
    ep = meta.get("registration_endpoint")
    if not ep:
        return ""
    body = json.dumps({"client_name": "Friday",
                       "redirect_uris": [redirect],
                       "grant_types": ["authorization_code"],
                       "response_types": ["code"],
                       "token_endpoint_auth_method": "none"}).encode()
    try:
        req = urllib.request.Request(ep, data=body, headers={
            "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read()).get("client_id", "")
    except Exception:
        return ""


def _exchange(meta: dict, client_id: str, code: str, verifier: str,
              redirect: str) -> str:
    ep = meta.get("token_endpoint")
    if not ep:
        return ""
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect, "client_id": client_id,
        "code_verifier": verifier}).encode()
    try:
        req = urllib.request.Request(ep, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("access_token", "")
    except Exception:
        return ""


# ------------------------------------------------------------- convenience --
def client(name: str):
    s = servers().get(name)
    if not s:
        return None
    return Client(name, s["url"], s.get("token", ""), s.get("writable", False))


def status() -> dict:
    """Every configured server and whether it actually answers."""
    out = {}
    for name, cfg in servers().items():
        c = Client(name, cfg["url"], cfg.get("token", ""), cfg.get("writable"))
        err = c.connect()
        out[name] = {"url": cfg["url"],
                     "connected": not err,
                     "needs_auth": bool(err and "unauthor" in
                                        str(err.get("error", {})).lower()),
                     "writable": bool(cfg.get("writable")),
                     "error": (err.get("error") or {}).get("message", "")
                     if err else ""}
    return out
