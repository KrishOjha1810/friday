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
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
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

    def failing(self, limit: int = 6) -> list:
        """Workflow runs that failed recently, across your repos.

        This is the question a notification list cannot answer: not 'what
        happened' but 'what is broken right now'. Grouped per repo+workflow so
        five failures of the same nightly job read as one problem, which is
        what it is."""
        out = _run(["gh", "api", "/notifications?all=true",
                    "--jq", "[.[] | select(.subject.type==\"CheckSuite\") | "
                            "{repo: .repository.full_name, "
                            "title: .subject.title, when: .updated_at}]"], 15)
        try:
            rows = json.loads(out) if out else []
        except Exception:
            return []
        seen, grouped = {}, []
        for r in rows:
            title = (r.get("title") or "")
            if "fail" not in title.lower():
                continue
            wf = title.split(" workflow")[0]
            key = (r.get("repo"), wf)
            if key in seen:
                seen[key]["count"] += 1
                continue
            item = {"repo": r.get("repo"), "workflow": wf,
                    "when": r.get("when", ""), "count": 1}
            seen[key] = item
            grouped.append(item)
        return grouped[:limit]

    def runs(self, repo: str, limit: int = 5) -> list:
        """Recent CI runs for one repo, with their conclusions."""
        out = _run(["gh", "run", "list", "--repo", repo, "--limit", str(limit),
                    "--json", "name,conclusion,status,createdAt,headBranch"], 20)
        try:
            return json.loads(out) if out else []
        except Exception:
            return []

    def issue(self, repo: str, number: str) -> dict:
        """One issue in full, with its conversation, so Friday can summarise
        what is actually being asked rather than reading you a title."""
        out = _run(["gh", "issue", "view", str(number), "--repo", repo,
                    "--json", "title,body,state,author,comments,url"], 20)
        try:
            return json.loads(out) if out else {}
        except Exception:
            return {}

    def activity(self, limit: int = 8) -> list:
        """What you have actually been doing on GitHub lately."""
        out = _run(["gh", "api", "/users/{}/events?per_page=30".format(self.me() or ""),
                    "--jq", "[.[] | {type, repo: .repo.name, when: .created_at}]"], 15)
        try:
            rows = json.loads(out) if out else []
        except Exception:
            return []
        seen, grouped = set(), []
        for r in rows:
            key = (r.get("repo"), r.get("type"))
            if key in seen:
                continue
            seen.add(key)
            grouped.append(r)
        return grouped[:limit]

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

    def token_problem(self) -> str:
        """Say precisely what is wrong with the token, if anything.

        Slack has three token shapes that all look similar and behave very
        differently, so 'it isn't answering' is a useless diagnosis. A refresh
        token in particular cannot be used to call the API at all, and no amount
        of retrying will change that."""
        t = self.token()
        if not t:
            return "there's no token saved yet"
        if t.startswith("xoxe.xoxp-") or t.startswith("xoxe-"):
            # Guessing "expired" from the prefix was wrong twice: these are
            # usually App Configuration Tokens, which are perfectly valid and
            # simply cannot read messages. Ask Slack which it is.
            if is_config_token(t):
                return ("that's an App Configuration Token, which can build "
                        "apps but not read messages. Say \"connect slack\" and "
                        "paste it again and I'll use it to set everything up "
                        "for you")
            return ("that token is no longer valid. It's a rotating one, so it "
                    "needs refreshing: say \"connect slack\" and I'll take it "
                    "from there")
        if t.startswith("xoxb-"):
            return ("that's a BOT token. I need the User OAuth Token (xoxp-) so "
                    "I can see what you see, including your DMs")
        if not t.startswith("xoxp-"):
            return "that doesn't look like a Slack token"
        # Probe fresh rather than reporting whatever failed last: a cached
        # ready() makes no calls, so a stale reason would be misleading here.
        self._call("users.conversations", limit=1)
        return self.last_error()

    _checked = {}        # token -> (ok, when): ask Slack once, not per sentence

    def ready(self) -> bool:
        """Connected means the token WORKS.

        Checking only that a token EXISTS accepted a made-up string as
        'connected', which is the worst kind of pass: it looks fine right up to
        the moment you rely on it. Verified against auth.test, cached briefly so
        a conversation does not make an API call per message."""
        tok = self.token()
        if not tok:
            return False
        hit = self._checked.get(tok)
        if hit and time.time() - hit[1] < 300:
            return hit[0]
        ok = bool(self._call("auth.test").get("ok"))
        if ok:
            # auth.test requires NO scopes, so passing it proves only that the
            # token is real. A token with an identity and zero scopes reported
            # "slack connected" and then failed every single question. Connected
            # has to mean "can read something".
            ok = bool(self._call("users.conversations", limit=1).get("ok"))
        self._checked[tok] = (ok, time.time())
        return ok

    # A pre-filled app manifest, so "create an app with these ten scopes" is
    # replaced by clicking a link and pressing Create. The scopes are all READ:
    # history and search, never chat:write. Friday physically cannot post.
    MANIFEST_URL = 'https://api.slack.com/apps?new_app=1&manifest_json=%7B%22display_information%22%3A%20%7B%22name%22%3A%20%22Friday%22%2C%20%22description%22%3A%20%22Read-only%20assistant%20that%20reads%20your%20Slack%20for%20you%22%2C%20%22background_color%22%3A%20%22%230b0d12%22%7D%2C%20%22oauth_config%22%3A%20%7B%22scopes%22%3A%20%7B%22user%22%3A%20%5B%22search%3Aread%22%2C%20%22channels%3Ahistory%22%2C%20%22groups%3Ahistory%22%2C%20%22im%3Ahistory%22%2C%20%22mpim%3Ahistory%22%2C%20%22channels%3Aread%22%2C%20%22groups%3Aread%22%2C%20%22im%3Aread%22%2C%20%22mpim%3Aread%22%2C%20%22users%3Aread%22%5D%7D%7D%2C%20%22settings%22%3A%20%7B%22org_deploy_enabled%22%3A%20false%2C%20%22socket_mode_enabled%22%3A%20false%2C%20%22token_rotation_enabled%22%3A%20false%7D%7D'

    def setup_hint(self) -> str:
        # When a token is already saved, the useful instruction is what to
        # CHANGE from here, not how to start over. Handing someone with an
        # installed app the "create an app" steps is how the same failure
        # repeats three times with nothing to act on.
        if self.token():
            why = self.token_problem()
            if why:
                return ("Right now " + why + ".\n"
                        "Your apps are at https://api.slack.com/apps")
        return ("Two steps, and I do the rest:\n"
                "1. Open https://api.slack.com/apps and press Generate Token "
                "(top of the page, under App Configuration Tokens). Copy it.\n"
                "2. Paste it here on its own. I'll build the app with the right "
                "permissions, then open one page for you to press Allow.\n"
                "Type it rather than saying it out loud; a token can't survive "
                "being dictated.")

    def setup_link(self) -> str:
        return self.MANIFEST_URL

    _err = ""        # why Slack last said no, so it can be repeated to you

    def _call(self, method: str, **params) -> dict:
        tok = self.token()
        if not tok:
            self._err = "not_configured"
            return {"ok": False, "error": "not_configured"}
        try:
            url = self.BASE + method + "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + tok})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read())
        except Exception as e:
            self._err = str(e)[:120]
            return {"ok": False, "error": self._err}
        self._err = "" if d.get("ok") else str(d.get("error") or "unknown")
        return d

    # The scopes the manifest asks for, repeated here so the fix-it message can
    # name them: an app created before these were added authenticates fine and
    # can read nothing.
    SCOPES = ("search:read, channels:history, groups:history, im:history, "
              "mpim:history, channels:read, groups:read, im:read, mpim:read, "
              "users:read")

    def last_error(self) -> str:
        """Slack's last refusal, in words, and what to do about it.

        Without this, every failure looked identical to an empty result: asking
        about a channel returned "nothing found" whether Slack had no messages
        or had flatly refused to answer."""
        e = self._err
        if not e:
            return ""
        if e == "missing_scope":
            return ("your Slack app has no read permission yet. Open the app, "
                    "OAuth & Permissions, and under User Token Scopes add: "
                    + self.SCOPES + ". Then Reinstall to Workspace and paste "
                    "me the new token")
        if e in ("not_in_channel", "channel_not_found"):
            return "I can't see that channel with this token"
        if e in ("invalid_auth", "token_expired", "token_revoked"):
            return "the token is no longer valid, paste me a fresh one"
        if e == "not_configured":
            return "there's no token saved yet"
        if e == "ratelimited":
            return "Slack is rate-limiting me, try again in a minute"
        return "Slack said: " + e

    def whoami(self) -> str:
        """You, and which workspace. Naming the workspace matters as soon as you
        have more than one Slack: Friday's app is installed in exactly one, and
        a channel missing from it is confusing until you know that."""
        d = self._call("auth.test")
        if not d.get("ok"):
            return ""
        who, team = d.get("user", ""), d.get("team", "")
        return f"{who} in {team}" if who and team else who

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
        """Match a spoken channel name to a real one, allowing for mishearing.

        Speech recognition mangles proper nouns badly, and channel names are all
        proper nouns: 'moonshot' came back as 'Munsheer', 'moon shot' and
        'moon of shot' on three consecutive tries. Substring matching cannot
        rescue any of those, so it failed three times in a row on a channel that
        was right there in the list.

        The list is the advantage here. Rather than parse what was said, compare
        it against the handful of names that actually exist, with the spaces
        removed, because a heard name is split into words at the wrong places
        ('moon shot' against 'moonshot')."""
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
        if len(near) == 1:
            return near[0]
        return self.closest_channel(q, rows)

    # A heard name only has to be close, but it does have to WIN. Acting on a
    # 0.62-vs-0.61 tie would be guessing, and guessing which channel to read is
    # worse than asking.
    SOUNDS_LIKE = 0.62
    MUST_BEAT_RUNNER_UP_BY = 0.08

    def closest_channel(self, heard: str, rows: list = None) -> dict:
        """The one channel that clearly sounds like what was said, or nothing."""
        import difflib
        rows = self.channels() if rows is None else rows
        flat = _despace(heard)
        if not flat:
            return {}
        scored = sorted(
            ((difflib.SequenceMatcher(None, flat, _despace(c["name"])).ratio(), c)
             for c in rows), key=lambda t: -t[0])
        if not scored or scored[0][0] < self.SOUNDS_LIKE:
            return {}
        if len(scored) > 1 and scored[0][0] - scored[1][0] < self.MUST_BEAT_RUNNER_UP_BY:
            return {}
        return scored[0][1]

    def channel_names(self, limit: int = 8) -> list:
        """What is actually there, for when nothing matched. Naming the real
        options beats asking someone to rephrase a name they said correctly."""
        return [c["name"] for c in self.channels()[:limit]]

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


def _despace(s: str) -> str:
    """Letters and digits only. Speech splits a compound name at the wrong
    place, so comparing word-by-word is comparing the wrong things."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())

# ------------------------------------------------------- Slack self-setup ----
# Connecting Slack by hand takes six screens: create an app, find User Token
# Scopes, add ten of them one at a time, install, find the token, copy it
# without picking up whitespace. Every one of those steps is a place to get it
# wrong, and getting it wrong looked identical to every other way of getting it
# wrong.
#
# Slack has one credential that makes all of that unnecessary: an App
# Configuration Token, which is generated with a single button at the top of
# api.slack.com/apps and can create apps. Given one, Friday builds the app
# itself with exactly the read scopes it needs, then runs the ordinary OAuth
# click. Two actions for you: generate a token, press Allow.
SCOPE_LIST = ["search:read", "channels:history", "groups:history", "im:history",
              "mpim:history", "channels:read", "groups:read", "im:read",
              "mpim:read", "users:read"]

SETUP_PORT = 7391
CONFIG_TOKEN_SCOPES = "app_configurations"


def is_config_token(token: str) -> bool:
    """Whether this is an App Configuration Token rather than a workspace one.

    Both start with xoxe.xoxp-, so the prefix cannot tell them apart, and a
    config token passes auth.test happily while being unable to read a single
    message. Asking Slack which scopes it carries is the only honest test, and
    Slack returns them in a response header."""
    if not token:
        return False
    try:
        req = urllib.request.Request(
            "https://slack.com/api/auth.test",
            headers={"Authorization": "Bearer " + token}, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return CONFIG_TOKEN_SCOPES in (r.headers.get("x-oauth-scopes") or "")
    except Exception:
        return False


def _post(method: str, token: str, body: dict) -> dict:
    try:
        req = urllib.request.Request(
            "https://slack.com/api/" + method,
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def _form(method: str, body: dict) -> dict:
    try:
        req = urllib.request.Request(
            "https://slack.com/api/" + method,
            data=urllib.parse.urlencode(body).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def _manifest(redirect: str) -> str:
    return json.dumps({
        "display_information": {
            "name": "Friday",
            "description": "Reads your Slack for you. Read-only.",
            "background_color": "#0b0d12"},
        "oauth_config": {
            "redirect_urls": [redirect],
            # USER scopes only, and every one is a read. There is no chat:write
            # anywhere in here, so Friday physically cannot post as you.
            "scopes": {"user": SCOPE_LIST}},
        "settings": {"org_deploy_enabled": False,
                     "socket_mode_enabled": False,
                     # Rotation off, so the result is a plain xoxp- token that
                     # keeps working instead of expiring in twelve hours.
                     "token_rotation_enabled": False}})


class _Catch(BaseHTTPRequestHandler):
    code = None
    state = ""

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        got = (q.get("state") or [""])[0]
        if got and got == _Catch.state:
            _Catch.code = (q.get("code") or [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = ("<h2>Slack connected.</h2><p>Close this tab and go back to "
                "Friday.</p>" if _Catch.code else
                "<h2>That didn't match.</h2><p>Go back to Friday and try "
                "connecting again.</p>")
        self.wfile.write(("<html><body style='font:16px system-ui;padding:40px;"
                          "background:#0b0d12;color:#e6e8ef'>" + body +
                          "</body></html>").encode())

    def log_message(self, *a):
        pass


def setup_from_config_token(config_token: str, timeout: float = 600) -> dict:
    """Build the app, then take you through one click. Returns {'ok'} or {'error'}.

    BLOCKS while waiting for you to approve in the browser, so a caller on a
    request path must run it in a thread: a four-minute HTTP request is
    indistinguishable from a hang."""
    redirect = f"http://localhost:{SETUP_PORT}/slack/callback"
    old_app = app_config().get("app_id")
    if old_app:
        # Tidy up the one from the last attempt rather than leaving a trail of
        # identical half-finished apps behind.
        _post("apps.manifest.delete", config_token, {"app_id": old_app})
    made = _post("apps.manifest.create", config_token,
                 {"manifest": _manifest(redirect)})
    if not made.get("ok"):
        err = str(made.get("error") or "")
        if err in ("invalid_auth", "not_authed", "token_expired"):
            return {"error": "that configuration token has expired (they last "
                             "12 hours). Generate a fresh one at "
                             "https://api.slack.com/apps and paste it again"}
        return {"error": "Slack wouldn't create the app: " + (err or "unknown")}

    creds = made.get("credentials") or {}
    cid, secret = creds.get("client_id", ""), creds.get("client_secret", "")
    app_id = made.get("app_id", "")
    if not (cid and secret):
        return {"error": "Slack made the app but returned no credentials"}
    # Keep the secret too. Without it, a click you did not get to in time meant
    # generating a new configuration token AND creating a second app, which is
    # how one workspace ends up with four apps called Friday.
    save_secret("slack_app", json.dumps({"app_id": app_id, "client_id": cid,
                                         "client_secret": secret}))

    return _approve(cid, secret, app_id, timeout)


def app_config() -> dict:
    """The app Friday built for you last time, if it did."""
    try:
        return json.loads(_secret("slack_app") or "{}")
    except Exception:
        return {}


def can_resume() -> bool:
    """Whether the Allow click can be retried with nothing new from you."""
    d = app_config()
    return bool(d.get("client_id") and d.get("client_secret"))


def resume_setup(timeout: float = 600) -> dict:
    """Re-open the Allow page for the app Friday already built.

    The click is the one step Friday cannot do for you, so missing it must cost
    nothing: no new token, no second app, just the page again."""
    d = app_config()
    if not can_resume():
        return {"error": "I haven't built your Slack app yet"}
    return _approve(d["client_id"], d["client_secret"], d.get("app_id", ""),
                    timeout)


def _approve(cid: str, secret: str, app_id: str, timeout: float) -> dict:
    """Open Slack's Allow page, catch the redirect, keep the user token."""
    redirect = f"http://localhost:{SETUP_PORT}/slack/callback"
    _Catch.code, _Catch.state = None, secrets.token_urlsafe(16)
    try:
        srv = HTTPServer(("127.0.0.1", SETUP_PORT), _Catch)
    except OSError as e:
        return {"error": f"port {SETUP_PORT} is busy, so I can't catch the "
                         f"approval ({e})"}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        webbrowser.open("https://slack.com/oauth/v2/authorize?" +
                        urllib.parse.urlencode({
                            "client_id": cid,
                            "user_scope": ",".join(SCOPE_LIST),
                            "redirect_uri": redirect,
                            "state": _Catch.state}))
        t0 = time.time()
        while time.time() - t0 < timeout and not _Catch.code:
            time.sleep(0.4)
    finally:
        srv.shutdown()
    if not _Catch.code:
        return {"error": "nobody pressed Allow, so nothing changed"}

    got = _form("oauth.v2.access", {"client_id": cid, "client_secret": secret,
                                    "code": _Catch.code,
                                    "redirect_uri": redirect})
    if not got.get("ok"):
        return {"error": "Slack refused the final exchange: "
                         + str(got.get("error") or "unknown")}
    tok = ((got.get("authed_user") or {}).get("access_token") or "")
    if not tok.startswith("xoxp-"):
        return {"error": "Slack returned no user token, only a bot one"}
    save_secret("slack_token", tok)

    sl = REGISTRY.get("slack")
    if sl is not None:
        sl._checked = {}                 # the old verdict is about the old token
        if not sl.ready():
            return {"error": "the new token still can't read: " + sl.last_error()}
        return {"ok": True, "who": sl.whoami(), "app_id": app_id}
    return {"ok": True, "who": "", "app_id": app_id}

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

    _checked = {}

    def ready(self) -> bool:
        """Same rule as Slack: a token that does not work is not a connection."""
        tok = self.token()
        if not tok:
            return False
        hit = self._checked.get(tok)
        if hit and time.time() - hit[1] < 300:
            return hit[0]
        ok = bool(self._call("profile").get("emailAddress"))
        self._checked[tok] = (ok, time.time())
        return ok

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


# ------------------------------------------------------------------- MCP ----
class MCPConnector:
    """Any MCP server, wrapped so it looks like every other connector.

    This is the reason MCP is worth having: one implementation, and every
    server anyone writes becomes usable without new code in Friday."""

    def __init__(self, name: str):
        self.name = name

    def _c(self):
        from . import mcp
        return mcp.client(self.name)

    def ready(self) -> bool:
        """Connected means USABLE, not merely reachable.

        Gmail's server happily completes a handshake and lists its tools with
        no credentials, then returns 'unauthorized' the moment you call one.
        Reporting that as connected would be a lie that only surfaces when you
        actually need it, so a server that has no token counts as not
        connected."""
        from . import mcp
        cfg = mcp.servers().get(self.name) or {}
        if not cfg.get("token"):
            return False
        c = self._c()
        return bool(c and not c.connect())

    def setup_hint(self) -> str:
        return (f"{self.name} isn't connected. Say \"connect {self.name}\" and "
                f"I'll give you the shortest way to do it.")

    def tools(self) -> list:
        c = self._c()
        if not c or c.connect():
            return []
        return c.tools()

    def call(self, tool: str, **args) -> dict:
        c = self._c()
        if not c:
            return {"error": f"{self.name} is not configured"}
        err = c.connect()
        if err:
            return {"error": "not connected"}
        return c.call(tool, args)


def mcp_servers() -> dict:
    """Configured MCP servers as connector objects, by name."""
    try:
        from . import mcp
        return {n: MCPConnector(n) for n in mcp.servers()}
    except Exception:
        return {}


# -------------------------------------------------------------- registry ----
REGISTRY = {c.name: c() for c in (GitHub, Slack, Gmail, Jira)}


def _works(c) -> bool:
    try:
        return bool(c.ready())
    except Exception:
        return False


def all_connectors() -> dict:
    """Built-in connectors plus every MCP server you have added.

    On a name collision, the one that can actually ANSWER wins. Letting MCP win
    unconditionally meant an added-but-never-authorized Slack MCP server
    shadowed a working token: "connect slack" reported success (it verified the
    token directly) while every question about Slack reported "not connected",
    with no way to see why the two disagreed. An MCP server still wins when both
    work, since it is the better path."""
    out = dict(REGISTRY)
    for name, srv in mcp_servers().items():
        if name not in out or _works(srv):
            out[name] = srv
    return out


def status() -> dict:
    """Which connections are live, and how to fix the ones that are not."""
    out = {}
    for name, c in all_connectors().items():
        try:
            ok = c.ready()
        except Exception:
            ok = False
        out[name] = {"ready": ok, "hint": "" if ok else c.setup_hint()}
    return out


def get(name: str):
    return all_connectors().get((name or "").lower())


def when(ts: float) -> str:
    d = max(0, time.time() - (ts or 0))
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"
