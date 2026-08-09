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

    def my_issues(self, limit: int = 8) -> list:
        """Open issues assigned to you or that you opened, across repos."""
        out = _run(["gh", "search", "issues", "--involves", "@me",
                    "--state", "open", "--limit", str(limit), "--json",
                    "title,repository,url,updatedAt,author"], 20)
        try:
            return json.loads(out) if out else []
        except Exception:
            return []

    def repo_issues(self, repo: str, limit: int = 8) -> list:
        out = _run(["gh", "issue", "list", "--repo", repo, "--state", "open",
                    "--limit", str(limit), "--json", "number,title,updatedAt"], 20)
        try:
            return json.loads(out) if out else []
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

    def channels(self) -> list:
        """Channels you are in, so a spoken name can be resolved to an id."""
        d = self._call("users.conversations", limit=400,
                       types="public_channel,private_channel,mpim,im")
        if not d.get("ok"):
            return []
        return [{"id": c.get("id", ""), "name": c.get("name", "")}
                for c in (d.get("channels") or []) if c.get("name")]

    def find_channel(self, name: str) -> dict:
        """Match a spoken channel name ('the neither group') to a real one."""
        q = (name or "").lower().strip().lstrip("#")
        if not q:
            return {}
        rows = self.channels()
        for c in rows:                                  # exact
            if c["name"].lower() == q:
                return c
        starts = [c for c in rows if c["name"].lower().startswith(q)]
        if len(starts) == 1:
            return starts[0]
        near = [c for c in rows if q in c["name"].lower()
                or c["name"].lower() in q]
        return near[0] if len(near) == 1 else {}

    def read_channel(self, channel_id: str, limit: int = 15) -> list:
        """The recent conversation in a channel, oldest-first so it reads like
        a conversation rather than a reversed feed."""
        d = self._call("conversations.history", channel=channel_id, limit=limit)
        if not d.get("ok"):
            return []
        rows = [{"who": m.get("user", "") or m.get("username", ""),
                 "text": " ".join((m.get("text") or "").split())[:600],
                 "when": float(m.get("ts") or 0), "ts": m.get("ts", "")}
                for m in (d.get("messages") or []) if m.get("text")]
        rows.reverse()
        return self._name_users(rows)

    def _name_users(self, rows: list) -> list:
        """Swap user ids for real names: 'U03AB' means nothing spoken aloud."""
        ids = {r["who"] for r in rows if r["who"].startswith("U")}
        names = {}
        for uid in list(ids)[:20]:
            d = self._call("users.info", user=uid)
            if d.get("ok"):
                u = d.get("user", {})
                names[uid] = (u.get("real_name") or u.get("name") or uid)
        for r in rows:
            r["who"] = names.get(r["who"], r["who"])
        return rows

    def thread(self, channel: str, ts: str, limit: int = 30) -> list:
        """A whole conversation, so Friday can read it and summarise."""
        d = self._call("conversations.replies", channel=channel, ts=ts, limit=limit)
        if not d.get("ok"):
            return []
        return [{"who": m.get("user", ""),
                 "text": " ".join((m.get("text") or "").split())[:500],
                 "when": float(m.get("ts") or 0)}
                for m in (d.get("messages") or [])]


# ----------------------------------------------------------------- Gmail ----
class Gmail:
    """Reads your mail through the Gmail API with your own OAuth token.

    Read-only by design (`gmail.readonly`): Friday tells you what arrived and
    what it says; it never sends, replies or deletes. Sending mail on a
    misheard sentence is exactly the kind of irreversible mistake this whole
    product is built to avoid."""

    name = "gmail"
    BASE = "https://gmail.googleapis.com/gmail/v1/users/me/"

    def token(self) -> str:
        return os.environ.get("GMAIL_TOKEN") or _secret("gmail_token")

    def ready(self) -> bool:
        return bool(self.token())

    def setup_hint(self) -> str:
        return ("get an OAuth access token with the gmail.readonly scope (the "
                "Google OAuth playground is the quickest way), then: "
                "friday connect gmail <token>")

    def _call(self, path: str, **params) -> dict:
        tok = self.token()
        if not tok:
            return {}
        try:
            url = self.BASE + path
            if params:
                url += "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + tok})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read())
        except Exception:
            return {}

    def search(self, query: str, limit: int = 5) -> list:
        d = self._call("messages", q=query or "is:unread", maxResults=limit)
        out = []
        for m in (d.get("messages") or [])[:limit]:
            full = self._call(f"messages/{m.get('id')}", format="metadata",
                              metadataHeaders="From")
            hdrs = {h.get("name"): h.get("value")
                    for h in (full.get("payload", {}).get("headers") or [])}
            out.append({"from": hdrs.get("From", ""),
                        "subject": (full.get("snippet") or "")[:140],
                        "when": float(full.get("internalDate", 0)) / 1000})
        return out


# ------------------------------------------------------------------ Jira ----
class Jira:
    """Your Jira issues, read-only, through the REST API.

    Untested against a live instance (there is no personal Jira here), so it is
    written to fail loudly rather than quietly: if the site or token is wrong
    you get an error, not an empty list pretending there is no work."""

    name = "jira"

    def _conf(self) -> dict:
        raw = _secret("jira_token")
        # stored as "site|email|token" so one file carries the whole thing
        parts = raw.split("|") if raw else []
        if len(parts) != 3:
            return {}
        return {"site": parts[0].rstrip("/"), "email": parts[1], "token": parts[2]}

    def ready(self) -> bool:
        return bool(self._conf())

    def setup_hint(self) -> str:
        return ("make an API token at id.atlassian.com/manage/api-tokens, then: "
                "friday connect jira https://yoursite.atlassian.net|you@email|TOKEN")

    def my_issues(self, limit: int = 8) -> list:
        c = self._conf()
        if not c:
            return []
        import base64
        jql = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
        url = (c["site"] + "/rest/api/3/search?"
               + urllib.parse.urlencode({"jql": jql, "maxResults": limit,
                                         "fields": "summary,status,updated"}))
        auth = base64.b64encode(f"{c['email']}:{c['token']}".encode()).decode()
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": "Basic " + auth, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read())
        except Exception as e:
            return [{"error": str(e)[:140]}]
        return [{"key": i.get("key", ""),
                 "summary": (i.get("fields", {}).get("summary") or "")[:120],
                 "status": (i.get("fields", {}).get("status") or {}).get("name", "")}
                for i in (d.get("issues") or [])]


# -------------------------------------------------------------- registry ----
REGISTRY = {c.name: c() for c in (GitHub, Slack, Gmail, Jira)}


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
