"""What Friday does when you say something.

This is the assistant. One thread, and everything arrives in it: what you type
or say, what Friday answers, and the things Friday brings up on its own when an
agent needs you.

The design rule that keeps it honest: understanding what you MEANT is a fast
deterministic pass first, and only genuinely open-ended talk reaches the model.
That is not a performance trick, it is a correctness one. "Open jobhunt" must do
the same thing every single time; a model that is right 95% of the time is the
wrong tool for a command that moves your work around.

Actions are TIERED by how reversible they are and by how sure Friday is, not
confirmed uniformly. Asking "Open jobhunt?" every single time is friction you
hit fifty times a day, and a confirmation you always say yes to stops being a
safety mechanism: you learn to tap through it. So:

  tier 0  do it, say so, offer undo      reversible and unambiguous
  tier 1  ask first, inline              Friday had to guess, or it writes
                                         into another agent's session
  tier 2  read it back, explicit yes     irreversible: push, merge, delete

The deciding question for tier 0 vs 1 is not WHICH action it is, it is whether
Friday had to guess. If you named a session and exactly one matches, that was
your confirmation.
"""

import re
import time

from . import actions, connectors, engine, memory

# What kind of thing the user just said.
ASK_FLEET = "fleet"        # "what's running", "who needs me"
OPEN = "open"              # "open jobhunt", "switch to api"
TELL = "tell"              # "tell api to use redis"  (routed to an agent)
CONFIRM = "confirm"        # "yes", "do it"
CANCEL = "cancel"          # "no", "cancel"
QUIET = "quiet"            # "quiet", "stop talking"
RESUME = "resume"          # "resume", "you can talk again"
NEEDS = "needs"            # "what does api need?"
FIND = "find"              # "find the session where I set up redis"
RECENT = "recent"          # "what was I working on yesterday"
OTHERS = "others"          # "are there other users' sessions?"
READ_CHANNEL = "readchan"  # "go to my #eng slack and read the chat"
GITHUB = "github"          # "anything on github", "search github for X"
ISSUES = "issues"          # "what are my open issues"
BROKEN = "broken"          # "what's broken?" / "is anything failing?"
ACTIVITY = "activity"      # "what have I been doing lately"
MAIL = "mail"              # "any new email"
JIRA = "jira"              # "my jira tickets"
DID_WE = "didwe"           # "did we ever talk about this?"
SLACK = "slack"            # "search slack for X"
CONNECT = "connect"        # "connect slack <token>"
OPEN_FOUND = "openfound"   # "open that one" after a search
NEW_SESSION = "newsession" # "start a new session on this"
CHAT = "chat"              # anything else: a real conversation

_FLEET_RE = re.compile(
    r"\b(what('?s| is)? (running|going on|happening)|status|who needs me|"
    r"which (agents?|sessions?) need|what are you (running|watching)|"
    r"how are (things|we)|(show|list) (me )?(my )?(agents?|sessions?))\b", re.I)
_OPEN_RE = re.compile(
    r"\b(open|switch to|go to|jump to|resume|show me)\s+(?:the\s+)?"
    r"(?:session\s+)?([\w.\-]+)", re.I)
# "open it", "open that" name nothing. Treating a pronoun as a session name is
# how "can you open it through Claude?" became "I couldn't bring Reply with
# exactly ALPHA to the front": it matched a session labelled by its own first
# prompt. A pronoun means ask, never guess.
_PRONOUNS = {"it", "that", "this", "them", "one", "there", "here", "him", "her"}
# Words that are never a session name, so a greedy pattern cannot mistake an
# article for the thing you meant.
_FILLER = {"the", "a", "an", "my", "your", "of", "to", "and", "session"}
# People do not phrase instructions as clean commands. "Can you go to the
# voicebridge session and tell him that the design looks good" must work, not
# fall through to chat and come back as a rephrasing of itself.
_TELL_RE = re.compile(
    r"(?:^|\b)(?:go to|open)?\s*(?:the\s+)?(?:session\s+(?:of|called)\s+)?"
    r"(?:tell|ask|reply to|answer|send(?:\s+a\s+message)?\s+to|message)\s+"
    r"(?:the\s+)?(?:session\s+)?([\w.\-]+)\s+"
    r"(?:session\s+)?(?:that\s+|to\s+|about\s+)?(.+)$", re.I)
_FIND_RE = re.compile(
    r"\b(?:find|search(?:\s+for)?|look for|which session|what session|"
    r"where did i|where was i|the (?:session|chat|conversation) (?:where|about|"
    r"in which))\b\s*(.*)$", re.I)
_RECENT_RE = re.compile(
    r"\b(?:what (?:was|were) i (?:working on|doing)|recent sessions?|"
    r"my recent work|what have i been (?:working on|doing))\b", re.I)
_OTHERS_RE = re.compile(
    r"\b(?:other users?|another user|someone else|other accounts?|"
    r"anyone else|nikhil|other people)\b", re.I)
_GITHUB_RE = re.compile(
    r"\b(?:github|gh|pull requests?|prs?\b|my notifications?)\b\s*(.*)$", re.I)
_SLACK_RE = re.compile(r"\bslack\b\s*(.*)$", re.I)
# "go to my neither group in slack and read the chat"
_READCHAN_RE = re.compile(
    r"\b(?:go to|open|check|read)\b[^.]*?\b(?:my\s+)?#?([\w.\-]+)\s*"
    r"(?:group|channel)\b[^.]*?\b(?:slack|read|chat|messages?)\b|"
    r"\bslack\b[^.]*?\b#?([\w.\-]+)\s*(?:group|channel)\b", re.I)
_ISSUES_RE = re.compile(r"\b(?:open\s+)?issues?\b", re.I)
_BROKEN_RE = re.compile(
    r"\b(?:what(?:'s| is)? (?:broken|failing|red)|anything (?:broken|failing)|"
    r"failing (?:tests?|builds?|ci|workflows?)|ci status|builds? failing)\b", re.I)
_ACTIVITY_RE = re.compile(
    r"\b(?:what have i been (?:doing|up to)|my (?:recent )?activity|"
    r"what did i (?:do|push|ship))\b", re.I)
_MAIL_RE = re.compile(r"\b(?:e-?mail|gmail|inbox|mails?)\b\s*(.*)$", re.I)
_JIRA_RE = re.compile(r"\bjira\b|\btickets?\b", re.I)
# "did we ever talk about this?" right after reading something
_DIDWE_RE = re.compile(
    r"\b(?:did (?:we|i) (?:ever )?(?:talk|discuss|work)|have (?:we|i) "
    r"(?:ever )?(?:talked|discussed|worked)|look into claude|check claude|"
    r"any (?:past )?session about)\b", re.I)
_CONNECT_RE = re.compile(r"^\s*connect\s+(\w+)(?:\s+(\S+))?", re.I)
_CONNS_RE = re.compile(
    r"\b(?:what(?:'s| is)? connected|connections?|integrations?|"
    r"what (?:tools|apps) (?:do you have|are connected))\b", re.I)
# Anchored to the END on purpose: "open that one" is this intent, but "go to
# the session of voicebridge and tell him…" is an instruction to that session,
# and an unanchored pattern swallowed it.
_OPENFOUND_RE = re.compile(
    r"\b(?:open|resume|bring up|go to)\s+(?:that|it|the)"
    r"(?:\s+(?:one|session|chat|conversation))?\s*[.!?]?$", re.I)
_NEWSESSION_RE = re.compile(
    r"\b(?:start|open|create|spin up|make)\s+(?:a\s+)?new\s+"
    r"(?:claude\s+)?(?:session|chat)\b\s*(?:on|about|for|to)?\s*(.*)$", re.I)
_NEEDS_RE = re.compile(
    r"\b(?:what (?:does|is)|why (?:does|is))\s+([\w.\-]+)\s+"
    r"(?:need|want|waiting|asking|blocked|stuck)", re.I)
_TELL_BACK_RE = re.compile(
    # the (?!…) stops "the session of voicebridge" from capturing "the": that
    # alternative sits earlier in the sentence and would otherwise win
    r"\b(?:session\s+(?:of|called|named)\s+([\w.\-]+)"
    r"|(?!the\b|a\b|an\b|my\b|your\b|this\b|that\b)([\w.\-]+)\s+session)\b"
    r"[^.]*?\b(?:tell|ask|say to|message)\b\s*(.+)$", re.I)
_NOISE_RE = re.compile(r"[\[\(][^\]\)]*[\]\)]|\W*", re.I)
_YES_RE = re.compile(r"^\s*(yes|yeah|yep|sure|do it|go ahead|please|ok(ay)?)\b", re.I)
_NO_RE = re.compile(r"^\s*(no|nope|cancel|stop|don'?t|never ?mind)\b", re.I)
_QUIET_RE = re.compile(r"^\s*(quiet|shush|be quiet|stop talking|silence)\b", re.I)
_RESUME_RE = re.compile(r"^\s*(resume|unmute|you can talk|start talking)\b", re.I)


def _strip_verbs(s: str) -> str:
    """'for the deploy thread' -> 'deploy thread'. What is left is the search."""
    s = re.sub(r"^\s*(?:for|about|on|in|any|anything|my|the)\b\s*", "", (s or ""),
               flags=re.I).strip(" ?.")
    return s


def classify(text: str) -> tuple:
    """(intent, payload). Deterministic and ordered: the most specific command
    wins, and only what is left over counts as conversation."""
    t = (text or "").strip()
    if not t:
        return CHAT, {}
    # Specific multi-word intents go FIRST. "resume that session" is not the
    # voice command "resume", and "open a new session" is not "open <name>":
    # the generic patterns would otherwise swallow both.
    if _DIDWE_RE.search(t):
        return DID_WE, {}
    m = _READCHAN_RE.search(t)
    if m:
        return READ_CHANNEL, {"channel": (m.group(1) or m.group(2) or "").strip()}
    if _JIRA_RE.search(t):
        return JIRA, {}
    m = _MAIL_RE.search(t)
    if m:
        return MAIL, {"query": _strip_verbs(m.group(1))}
    if _BROKEN_RE.search(t):
        return BROKEN, {}
    if _ACTIVITY_RE.search(t):
        return ACTIVITY, {}
    if _ISSUES_RE.search(t) and "github" not in t.lower():
        return ISSUES, {}
    m = _NEWSESSION_RE.search(t)
    if m:
        return NEW_SESSION, {"about": m.group(1).strip()}
    if _OPENFOUND_RE.search(t):
        return OPEN_FOUND, {}
    if _QUIET_RE.match(t):
        return QUIET, {}
    if _RESUME_RE.match(t):
        return RESUME, {}
    # "go to the voicebridge session and tell him that X": the name lands
    # BEFORE the verb, which is how people actually speak.
    m2 = _TELL_BACK_RE.search(t)
    if m2:
        # the pattern has two shapes ("<name> session …" and "session of
        # <name> …"), so take whichever pair matched
        name = m2.group(1) or m2.group(2) or ""
        msg = (m2.group(3) or "").strip()
        msg = re.sub(r"^(?:him|her|it|them)\s+(?:that\s+)?", "", msg, flags=re.I)
        if name.lower() not in _PRONOUNS | _FILLER and msg:
            return TELL, {"name": name, "message": msg}
    m = _TELL_RE.search(t)
    if m:
        name, msg = m.group(1), m.group(2).strip()
        # "tell voicebridge him that X" / "tell it that X": drop the pronoun
        msg = re.sub(r"^(?:him|her|it|them)\s+(?:that\s+)?", "", msg, flags=re.I)
        if name.lower() not in _PRONOUNS and msg:
            return TELL, {"name": name, "message": msg}
    m = _OPEN_RE.search(t)
    if m and len(t.split()) <= 6:      # a command, not a sentence about opening
        name = m.group(2)
        if name.lower() in _PRONOUNS:
            return OPEN, {"name": ""}   # "open it": we must ask which
        return OPEN, {"name": name}
    if _CONNS_RE.search(t):
        return CONNECT, {"which": "", "token": ""}
    m = _CONNECT_RE.match(t)
    if m:
        return CONNECT, {"which": m.group(1), "token": m.group(2) or ""}
    if _SLACK_RE.search(t):
        return SLACK, {"query": _strip_verbs(_SLACK_RE.search(t).group(1) or t)}
    m = _GITHUB_RE.search(t)
    if m:
        return GITHUB, {"query": _strip_verbs(m.group(1))}
    if _OTHERS_RE.search(t):
        return OTHERS, {}
    if _RECENT_RE.search(t):
        return RECENT, {}
    m = _FIND_RE.search(t)
    if m and len(m.group(1).strip()) >= 3:
        return FIND, {"query": m.group(1).strip()}
    m = _NEEDS_RE.search(t)
    if m:
        return NEEDS, {"name": m.group(1)}
    if _FLEET_RE.search(t):
        return ASK_FLEET, {}
    if _YES_RE.match(t):
        return CONFIRM, {}
    if _NO_RE.match(t):
        return CANCEL, {}
    return CHAT, {}


class Friday:
    """One conversation, with memory of what it just offered to do."""

    def __init__(self):
        self.pending = None        # an action awaiting your yes
        self._last_found = []      # sessions a search turned up
        self._last_slack = []      # slack messages just read out
        self.history = []          # [{role, text, ts, kind}]
        self.focus = (engine.routing.new_focus()
                      if engine.AVAILABLE else {"mentioned": [], "ts": 0})

    # ---- the thread -------------------------------------------------------
    def add(self, role: str, text: str, kind: str = "") -> dict:
        msg = {"role": role, "text": text, "ts": time.time(), "kind": kind}
        self.history.append(msg)
        self.history = self.history[-400:]
        return msg

    # ---- the main entry point --------------------------------------------
    def handle(self, text: str) -> dict:
        """Take what the user said, return {reply, action, needs_confirm}."""
        # Whisper labels non-speech as [SOUND], [BLANK_AUDIO], (music) and so
        # on. Treating a door closing as a question produced "I don't know what
        # that sound is. Can you clarify?", which is a machine talking to noise.
        if _NOISE_RE.fullmatch((text or "").strip()):
            return {"reply": "", "needs_confirm": False, "action": {}}
        self.add("user", text)
        intent, payload = classify(text)

        # A pending offer takes precedence: "yes" means yes to THAT.
        if self.pending and intent in (CONFIRM, CANCEL):
            act = self.pending
            self.pending = None
            if intent == CANCEL:
                return self._say("Okay, left it alone.")
            return self._perform(act)

        if intent == QUIET:
            if engine.AVAILABLE:
                engine.attention.hush()
            return self._say("Quiet. I won't interrupt you.")
        if intent == RESUME:
            if engine.AVAILABLE:
                engine.attention.unhush()
            return self._say("Listening again.")
        if intent == ASK_FLEET:
            return self._say(self.fleet_summary())
        if intent == NEEDS:
            return self._what_needs(payload["name"])
        if intent == FIND:
            return self._find_past(payload["query"])
        if intent == RECENT:
            return self._recent_work()
        if intent == OTHERS:
            return self._other_users()
        if intent == OPEN_FOUND:
            return self._open_found()
        if intent == NEW_SESSION:
            return self._new_session(payload.get("about", ""))
        if intent == CONNECT:
            return self._connect(payload["which"], payload["token"])
        if intent == READ_CHANNEL:
            return self._read_channel(payload["channel"])
        if intent == DID_WE:
            return self._did_we_discuss()
        if intent == ISSUES:
            return self._issues()
        if intent == BROKEN:
            return self._broken()
        if intent == ACTIVITY:
            return self._activity()
        if intent == MAIL:
            return self._mail(payload.get("query", ""))
        if intent == JIRA:
            return self._jira()
        if intent == GITHUB:
            return self._github(payload.get("query", ""))
        if intent == SLACK:
            return self._slack(payload.get("query", ""))
        if intent == OPEN:
            return self._propose_open(payload["name"])
        if intent == TELL:
            return self._propose_tell(payload["name"], payload["message"])
        if intent in (CONFIRM, CANCEL):
            return self._say("Nothing was waiting on you." if intent == CONFIRM
                             else "Okay.")

        # An agent asked you something a moment ago and this reads like the
        # answer: send it there rather than treating it as small talk. This is
        # the whole point of a supervisor, you answer once, in the thread, and
        # it reaches the right session.
        routed = self._maybe_route_answer(text)
        if routed is not None:
            return routed
        return self._chat(text)

    def _maybe_route_answer(self, text: str):
        """Return a reply if this belongs to a waiting agent, else None."""
        if not engine.AVAILABLE:
            return None
        try:
            r = engine.routing.route(text, self.focus, active_sid="",
                                     find=self._find)
        except Exception:
            return None
        if r.get("ask"):
            # Two agents are waiting: ask which, never guess. A wrong answer
            # delivered to the wrong agent is a wrong instruction it will act on.
            return self._say(r["ask"])
        sid = r.get("sid")
        if not sid or "answering" not in (r.get("why") or ""):
            return None
        label = r.get("label") or "it"
        ok = actions.send_to_session(sid, text)
        return self._say(f"Told {label}." if ok
                         else f"I couldn't reach {label}.",
                         action={"kind": "tell", "sid": sid})

    # ---- what it knows ----------------------------------------------------
    def fleet_summary(self) -> str:
        """Plain-English answer to 'what is going on', the question this whole
        product exists to answer."""
        if not engine.AVAILABLE:
            return "I can't see your sessions right now."
        snap = engine.fleet.snapshot()
        if not snap:
            return "Nothing is running."
        waiting = [r for r in snap.values() if r.get("question") or r.get("permission")]
        working = [r for r in snap.values() if r.get("status") == "working"
                   and r not in waiting]
        idle = [r for r in snap.values() if r.get("status") != "working"
                and r not in waiting]
        bits = []
        if waiting:
            names = _join([r["label"] for r in waiting])
            bits.append(f"{names} {_is(len(waiting))} waiting on you.")
        if working:
            bits.append(f"{_join([r['label'] for r in working])} "
                        f"{_is(len(working))} still working.")
        if idle:
            bits.append(f"{_join([r['label'] for r in idle])} {_is(len(idle))} done.")
        return " ".join(bits) or "Nothing is running."

    def _open_found(self) -> dict:
        """'open that one' after a search. If the session is running we raise
        its window; if it is closed we RESUME it, which is the whole point of
        remembering it in the first place."""
        if not self._last_found:
            return self._say("I haven't found a session for you to open yet.")
        hit = self._last_found[0]
        sid = hit["sid"]
        live = {}
        try:
            live = {r["sid"]: r for r in engine.fleet.snapshot().values()}
        except Exception:
            pass
        if sid in live:
            ok = actions.focus_session(sid)
            return self._say("Opened it." if ok else
                             "I couldn't bring that window to the front.")
        ok = actions.resume_session(sid, cwd=_cwd_for(hit.get("path", "")))
        return self._say("Resumed it in a new window." if ok else
                         "I couldn't resume that session.")

    def _new_session(self, about: str) -> dict:
        """Start fresh work, handing the new session its purpose so you do not
        have to retype what you just told me."""
        opening = about.strip()
        if not opening and self._last_slack:
            # straight from what was just read out of Slack
            opening = ("Context from a Slack thread:\n"
                       + "\n".join(f"- {m['who']}: {m['text']}"
                                    for m in self._last_slack[:6]))
        self.pending = {"kind": "new", "about": opening}
        preview = (opening[:90] + "…") if len(opening) > 90 else opening
        return self._say(
            (f'Start a new session on "{preview}"?' if preview
             else "Start a new empty session?"), needs_confirm=True)

    # ---- connectors: Friday's own eyes on your tools ---------------------
    def _connect(self, which: str, token: str) -> dict:
        # no argument: report what is connected and what is not
        if not which:
            rows = connectors.status()
            live = [n for n, v in rows.items() if v["ready"]]
            dead = [n for n, v in rows.items() if not v["ready"]]
            bits = []
            if live:
                bits.append("Connected: " + ", ".join(sorted(live)) + ".")
            if dead:
                bits.append("Not connected: " + ", ".join(sorted(dead))
                            + '. Say "connect <name>" and I\'ll walk you through it.')
            return self._say(" ".join(bits) or "Nothing is connected yet.")
        # no token: try the browser flow (MCP), which is the one-time approval
        if not token:
            from . import mcp as _mcp
            cfg = _mcp.servers().get(which)
            # Only open a browser if the flow can actually succeed. Otherwise
            # fall through to the connector's own instructions, which for Slack
            # is a pre-filled app link rather than a scope checklist.
            if cfg and _mcp.can_authorize(cfg["url"]):
                def _flow():
                    r = _mcp.authorize(which, cfg["url"])
                    self.announce(f"{which} connected." if r.get("ok") else
                                  f"{which} didn't connect: {r.get('error')}")
                import threading
                threading.Thread(target=_flow, daemon=True).start()
                return self._say(f"Opening your browser to approve {which}. "
                                 "I'll tell you when it's done.")
            # The MCP wrapper's hint is generic. If we have a hand-written
            # connector for the same service, ITS instructions are the useful
            # ones (Slack's is a pre-filled app link, not a scope checklist).
            builtin = connectors.REGISTRY.get(which)
            c0 = builtin or connectors.get(which)
            if c0:
                return self._say(c0.setup_hint())
            return self._say(f"I don't know a connector called {which}.")
        c = connectors.get(which)
        if not c:
            return self._say(f"I don't have a {which} connector.")
        if not connectors.save_secret(f"{which}_token", token):
            return self._say(f"I couldn't save the {which} token.")
        ok = False
        try:
            ok = c.ready()
        except Exception:
            ok = False
        return self._say(f"{which} connected." if ok else
                         f"Saved it, but {which} still isn't answering. "
                         f"Check the token's scopes.")

    def _github(self, query: str) -> dict:
        gh = connectors.get("github")
        if not gh.ready():
            return self._say("GitHub isn't connected: " + gh.setup_hint())
        if query:
            rows = gh.search(query, limit=4)
            if not rows:
                return self._say(f"Nothing on GitHub for {query}.")
            lines = [f"{r.get('repository', {}).get('nameWithOwner', '')}: "
                     f"{r.get('title', '')[:80]} ({r.get('state', '')})"
                     for r in rows]
            return self._say("On GitHub:\n- " + "\n- ".join(lines))
        # no query: what actually wants your attention
        notes, prs = gh.notifications(6), gh.my_prs(4)
        if not notes and not prs:
            return self._say("Nothing waiting on you on GitHub.")
        bits = []
        if prs:
            bits.append("Open pull requests:\n- " + "\n- ".join(
                f"{p.get('repository', {}).get('nameWithOwner', '')}: {p.get('title', '')[:70]}"
                for p in prs))
        if notes:
            bits.append("Notifications:\n- " + "\n- ".join(
                f"{n.get('repo', '')}: {(n.get('title') or '')[:70]} [{n.get('reason', '')}]"
                for n in notes))
        return self._say("\n\n".join(bits))

    def _read_channel(self, name: str) -> dict:
        """Read an actual channel and tell you what is being asked.

        This is the front of the chain: read the thread, then 'did we ever talk
        about this?' searches your sessions using what was just read, so you
        never have to retype the subject."""
        sl = connectors.get("slack")
        if not sl.ready():
            return self._say("Slack isn't connected yet. " + sl.setup_hint())
        ch = sl.find_channel(name)
        if not ch:
            return self._say(f"I can't find a Slack channel called {name}.")
        rows = sl.read_channel(ch["id"], limit=15)
        if not rows:
            return self._say(f"#{ch['name']} is empty, or I can't read it.")
        self._last_slack = rows
        convo = "\n".join(f"{r['who']}: {r['text']}" for r in rows[-12:])
        summary = self._summarise_thread(convo)
        return self._say(f"In #{ch['name']}:\n{summary}")

    def _summarise_thread(self, convo: str) -> str:
        """What is actually being ASKED, in a sentence or two."""
        if not (engine.AVAILABLE and engine.brain.up()):
            return convo[:600]
        out = engine.brain._chat(
            [{"role": "system", "content":
              "Summarise this chat for someone who has not read it. Say who is "
              "asking what, and what they need. Two or three short sentences. "
              "Only use what is in the messages."},
             {"role": "user", "content": convo[:4000]}],
            timeout=engine.brain.TIMEOUT_SLOW, max_tokens=180)
        return engine.brain._clean(out) if out else convo[:600]

    def _did_we_discuss(self) -> dict:
        """Search your past sessions using whatever we were just talking about,
        so the chain flows without you restating the subject."""
        seed = ""
        if self._last_slack:
            seed = " ".join(r["text"] for r in self._last_slack[-6:])
        if not seed:
            for m in reversed(self.history[:-1]):
                if m["role"] == "friday" and len(m["text"]) > 40:
                    seed = m["text"]
                    break
        if not seed:
            return self._say("About what? Point me at something first.")
        terms = self._key_terms(seed)
        hits = memory.search(terms, limit=3)
        if not hits:
            self._last_found = []
            return self._say(f"I searched your sessions for {terms} and found "
                             "nothing. Want me to start a new one on it?")
        self._last_found = hits
        live = set()
        try:
            live = set(engine.fleet.snapshot())
        except Exception:
            pass
        h = hits[0]
        where = " (running now)" if h["sid"] in live else ""
        return self._say(
            f"Yes. {memory.ago(h['when'])}{where}: {(h.get('about') or '')[:110]}"
            "\n\nSay \"open that one\" and I'll bring it up.")

    def _key_terms(self, text: str) -> str:
        """The few words worth searching for, so a whole thread does not become
        a query full of 'the' and 'please'."""
        if engine.AVAILABLE and engine.brain.up():
            out = engine.brain._chat(
                [{"role": "system", "content":
                  "Pick the 2-5 most distinctive search keywords from this "
                  "text: proper nouns, technical terms, product names. "
                  "Lowercase, space separated, nothing else."},
                 {"role": "user", "content": text[:1500]}],
                timeout=6.0, max_tokens=24)
            out = " ".join((out or "").split())[:80]
            if out:
                return out
        return " ".join(text.split()[:8])

    def _issues(self) -> dict:
        gh = connectors.get("github")
        if not gh.ready():
            return self._say("GitHub isn't connected: " + gh.setup_hint())
        rows = gh.my_issues(8)
        if not rows:
            return self._say("No open issues involving you.")
        lines = [f"{r.get('repository', {}).get('nameWithOwner', '')}: "
                 f"{r.get('title', '')[:80]}" for r in rows]
        return self._say(f"{len(rows)} open issue"
                         f"{'s' if len(rows) != 1 else ''}:\n- "
                         + "\n- ".join(lines))

    def _broken(self) -> dict:
        """What is actually broken right now, deduplicated.

        A notification list says 49 things happened; this says four things are
        wrong. Repeats are counted, not listed, because ten failures of the
        same nightly job is one problem you have not looked at."""
        gh = connectors.get("github")
        if not gh or not gh.ready():
            return self._say("GitHub isn't connected, so I can't see your builds.")
        rows = gh.failing(6)
        if not rows:
            return self._say("Nothing's failing on GitHub.")
        lines = []
        for r in rows:
            times = f" ({r['count']} times)" if r.get("count", 1) > 1 else ""
            lines.append(f"{r['repo']}: {r['workflow']}{times}")
        n = len(lines)
        return self._say(f"{n} thing{'s' if n != 1 else ''} failing:\n- "
                         + "\n- ".join(lines))

    def _activity(self) -> dict:
        gh = connectors.get("github")
        if not gh or not gh.ready():
            return self._say("GitHub isn't connected.")
        rows = gh.activity(6)
        if not rows:
            return self._say("No recent GitHub activity.")
        pretty = {"PushEvent": "pushed to", "IssuesEvent": "worked on issues in",
                  "PullRequestEvent": "opened a PR in", "CreateEvent": "created",
                  "DeleteEvent": "deleted in", "WatchEvent": "starred"}
        lines = [f"{pretty.get(r.get('type'), r.get('type', ''))} {r.get('repo', '')}"
                 for r in rows]
        return self._say("Lately you:\n- " + "\n- ".join(lines))

    def _mail(self, query: str) -> dict:
        gm = connectors.get("gmail")
        if not gm.ready():
            return self._say("Gmail isn't connected yet. " + gm.setup_hint())
        rows = gm.search(query, limit=5)
        if not rows:
            return self._say("Nothing matching in your mail.")
        lines = [f"{r['from'][:40]}: {r['subject'][:90]}" for r in rows]
        return self._say("Mail:\n- " + "\n- ".join(lines))

    def _jira(self) -> dict:
        ji = connectors.get("jira")
        if not ji.ready():
            return self._say("Jira isn't connected yet. " + ji.setup_hint())
        rows = ji.my_issues(8)
        if rows and rows[0].get("error"):
            return self._say("Jira answered with an error: " + rows[0]["error"])
        if not rows:
            return self._say("No open Jira tickets assigned to you.")
        lines = [f"{r['key']} [{r['status']}]: {r['summary'][:80]}" for r in rows]
        return self._say("Jira:\n- " + "\n- ".join(lines))

    def _slack(self, query: str) -> dict:
        sl = connectors.get("slack")
        if not sl.ready():
            return self._say("Slack isn't connected yet. To fix that: "
                             + sl.setup_hint())
        if not query:
            return self._say("What should I look for in Slack?")
        rows = sl.search(query, limit=4)
        if not rows:
            return self._say(f"Nothing in Slack about {query}.")
        lines = [f"#{r['channel']} · {r['who']} · {connectors.when(r['when'])}: "
                 f"{r['text'][:120]}" for r in rows]
        self._last_slack = rows
        return self._say("In Slack:\n- " + "\n- ".join(lines))

    def _find_past(self, query: str) -> dict:
        """Search everything you have ever done, not just what is running."""
        hits = memory.search(query, limit=4)
        if not hits:
            return self._say(f"I couldn't find anything about {query}.")
        live = {}
        try:
            live = {r["sid"]: r for r in engine.fleet.snapshot().values()}
        except Exception:
            pass
        lines = []
        for h in hits:
            mark = " (running now)" if h["sid"] in live else ""
            about = (h.get("about") or h.get("snippet") or "").strip()
            lines.append(f"{memory.ago(h['when'])}{mark}: {about[:100]}")
        self._last_found = hits
        head = ("Found one:" if len(lines) == 1 else f"Found {len(lines)}:")
        tail = ("\n\nSay \"open that one\" to bring it up."
                if hits[0]["sid"] in live else
                "\n\nThe top one isn't running, so I can't open it, only tell you about it.")
        return self._say(head + "\n- " + "\n- ".join(lines) + tail)

    def _recent_work(self) -> dict:
        rows = memory.recent(limit=5)
        if not rows:
            return self._say("I can't see any recent sessions.")
        lines = [f"{memory.ago(r['when'])}: {(r['about'] or 'no description')[:90]}"
                 for r in rows]
        return self._say("Recently:\n- " + "\n- ".join(lines))

    def _other_users(self) -> dict:
        """Honest about what is on the machine, and about the wall."""
        try:
            others = engine.fleet.other_users()
        except Exception:
            others = {}
        if not others:
            return self._say("No one else has Claude running on this Mac.")
        bits = [f"{n} session{'s' if n != 1 else ''} under {u}"
                for u, n in others.items()]
        return self._say(_join(bits) + ". I can see they're running, but not "
                         "what they're doing: another account's work isn't "
                         "readable from here, and shouldn't be.")

    def _what_needs(self, name: str) -> dict:
        """Report exactly what one agent is waiting on, and remember that it is
        waiting, so your very next message can just be the answer."""
        hit, _ = self._find_how(name)
        if not hit:
            return self._say(f"I can't find a session called {name}.")
        label = hit.get("label", name)
        q = (hit.get("question") or hit.get("permission") or "").strip()
        if not q:
            state = "still working" if hit.get("status") == "working" else "done"
            return self._say(f"{label} doesn't need anything, it's {state}.")
        # Mark it as waiting so a bare reply routes straight there.
        if engine.AVAILABLE:
            try:
                self.focus = engine.routing.note_spoken(
                    self.focus, [{"sid": hit.get("sid", ""), "label": label,
                                  "kind": "blocked"}])
            except Exception:
                pass
        return self._say(f"{label} is asking: {q}")

    # ---- actions, always proposed first -----------------------------------
    def _propose_open(self, name: str) -> dict:
        """TIER 0. Bringing a window to the front is instantly reversible (you
        just look away), so asking permission is pure friction."""
        if not name:
            names = self._session_names()
            return self._say("Which one? " + (", ".join(names) if names
                                              else "nothing is running."))
        hit, _how = self._find_how(name)
        if not hit:
            return self._say(f"I can't find a session called {name}.")
        return self._perform({"kind": "open", "sid": hit.get("sid", ""),
                              "label": hit.get("label", name)})

    def _propose_tell(self, name: str, message: str) -> dict:
        """TIER 0 when you named the session exactly (that was your
        confirmation), TIER 1 when Friday had to guess which one you meant."""
        hit, how = self._find_how(name)
        if not hit:
            return self._say(f"I can't find a session called {name}.")
        act = {"kind": "tell", "sid": hit.get("sid", ""),
               "label": hit.get("label", name), "message": message}
        if how == "exact":
            return self._perform(act)
        self.pending = act
        return self._say(f'Did you mean {hit.get("label", name)}? '
                         f'I\'ll send "{message}".', needs_confirm=True)

    def _perform(self, act: dict) -> dict:
        """Do the thing that was just confirmed. Failures are reported plainly,
        never swallowed: a silent no-op here is the worst possible outcome."""
        if not engine.AVAILABLE:
            return self._say("I can't reach your sessions right now.")
        label = act.get("label", "it")
        try:
            if act["kind"] == "open":
                ok = actions.focus_session(act["sid"])
                return self._say(f"Opened {label}." if ok else
                                 f"I couldn't bring {label} to the front. It may "
                                 f"not be running in a terminal I can reach.",
                                 action={"kind": "open", "sid": act["sid"]})
            if act["kind"] == "new":
                ok = actions.new_session(act.get("about", ""))
                return self._say("Started it in a new window." if ok else
                                 "I couldn't open a new window.")
            if act["kind"] == "tell":
                ok = actions.send_to_session(act["sid"], act["message"])
                return self._say(f"Sent it to {label}." if ok else
                                 f"I couldn't reach {label}.",
                                 action={"kind": "tell", "sid": act["sid"],
                                         "undo": bool(ok)})
        except Exception as e:
            return self._say(f"That failed: {e}")
        return self._say("I'm not sure what to do with that.")

    # ---- open-ended conversation -----------------------------------------
    # Exactly what Friday can do today. The model is told this verbatim, because
    # the alternative is what actually happened in testing: asked to open a past
    # session it replied "I don't have access to your past sessions", which is
    # false, and asked about a session id it invented a confident paragraph.
    # A small model with no tools will fill any gap with plausible nonsense, so
    # the gap has to be closed explicitly.
    CAN_DO = [
        "tell you which coding sessions are running and what each is doing",
        "tell you what a specific session is waiting on",
        "bring a session's window to the front (say: open <name>)",
        "send an instruction to a session (say: tell <name> to <something>)",
        "take your answer to a session that asked you a question",
        "go quiet, or start speaking again",
    ]
    CAN_DO_MORE = [
        "search everything you have worked on before, not just what is running",
        "tell you what you were working on recently",
        "say whether other people on this Mac have sessions running",
        "read your GitHub: notifications, open pull requests, search issues",
        "search your Slack, once you connect it",
    ]
    CANNOT_YET = [
        "search Jira or email",
        "post, comment, merge or change anything (everything is read-only)",
        "start a brand new session, or write code itself",
        "see INSIDE another person's sessions on this Mac",
    ]

    def _chat(self, text: str) -> dict:
        """Real conversation, bounded by what Friday can actually do.

        The model never answers about the machine from its own head: the live
        facts are handed to it, and anything outside its abilities must be an
        honest 'I can't do that yet' rather than an invention."""
        if not engine.AVAILABLE or not engine.brain.model_ready():
            return self._say("I'm here, but my brain isn't loaded yet.")
        sessions = self._session_facts() or "none"
        sys_prompt = (
            "You are Friday, a calm assistant that coordinates a developer's "
            "coding agents. You do not write code.\n\n"
            "ONLY these facts are true; never invent others.\n"
            f"Sessions running right now:\n{sessions}\n"
            "That list is the complete truth about what each session is and what "
            "it is about. Never invent a description for a session; if its "
            "subject is not listed, say you do not know what it is working on.\n\n"
            "You CAN: " + "; ".join(self.CAN_DO + self.CAN_DO_MORE) + ".\n"
            "You CANNOT yet: " + "; ".join(self.CANNOT_YET) + ".\n\n"
            "Rules: if asked for something in the CANNOT list, say plainly that "
            "you cannot do it yet, in one sentence, and do not speculate. If "
            "asked about something you have no fact for, say you do not know. "
            "Never guess what an unfamiliar name or id means. Answer in one or "
            "two short sentences, no lists, no markdown.")
        recent = [{"role": "user" if m["role"] == "user" else "assistant",
                   "content": m["text"]} for m in self.history[-6:]]
        # If the model is not actually up, say THAT. Answering "I didn't catch
        # that" when the truth is "my brain is still loading" sends the user
        # rephrasing a question that was fine, and it took 35 seconds to say it.
        if not engine.brain.up():
            engine.brain.start()
            return self._say("My brain is still loading, give it a few seconds "
                             "and ask again.")
        try:
            out = engine.brain._chat(
                [{"role": "system", "content": sys_prompt}] + recent,
                timeout=engine.brain.TIMEOUT_SLOW, max_tokens=120)
        except Exception:
            out = ""
        if out:
            return self._say(engine.brain._clean(out))
        return self._say("That one took too long to think about. Ask me again?")

    # ---- helpers ----------------------------------------------------------
    _TOPIC_CACHE = {}          # sid -> short subject, computed once per session

    def _subject(self, sid: str, raw: str) -> str:
        """A rambling first prompt turned into a few words a person would use.

        "Hey. I need to learn about RWA tokenization and ERC 7943 and 3643
        Because I would be doing…" becomes "learning RWA tokenisation". Quoting
        the raw prompt is technically honest but reads like a log; this is what
        you would actually call that session."""
        raw = (raw or "").strip()
        if not raw:
            return ""
        hit = self._TOPIC_CACHE.get(sid)
        if hit and hit[0] == raw:
            return hit[1]
        short = ""
        try:
            if engine.AVAILABLE and engine.brain.up():
                short = engine.brain._chat(
                    [{"role": "system", "content":
                      "Turn this first request into a 3-6 word description of "
                      "what the session is about, as a person would say it "
                      "(e.g. 'learning RWA tokenisation', 'building the voice "
                      "app'). Lowercase, no quotes, no punctuation, no preamble."},
                     {"role": "user", "content": raw[:400]}],
                    timeout=6.0, max_tokens=20)
                short = " ".join((short or "").split())[:60].strip(' ."\'')
        except Exception:
            short = ""
        if not short:                       # fall back to a trimmed prompt
            short = " ".join(raw.split()[:8])
        self._TOPIC_CACHE[sid] = (raw, short)
        return short

    def _session_facts(self) -> str:
        """Every session as a line of FACT: its name, its state, and what it is
        actually about (taken from the first thing the human asked it). Without
        this the model had only a generated id like 'krishojha-7f' to reason
        with, and duly invented 'a backend data processing pipeline'."""
        try:
            rows = list(engine.fleet.snapshot().values())
        except Exception:
            return ""
        out = []
        for r in rows:
            state = ("waiting on you" if (r.get("question") or r.get("permission"))
                     else ("working" if r.get("status") == "working" else "idle"))
            topic = self._subject(r.get("sid", ""), r.get("topic"))
            line = f"- {r.get('label')}: {state}"
            if topic:
                line += f'; it is about: "{topic}"'
            else:
                line += "; subject unknown"
            need = r.get("question") or r.get("permission")
            if need:
                line += f'; it is asking: "{need}"'
            out.append(line)
        return "\n".join(out)

    def _session_names(self) -> list:
        try:
            return [r.get("label", "") for r in engine.fleet.snapshot().values()
                    if r.get("label")]
        except Exception:
            return []

    def _find(self, name: str):
        """Match a spoken/typed name to a session.

        Searches the FLEET first, deliberately: those are the names Friday
        shows you, and an assistant that displays "krishojha-7f" then claims it
        cannot find "krishojha-7f" is broken in the most infuriating way. The
        older roster lookup stays as a fallback for sessions the fleet sensor
        cannot see."""
        if not (name and engine.AVAILABLE):
            return None
        hit, _ = self._find_how(name)
        return hit

    def _find_how(self, name: str):
        """(session, how) where how is 'exact' | 'fuzzy' | ''. The caller uses
        `how` to decide whether it may act without asking: an exact name is the
        user's own confirmation, a fuzzy one is Friday guessing."""
        if not (name and engine.AVAILABLE):
            return None, ""
        q = name.strip().lower()
        try:
            rows = list(engine.fleet.snapshot().values())
        except Exception:
            rows = []
        for r in rows:                                  # exact name
            if (r.get("label") or "").lower() == q:
                return r, "exact"
        starts = [r for r in rows if (r.get("label") or "").lower().startswith(q)]
        if len(starts) == 1:                            # unambiguous prefix
            return starts[0], "fuzzy"
        contains = [r for r in rows if q in (r.get("label") or "").lower()]
        if len(contains) == 1:                          # unambiguous substring
            return contains[0], "fuzzy"
        # Deliberately NO fallback to the older roster lookup: it labels
        # sessions by their first prompt, so it happily "finds" a session
        # called "Reply with exactly ALPHA". A miss is better than nonsense.
        return None, ""

    def _say(self, text: str, needs_confirm: bool = False,
             action: dict = None) -> dict:
        self.add("friday", text)
        return {"reply": text, "needs_confirm": needs_confirm,
                "action": action or {}}

    def announce(self, text: str, items: list = None) -> dict:
        """Something Friday brings up on its own (the attention engine decided
        it was worth it). Marked distinctly so the UI can show it as Friday
        starting the conversation, not answering.

        `items` are the underlying events. Remembering which of them are
        WAITING on an answer is what lets you reply "use the redis one" and
        have it reach the agent that asked, instead of being chat."""
        if items and engine.AVAILABLE:
            try:
                self.focus = engine.routing.note_spoken(self.focus, items)
            except Exception:
                pass
        return self.add("friday", text, kind="proactive")


def _cwd_for(transcript_path: str) -> str:
    """Best guess at where a session was working, so a resume lands in the
    right project rather than the home directory."""
    try:
        import json as _j
        with open(transcript_path, "r", errors="ignore") as f:
            for _ in range(30):
                line = f.readline()
                if not line:
                    break
                try:
                    rec = _j.loads(line)
                except Exception:
                    continue
                cwd = rec.get("cwd")
                if cwd:
                    return cwd
    except Exception:
        pass
    return ""


def _join(names: list) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def _is(n: int) -> str:
    return "is" if n == 1 else "are"
