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
from pathlib import Path

from . import actions, connectors, engine, memory, nearest, replies

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
ENGINE = "engine"          # "are you using claude for this?"
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
# "ask" means you want the answer, "tell" means you want it done. Both send a
# prompt; only one is worth waiting on.
_ASKED_RE = re.compile(r"\b(?:ask|reply to|answer)\b", re.I)
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
# Channel names arrive as several words, because speech splits a compound name
# at the wrong place ('moonshot' becomes 'moon shot'), and the read verb can
# come before the name as easily as after it. Requiring a single token followed
# by 'slack' or 'chat' meant "read the moon shot channel" was not recognised
# as a Slack request at all, and fell through to the model, which invented a
# refusal about not having access.
_READCHAN_RE = re.compile(
    r"\b(?:go to|open|check|read|look at|catch me up on|"
    r"what(?:'?s| is) (?:in|happening in|going on in))\b"
    r"[^.]*?#?([\w.\-]+(?:\s+[\w.\-]+){0,3}?)\s+(?:group|channel)\b"
    r"|\b(?:group|channel)\s+(?:called\s+|named\s+)?#?([\w.\-]+)\b"
    r"|\bslack\b[^.]*?#?([\w.\-]+(?:\s+[\w.\-]+){0,3}?)\s+(?:group|channel)\b",
    re.I)
# You do not always say the word "channel". "Can you read about my chat from
# moon shot?" names a source with "from", and requiring the literal word
# channel or group meant that sentence never reached Slack at all: it fell
# through to the model, which invented "I don't have access to personal chat
# histories" while Friday was connected and could read it.
# Saying any of these IS asking to be caught up, with no need to also name a
# noun: "what was discussed in X" has no word like chat or messages in it.
_TALKREAD_RE = re.compile(
    r"\b(?:what (?:was|were|got) (?:talked|discussed|said|asked)|"
    r"what (?:did|does)\s+[\w']+\s+(?:say|ask|want)|"
    r"catch me up|what happened|what'?s new)\b", re.I)
# These are about reading, but could be about anything, so they need a subject.
_READVERB_RE = re.compile(r"\b(?:read|summar(?:ise|ize)|go through)\b", re.I)
_SUBJECT_RE = re.compile(
    r"\b(?:chat|chats|messages?|conversation|thread|dms?|talk)\b", re.I)
# The source of it: "from moon shot", "in moonshot".
_SOURCE_RE = re.compile(
    r"\b(?:from|in|on)\s+(?:my\s+|the\s+|our\s+)?"
    r"([\w.\-]+(?:\s+[\w.\-]+){0,2})", re.I)

# Words that ride along with a spoken channel name but are never part of it.
# "read the chat in slack moonshot group" otherwise yields the channel name
# "chat in slack moonshot".
_NOT_NAME = {"to", "in", "at", "on", "my", "the", "our", "a", "that", "this",
             "chat", "slack", "message", "messages", "conversation", "read",
             "last", "few", "recent", "from"}


def _clean_channel(name: str) -> str:
    words = [w for w in (name or "").split()]
    while words and words[0].lower().strip(".,") in _NOT_NAME:
        words.pop(0)
    while words and words[-1].lower().strip(".,") in _NOT_NAME:
        words.pop()
    return " ".join(words).strip()
_ISSUES_RE = re.compile(r"\b(?:open\s+)?issues?\b", re.I)
_BROKEN_RE = re.compile(
    r"\b(?:what(?:'s| is)? (?:broken|failing|red)|anything (?:broken|failing)|"
    r"failing (?:tests?|builds?|ci|workflows?)|ci status|builds? failing)\b", re.I)
_ACTIVITY_RE = re.compile(
    r"\b(?:what have i been (?:doing|up to)|my (?:recent )?activity|"
    r"what did i (?:do|push|ship))\b", re.I)
_MAIL_RE = re.compile(r"\b(?:e-?mail|gmail|inbox|mails?)\b\s*(.*)$", re.I)
_JIRA_RE = re.compile(r"\bjira\b|\btickets?\b", re.I)
# "are you using Claude for this?" A fair question with a real answer, and one
# a language model asked to improvise will get wrong in the flattering direction.
_ENGINE_RE = re.compile(
    r"\b(?:are|do)\s+you\s+(?:using|use)\s+(?:claude|chatgpt|gpt|openai|an?\s+api)"
    r"|\b(?:what|which)\s+(?:model|brain|llm|engine)\b"
    r"|\bis\s+(?:this|that)\s+claude\b"
    r"|\bwhere\s+(?:do|does)\s+(?:my|the)\s+(?:data|messages?|audio)\s+go\b", re.I)
# "did we ever talk about this?" right after reading something
_DIDWE_RE = re.compile(
    r"\b(?:did (?:we|i) (?:ever )?(?:talk|discuss|work)|have (?:we|i) "
    r"(?:ever )?(?:talked|discussed|worked)|look into claude|check claude|"
    r"any (?:past )?session about)\b", re.I)
# NOT anchored to the start: spoken input arrives as "Friday, connect slack
# xoxp-…" and requiring "connect" first meant it never matched.
_CONNECT_RE = re.compile(r"\bconnect\s+(?:to\s+)?(\w+)(?:\s+([\w.\-]{8,}))?", re.I)
# A pasted token is unmistakable, so accept it on its own and work out where it
# belongs from its prefix. Asking someone to remember command syntax while
# holding a secret in their clipboard is bad design.
# The xoxe. prefix MUST come first in the alternation, or the pattern matches
# the xoxp- part in the middle of "xoxe.xoxp-…" and saves a truncated token
# that can never work. Slack issues xoxe.xoxp- when token rotation is on.
_TOKEN_RE = re.compile(
    r"(xoxe\.xoxp-[\w.\-]{10,}|xoxe-[\w.\-]{10,}|xoxp-[\w-]{10,}"
    r"|xoxb-[\w-]{10,}|ya29\.[\w.\-]{20,})")
_CONNS_RE = re.compile(
    r"\b(?:what(?:'?s| is)? connected|connections?|integrations?|"
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
    # a bare token, pasted with no command around it
    m = _TOKEN_RE.search(t)
    if m:
        tok = m.group(1)
        # xoxe.xoxp- and xoxe- are Slack too (rotation-enabled tokens). Missing
        # them here meant a pasted token silently became "what's connected?".
        which = ("slack" if tok.startswith(("xoxp-", "xoxb-", "xoxe.", "xoxe-"))
                 else "gmail" if tok.startswith("ya29.") else "")
        return CONNECT, {"which": which, "token": tok}
    if _CONNS_RE.search(t):
        return CONNECT, {"which": "", "token": ""}
    m = _CONNECT_RE.search(t)
    if m:
        return CONNECT, {"which": m.group(1), "token": m.group(2) or ""}
    if _ENGINE_RE.search(t):
        return ENGINE, {}
    if _DIDWE_RE.search(t):
        return DID_WE, {}
    m = _READCHAN_RE.search(t)
    if m:
        name = _clean_channel(next((g for g in m.groups() if g), ""))
        if name:
            return READ_CHANNEL, {"channel": name}
    # "read my chat from X" / "what was discussed in X": a read verb, something
    # to read, and a named source, with no need to say the word channel.
    if _TALKREAD_RE.search(t) or (_READVERB_RE.search(t)
                                  and _SUBJECT_RE.search(t)):
        # Hand over the sentence as well as the best guess at the name. Pulling
        # the name out by grammar picks the wrong preposition often enough that
        # the sentence itself has to stay available: the real channel list is a
        # far better anchor than the shape of the request.
        srcs = [_clean_channel(m.group(1)) for m in _SOURCE_RE.finditer(t)]
        srcs = [x for x in srcs if x]
        return READ_CHANNEL, {"channel": srcs[-1] if srcs else "",
                              "said": t}
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
            want = bool(_ASKED_RE.search(t[:m2.start(3)] if m2.group(3)
                                         else t))
            # "ask X for a summary" leaves the message as "for a summary",
            # which is not a sentence anyone would type into an agent.
            if want and msg.lower().startswith("for "):
                msg = "Give me " + msg[4:]
            return TELL, {"name": name, "message": msg, "await": want}
    m = _TELL_RE.search(t)
    if m:
        name, msg = m.group(1), m.group(2).strip()
        # "tell voicebridge him that X" / "tell it that X": drop the pronoun
        msg = re.sub(r"^(?:him|her|it|them)\s+(?:that\s+)?", "", msg, flags=re.I)
        if name.lower() not in _PRONOUNS and msg:
            want = bool(_ASKED_RE.search(t[:m.start(1)]))
            # "ask X for a summary" leaves the message as "for a summary",
            # which is not a sentence anyone would type into an agent.
            if want and msg.lower().startswith("for "):
                msg = "Give me " + msg[4:]
            return TELL, {"name": name, "message": msg, "await": want}
    m = _OPEN_RE.search(t)
    if m and len(t.split()) <= 6:      # a command, not a sentence about opening
        name = m.group(2)
        if name.lower() in _PRONOUNS:
            return OPEN, {"name": ""}   # "open it": we must ask which
        return OPEN, {"name": name}
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
        # An offer Friday just made ("did you mean X?"), and what to do about
        # the answer. One mechanism for every kind of name, so a session, a
        # channel and a connector all behave the same way when misheard.
        self._offered = None
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

        # Something Friday offered a moment ago ("did you mean X?"). A yes takes
        # it; a short reply is another go at the name. Letting either fall
        # through to the model produced the same invented refusal three times
        # while the answer sat in a list Friday had already fetched.
        if self._offered and intent in (CONFIRM, CANCEL, CHAT):
            offer, self._offered = self._offered, None
            if intent == CONFIRM:
                return offer["yes"]()
            if intent == CANCEL:
                return self._say(offer.get("no") or "Okay. Which one did you "
                                                   "mean?")
            if len(text.split()) <= 4 and offer.get("again"):
                return offer["again"](text)
            self._offered = offer          # not an answer; the offer stands
        elif self._offered:
            self._offered = None           # you moved on; it is no longer live

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
            return self._read_channel(payload["channel"],
                                      payload.get("said", ""))
        if intent == ENGINE:
            return self._engine()
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
            return self._propose_tell(payload["name"], payload["message"],
                                      payload.get("await", False))
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
        which = (which or "").strip().lower()   # "Friday Connect Slack." -> slack
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
            # If Friday already built your Slack app, the only thing left is the
            # click, so "connect slack" should just re-open that page. Asking
            # for a fresh configuration token to retry a click you missed is
            # punishing you for reading slowly.
            if which == "slack" and connectors.can_resume():
                def _again():
                    r = connectors.resume_setup()
                    if r.get("ok"):
                        who = r.get("who") or ""
                        self.announce("Slack is connected"
                                      + (f", I can see you as {who}." if who
                                         else "."))
                    else:
                        self.announce("Slack setup stopped: "
                                      + r.get("error", "") + ".")
                import threading
                threading.Thread(target=_again, daemon=True).start()
                return self._say("Your Slack app is already built, so all "
                                 "that's left is the click. Opening that page "
                                 "now, press Allow. No rush.")
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
            guess = nearest.suggest(which, list(connectors.all_connectors()))
            if guess:
                return self._offer(
                    f"I don't have a connector called {which}. Did you mean "
                    f"{guess}?",
                    yes=lambda g=guess: self._connect(g, ""),
                    again=lambda t: self._connect(t.strip().lower(), ""))
            return self._say(f"I don't know a connector called {which}. I have: "
                             + ", ".join(sorted(connectors.all_connectors())))
        c = connectors.get(which)
        if not c:
            guess = nearest.suggest(which, list(connectors.all_connectors()))
            if guess:
                return self._offer(
                    f"I don't have a {which} connector. Did you mean {guess}?",
                    yes=lambda g=guess, tk=token: self._connect(g, tk))
            return self._say(f"I don't have a {which} connector. I have: "
                             + ", ".join(sorted(connectors.all_connectors())))
        # A Slack App Configuration Token is not a credential to store, it is
        # permission to BUILD. Given one, do the six manual screens (create the
        # app, add ten scopes, install, copy the token) automatically, and leave
        # one click for you. Storing it instead was the dead end: it passes
        # auth.test and can read nothing.
        if which == "slack" and connectors.is_config_token(token):
            def _build():
                r = connectors.setup_from_config_token(token)
                if r.get("ok"):
                    who = r.get("who") or ""
                    self.announce("Slack is connected"
                                  + (f", I can see you as {who}." if who else ".")
                                  + " Try: go to my <channel> group in slack "
                                    "and read the chat.")
                else:
                    self.announce("Slack setup stopped: " + r.get("error", "") + ".")
            import threading
            threading.Thread(target=_build, daemon=True).start()
            return self._say("That's a configuration token, which is even "
                             "better: I'll build the Slack app myself with the "
                             "right permissions. A browser tab is opening now. "
                             "Press Allow and you're done.")
        if not connectors.save_secret(f"{which}_token", token):
            return self._say(f"I couldn't save the {which} token.")
        # Verify against the BUILT-IN connector (the token path), not the MCP
        # wrapper, which looks for a token in a different place.
        c = connectors.REGISTRY.get(which) or c
        ok = False
        try:
            ok = c.ready()
        except Exception:
            ok = False
        if not ok:
            # Say WHICH thing is wrong. "isn't answering" sent you round the
            # same loop three times with no way to tell what to change.
            why = ""
            try:
                if hasattr(c, "token_problem"):
                    why = c.token_problem()
            except Exception:
                why = ""
            if why:
                return self._say(f"Saved it, but {why}.")
            return self._say(f"Saved it, but {which} isn't answering. The token "
                             f"may be missing a scope.")
        who = ""
        try:
            who = c.whoami() if hasattr(c, "whoami") else ""
        except Exception:
            pass
        extra = f" I can see you as {who}." if who else ""
        return self._say(f"{which} connected.{extra} Try: go to my <channel> "
                         f"group in slack and read the chat.")

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

    def _read_channel(self, name: str, said: str = "") -> dict:
        # Look for a real channel name anywhere in what was actually said,
        # before falling back to whatever the grammar suggested.
        if said:
            sl = connectors.get("slack")
            if sl.ready() and hasattr(sl, "channel_names"):
                found = nearest.best_window(said, sl.channel_names(40))
                if found:
                    return self._read_channel_named(found, said)
        if not (name or "").strip():
            sl = connectors.get("slack")
            if not sl.ready():
                return self._say("Slack isn't connected yet. " + sl.setup_hint())
            names = (sl.channel_names(40) if hasattr(sl, "channel_names") else [])
            if names:
                return self._offer(
                    "Which one? I can see: " + ", ".join("#" + n for n in names[:8]),
                    yes=lambda: self._read_channel(names[0]),
                    again=lambda t: self._read_channel(t))
            return self._say("Which channel?")
        return self._read_channel_named(name, said)

    def _read_channel_named(self, name: str, said: str = "") -> dict:
        """Read an actual channel and tell you what is being asked.

        This is the front of the chain: read the thread, then 'did we ever talk
        about this?' searches your sessions using what was just read, so you
        never have to retype the subject."""
        sl = connectors.get("slack")
        if not sl.ready():
            return self._say("Slack isn't connected yet. " + sl.setup_hint())
        err = (lambda: sl.last_error() if hasattr(sl, "last_error") else "")
        ch = sl.find_channel(name)
        if not ch:
            # Distinguish "no such channel" from "Slack wouldn't give me the
            # list", which look identical from here but need different fixes.
            why = err()
            if why:
                return self._say("I couldn't look at your channels: " + why + ".")
            # Friday HAS the list, so "I can't find it" on its own is withholding
            # the answer. Saying the real names turns a dead end into a choice,
            # which is what three rounds of 'Munsheer' actually needed.
            names = sl.channel_names(40) if hasattr(sl, "channel_names") else []
            guess = nearest.suggest(name, names)
            if guess:
                return self._offer(
                    f"I don't have a channel called {name}. Did you mean "
                    f"#{guess}?",
                    yes=lambda g=guess, q=said: self._read_channel_named(g, q),
                    again=lambda t: self._read_channel(t))
            if names:
                return self._say(f"I don't have a channel called {name}. What I "
                                 f"can see: " + ", ".join("#" + n for n in names[:8]))
            return self._say(f"I can't find a Slack channel called {name}.")
        rows = sl.read_channel(ch["id"], limit=15)
        if not rows:
            why = err()
            if why:
                return self._say(f"I couldn't read #{ch['name']}: " + why + ".")
            return self._say(f"#{ch['name']} is empty.")
        self._last_slack = rows
        convo = "\n".join(f"{r['who']}: {r['text']}" for r in rows[-12:])
        summary = self._summarise_thread(convo, said)
        # Say WHICH messages were read. Asked what was discussed yesterday and
        # given a summary with no timeframe, you cannot tell whether Friday
        # honoured "yesterday" or quietly ignored it.
        span = ""
        stamps = [r.get("when") or 0 for r in rows if r.get("when")]
        if stamps:
            span = (f" (last {len(rows)} messages, "
                    f"{memory.ago(min(stamps))} to {memory.ago(max(stamps))})")
        return self._say(f"In #{ch['name']}{span}:\n{summary}")

    def _summarise_thread(self, convo: str, question: str = "") -> str:
        """What is actually being ASKED, in a sentence or two.

        If you asked something specific ("what did Sam say"), answer THAT
        from the thread. Returning the same general summary whatever was asked
        is a way of not listening."""
        if not (engine.AVAILABLE and engine.brain.up()):
            return convo[:600]
        # Names, never he or she: these are real colleagues and the messages do
        # not say anyone's pronouns, so guessing gets it wrong about a person.
        RULES = (" Refer to people by name, and never use he, she, his or her."
                 " Two or three short sentences, no lists.")
        task = ("Summarise this chat for someone who has not read it. Say who is "
                "asking what, and what they need. Only use what is in the "
                "messages." + RULES)
        q = (question or "").strip()
        if q and len(q.split()) > 2:
            task = ("Answer this question using ONLY these messages: " + q[:160]
                    + "\nAnswer it directly. If the messages genuinely do not "
                      "cover it, say only that and summarise what they do say. "
                      "Never do both: do not open by denying something you then "
                      "answer." + RULES)
        out = engine.brain._chat(
            [{"role": "system", "content": task},
             {"role": "user", "content": convo[:4000]}],
            timeout=engine.brain.TIMEOUT_SLOW, max_tokens=180)
        return engine.brain._clean(out) if out else convo[:600]

    def _engine(self) -> dict:
        """What is actually doing the work, stated plainly.

        Asked this, a model with no facts will say whatever sounds reassuring.
        The real answer matters: it decides whether reading your Slack means
        sending it to somebody else's server."""
        bits = []
        if engine.AVAILABLE:
            name = (Path(getattr(engine.brain, "MODEL_PATH", "")).stem
                    or "a local model")
            up = False
            try:
                up = engine.brain.up()
            except Exception:
                pass
            bits.append(f"No, not Claude. I think with {name}, which runs on "
                        f"this Mac" + ("" if up else " (not loaded yet)"))
            bits.append("speech in and out is local too, whisper and Kokoro")
        else:
            bits.append("No, not Claude. My local brain isn't available right "
                         "now, so I'm only doing the parts that need no model")
        bits.append("what I read from Slack or GitHub stays here; nothing is "
                    "sent to Anthropic or anyone else")
        return self._say(". ".join(b[0].upper() + b[1:] if b else b
                                   for b in bits) + ".")

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
        hit_on = ", ".join(h.get("matched") or [])
        # A match on two common words out of five is a coincidence, not a
        # memory. Saying a flat "Yes" to it is the bluffing failure: the answer
        # sounds certain and the session it names has nothing to do with the
        # subject. Claim it only when most of the distinctive terms are there.
        need = max(2, (h.get("terms") or 1) // 2 + 1)
        strong = bool(h.get("phrase")) or len(h.get("matched") or []) >= need
        if not strong:
            self._last_found = hits
            return self._say(
                f"Probably not. I searched for {terms}, and the closest is "
                f"{memory.ago(h['when'])}{where}, but it only matches on "
                f"{hit_on}: {(h.get('about') or '')[:90]}\n\nSay \"open that "
                f"one\" if it is the one, or I can start a new session on it.")
        return self._say(
            f"Yes. {memory.ago(h['when'])}{where}, matching {hit_on}: "
            f"{(h.get('about') or '')[:110]}"
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
            # An empty list can mean "no messages" or "Slack refused". Saying
            # "nothing found" for a refusal is the bug that made this feel
            # broken with no way to tell what to fix.
            why = sl.last_error() if hasattr(sl, "last_error") else ""
            if why:
                return self._say("I couldn't search Slack: " + why + ".")
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

    def _no_session(self, name: str, retry) -> dict:
        """A session name Friday does not recognise, handled like every other
        unrecognised name: offer the closest, or say what does exist."""
        names = self._names_of_sessions()
        guess = nearest.suggest(name, names)
        if guess:
            return self._offer(
                f"I don't have a session called {name}. Did you mean {guess}?",
                yes=lambda g=guess: retry(g), again=retry)
        if names:
            return self._say(f"I don't have a session called {name}. Running "
                             f"now: " + ", ".join(names[:8]))
        return self._say(f"I can't find a session called {name}, and nothing "
                         f"is running.")

    def _what_needs(self, name: str) -> dict:
        """Report exactly what one agent is waiting on, and remember that it is
        waiting, so your very next message can just be the answer."""
        hit, how = self._find_how(name)
        if not hit:
            return self._no_session(name, self._what_needs)
        if how == "maybe":
            # A weak match must be ASKED about, never answered as though it were
            # the thing you named: "what is fridey waiting on" reported on
            # voicebridge, which reads as Friday mishearing you and hiding it.
            return self._offer(f"Did you mean {hit.get('label', name)}?",
                               yes=lambda h=hit: self._what_needs(
                                   h.get("label", name)),
                               again=self._what_needs)
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
        hit, how = self._find_how(name)
        if not hit:
            return self._no_session(name, self._propose_open)
        if how == "maybe":
            # Close enough to mention, not close enough to act on unasked.
            return self._offer(
                f"Did you mean {hit.get('label', name)}?",
                yes=lambda h=hit: self._perform(
                    {"kind": "open", "sid": h.get("sid", ""),
                     "label": h.get("label", name)}),
                again=self._propose_open)
        return self._perform({"kind": "open", "sid": hit.get("sid", ""),
                              "label": hit.get("label", name)})

    def _propose_tell(self, name: str, message: str,
                      want_answer: bool = False) -> dict:
        """TIER 0 when you named the session exactly (that was your
        confirmation), TIER 1 when Friday had to guess which one you meant."""
        hit, how = self._find_how(name)
        if not hit:
            return self._no_session(
                name, lambda n: self._propose_tell(n, message, want_answer))
        act = {"kind": "tell", "sid": hit.get("sid", ""),
               "await": want_answer, "path": hit.get("path", ""),
               "label": hit.get("label", name), "message": message}
        if how == "exact":
            return self._perform(act)
        self.pending = act
        self._offered = None     # likewise: the newer question owns "yes"
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
                # Note where the transcript ends BEFORE sending, or the agent's
                # answer cannot be told apart from what it said a minute ago.
                mark = ""
                if act.get("await") and act.get("path"):
                    try:
                        mark = replies.mark(act["path"])
                    except Exception:
                        mark = ""
                ok = actions.send_to_session(act["sid"], act["message"])
                if ok and act.get("await") and act.get("path"):
                    self._bring_back(act["path"], mark, label)
                    return self._say(f"Asked {label}. I'll tell you what it "
                                     f"says.",
                                     action={"kind": "tell",
                                             "sid": act["sid"], "undo": True})
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
        "ask a session a question and bring its answer back here "
        "(say: ask <name> for a summary of changes)",
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

    def _abilities(self) -> tuple:
        """What Friday can do RIGHT NOW, given what is actually connected.

        A fixed list goes stale the moment you connect something: with Slack
        live, the model was still told it could only "search your Slack once you
        connect it", and duly told you it had no access to your channels while
        Friday was reading them."""
        can, cannot = list(self.CAN_DO), []
        try:
            live = {n: v["ready"] for n, v in connectors.status().items()}
        except Exception:
            live = {}
        can += [a for a in self.CAN_DO_MORE if "slack" not in a.lower()]
        if live.get("slack"):
            can.append("read any channel in the Slack workspace you connected, "
                       "and summarise what is being asked")
            can.append("search your Slack messages")
        else:
            cannot.append("read Slack (not connected yet)")
        for name, label in (("gmail", "read your email"),
                            ("jira", "look at your Jira tickets")):
            (can if live.get(name) else cannot).append(
                label + ("" if live.get(name) else " (not connected yet)"))
        cannot += [c for c in self.CANNOT_YET
                   if "jira or email" not in c.lower()]
        return can, cannot

    def _chat(self, text: str) -> dict:
        """Real conversation, bounded by what Friday can actually do.

        The model never answers about the machine from its own head: the live
        facts are handed to it, and anything outside its abilities must be an
        honest 'I can't do that yet' rather than an invention."""
        # Last line of defence against the bluff. If you mention messages AND
        # name a channel that really exists, this is a Slack request however it
        # was phrased, and it must not reach a model that will answer "I don't
        # have access to personal chat histories" about a channel Friday can
        # read. Both conditions are required, so "moonshot is annoying" stays
        # ordinary conversation.
        if _SUBJECT_RE.search(text):
            try:
                sl = connectors.get("slack")
                if sl.ready() and hasattr(sl, "channel_names"):
                    found = nearest.best_window(text, sl.channel_names(40))
                    if found:
                        return self._read_channel_named(found, text)
            except Exception:
                pass
        if not engine.AVAILABLE or not engine.brain.model_ready():
            return self._say("I'm here, but my brain isn't loaded yet.")
        sessions = self._session_facts() or "none"
        can, cannot = self._abilities()
        sys_prompt = (
            "You are Friday, a calm assistant that coordinates a developer's "
            "coding agents. You do not write code.\n\n"
            "ONLY these facts are true; never invent others.\n"
            f"Sessions running right now:\n{sessions}\n"
            "That list is the complete truth about what each session is and what "
            "it is about. Never invent a description for a session; if its "
            "subject is not listed, say you do not know what it is working on.\n\n"
            "You CAN: " + "; ".join(can) + ".\n"
            "You CANNOT yet: " + "; ".join(cannot) + ".\n\n"
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
        """Match a spoken/typed name to a session, EXCLUDING weak matches.

        This drops `how`, so a caller cannot tell a solid match from a guess.
        That makes it the wrong tool for anything that acts, and it must
        therefore never return a 'maybe'.

        Searches the FLEET first, deliberately: those are the names Friday
        shows you, and an assistant that displays "krishojha-7f" then claims it
        cannot find "krishojha-7f" is broken in the most infuriating way. The
        older roster lookup stays as a fallback for sessions the fleet sensor
        cannot see."""
        if not (name and engine.AVAILABLE):
            return None
        hit, how = self._find_how(name)
        return hit if how in ("exact", "fuzzy") else None

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
        #
        # But a miss is not the end. Session names get misheard exactly like
        # channel names do, so if one sounds close, say which and let the user
        # decide. 'sounds-like' may be acted on; 'maybe' may only be offered.
        labels = [(r.get("label") or "") for r in rows]
        how, label = nearest.resolve(name, labels)
        if how in ("sounds-like", "maybe"):
            for r in rows:
                if (r.get("label") or "") == label:
                    return r, ("fuzzy" if how == "sounds-like" else "maybe")
        return None, ""

    def _bring_back(self, path: str, mark: str, label: str) -> None:
        """Watch for the agent's answer and say it here, in this thread.

        Delivering a question and then leaving the answer in a terminal you are
        not looking at is half a conversation, and it is the half that saves you
        nothing: you still have to go to the window to find out."""
        import threading

        def _watch():
            try:
                said = replies.wait_for_reply(path, mark)
            except Exception:
                said = ""
            if said:
                self.announce(f"{label} says: " + said[:700])
            else:
                self.announce(f"{label} hasn't answered yet. It may be waiting "
                              f"on something, or still working.")
        threading.Thread(target=_watch, daemon=True).start()

    def _offer(self, question: str, yes, again=None, no: str = "") -> dict:
        """Ask "did you mean X?" and remember what a yes means.

        Withholding a name Friday already has is the failure this replaces: it
        knows the real list, so the honest move is to put the closest one to you
        rather than report that you said something unrecognisable."""
        self._offered = {"yes": yes, "again": again, "no": no}
        self.pending = None      # a "yes" must have exactly one meaning
        return self._say(question)

    def _names_of_sessions(self) -> list:
        try:
            return [r.get("label") or "" for r in
                    engine.fleet.snapshot().values() if r.get("label")]
        except Exception:
            return []

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
