"""Friday's own connections to your tools.

These belong to FRIDAY, not to Claude. When you ask Friday to find a Slack
thread, Friday fetches it, reads it, and decides what to tell you. Nothing is
handed to a coding agent unless you say so. That distinction is the whole point
of a conductor: it is the thing with the context, and the agents are workers it
hands pieces to.

Design rules, all of them learned the hard way elsewhere in this codebase:

  * A connector that is not set up says exactly how to set it up. It never
    pretends, and it never fails silently.
  * Read-only by default. Nothing here posts, comments, merges or deletes.
    Writing is a separate, explicitly-confirmed act.
  * Credentials live in files only you can read, never in the code, never in a
    URL, never logged.
  * Every call is bounded and fails soft: a dead network makes Friday say so,
    not hang.
"""

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

CONF_DIR = Path.home() / ".friday"
TIMEOUT = 12.0


def _run(cmd: list, timeout: float = TIMEOUT) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _secret(name: str) -> str:
    """Read a token from ~/.friday/<name>, owner-only."""
    p = CONF_DIR / name
    try:
        if p.stat().st_mode & 0o077:
            # too permissive: refuse rather than quietly leak
            return ""
        return p.read_text().strip()
    except Exception:
        return ""


def save_secret(name: str, value: str) -> bool:
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        p = CONF_DIR / name
        p.write_text(value.strip())
        p.chmod(0o600)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- GitHub ----
class GitHub:
    """Uses the `gh` CLI, which is already signed in as you. No new tokens, no
    OAuth dance, and it inherits exactly your permissions, which is the right
    default for something reading your work."""

    name = "github"

    def ready(self) -> bool:
        return bool(_run(["gh", "auth", "status"], 8))

    def setup_hint(self) -> str:
        return "run `gh auth login` in a terminal, then ask me again"

    def me(self) -> str:
        out = _run(["gh", "api", "user", "--jq", ".login"], 8)
        return out.strip()

    def my_prs(self, limit: int = 6) -> list:
        """Open pull requests involving you, across every repo."""
        out = _run(["gh", "search", "prs", "--involves", "@me", "--state", "open",
                    "--limit", str(limit), "--json",
                    "title,repository,url,updatedAt,isDraft"], 20)
        try:
            return json.loads(out) if out else []
        except Exception:
            return []

    def notifications(self, limit: int = 8) -> list:
        """What GitHub thinks needs your attention."""
        out = _run(["gh", "api", "/notifications",
                    "--jq", "[.[] | {reason, title: .subject.title, "
                            "repo: .repository.full_name, type: .subject.type, "
                            "updated: .updated_at}]"], 15)
        try:
            rows = json.loads(out) if out else []
            return rows[:limit]
        except Exception:
            return []

    def search(self, query: str, limit: int = 5) -> list:
        """Issues and PRs matching a query, in repos you can see."""
        out = _run(["gh", "search", "issues", query, "--limit", str(limit),
                    "--json", "title,repository,url,state,updatedAt"], 20)
        try:
            return json.loads(out) if out else []
        except Exception:
            return []


# ----------------------------------------------------------------- Slack ----
class Slack:
    """Talks to Slack's Web API directly with YOUR user token.

    A user token (xoxp-) is used rather than a bot token on purpose: Friday
    should see what you see, including your DMs and private channels, and it
    should never see anything you cannot. Read scopes only."""

    name = "slack"
    BASE = "https://slack.com/api/"

    def token(self) -> str:
        return os.environ.get("SLACK_TOKEN") or _secret("slack_token")

    def ready(self) -> bool:
        return bool(self.token())

    def setup_hint(self) -> str:
        return ("create a Slack app at api.slack.com/apps, give it the user "
                "scopes search:read, channels:history and users:read, install "
                "it to your workspace, then save the xoxp- token with: "
                "friday connect slack <token>")

    def _call(self, method: str, **params) -> dict:
        tok = self.token()
        if not tok:
            return {"ok": False, "error": "not_configured"}
        try:
            url = self.BASE + method + "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + tok})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read())
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}

    def whoami(self) -> str:
        d = self._call("auth.test")
        return d.get("user", "") if d.get("ok") else ""

    def search(self, query: str, limit: int = 5) -> list:
        """Messages matching a query, newest first, as plain rows."""
        d = self._call("search.messages", query=query, count=limit, sort="timestamp")
        if not d.get("ok"):
            return []
        out = []
        for m in (d.get("messages", {}).get("matches") or [])[:limit]:
            out.append({
                "text": " ".join((m.get("text") or "").split())[:300],
                "who": (m.get("username") or m.get("user") or "someone"),
                "channel": (m.get("channel") or {}).get("name", ""),
                "when": float(m.get("ts") or 0),
                "url": m.get("permalink", ""),
            })
        return out

    def thread(self, channel: str, ts: str, limit: int = 30) -> list:
        """A whole conversation, so Friday can read it and summarise."""
        d = self._call("conversations.replies", channel=channel, ts=ts, limit=limit)
        if not d.get("ok"):
            return []
        return [{"who": m.get("user", ""),
                 "text": " ".join((m.get("text") or "").split())[:500],
                 "when": float(m.get("ts") or 0)}
                for m in (d.get("messages") or [])]


# -------------------------------------------------------------- registry ----
REGISTRY = {c.name: c() for c in (GitHub, Slack)}


def status() -> dict:
    """Which connections are live, and how to fix the ones that are not."""
    out = {}
    for name, c in REGISTRY.items():
        try:
            ok = c.ready()
        except Exception:
            ok = False
        out[name] = {"ready": ok, "hint": "" if ok else c.setup_hint()}
    return out


def get(name: str):
    return REGISTRY.get((name or "").lower())


def when(ts: float) -> str:
    d = max(0, time.time() - (ts or 0))
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"
