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

from . import actions, engine

# What kind of thing the user just said.
ASK_FLEET = "fleet"        # "what's running", "who needs me"
OPEN = "open"              # "open jobhunt", "switch to api"
TELL = "tell"              # "tell api to use redis"  (routed to an agent)
CONFIRM = "confirm"        # "yes", "do it"
CANCEL = "cancel"          # "no", "cancel"
QUIET = "quiet"            # "quiet", "stop talking"
RESUME = "resume"          # "resume", "you can talk again"
NEEDS = "needs"            # "what does api need?"
CHAT = "chat"              # anything else: a real conversation

_FLEET_RE = re.compile(
    r"\b(what('?s| is)? (running|going on|happening)|status|who needs me|"
    r"which (agents?|sessions?) need|what are you (running|watching)|"
    r"how are (things|we)|(show|list) (me )?(my )?(agents?|sessions?))\b", re.I)
_OPEN_RE = re.compile(
    r"\b(open|switch to|go to|jump to|resume|show me)\s+(?:the\s+)?"
    r"(?:session\s+)?([\w.\-]+)", re.I)
_TELL_RE = re.compile(
    r"^\s*(?:tell|reply to|answer)\s+([\w.\-]+)\s+(?:to\s+)?(.+)$", re.I)
_NEEDS_RE = re.compile(
    r"\b(?:what (?:does|is)|why (?:does|is))\s+([\w.\-]+)\s+"
    r"(?:need|want|waiting|asking|blocked|stuck)", re.I)
_YES_RE = re.compile(r"^\s*(yes|yeah|yep|sure|do it|go ahead|please|ok(ay)?)\b", re.I)
_NO_RE = re.compile(r"^\s*(no|nope|cancel|stop|don'?t|never ?mind)\b", re.I)
_QUIET_RE = re.compile(r"^\s*(quiet|shush|be quiet|stop talking|silence)\b", re.I)
_RESUME_RE = re.compile(r"^\s*(resume|unmute|you can talk|start talking)\b", re.I)


def classify(text: str) -> tuple:
    """(intent, payload). Deterministic and ordered: the most specific command
    wins, and only what is left over counts as conversation."""
    t = (text or "").strip()
    if not t:
        return CHAT, {}
    if _QUIET_RE.match(t):
        return QUIET, {}
    if _RESUME_RE.match(t):
        return RESUME, {}
    m = _TELL_RE.match(t)
    if m:
        return TELL, {"name": m.group(1), "message": m.group(2).strip()}
    m = _OPEN_RE.search(t)
    if m and len(t.split()) <= 6:      # a command, not a sentence about opening
        return OPEN, {"name": m.group(2)}
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
    def _chat(self, text: str) -> dict:
        """Real conversation. The local model answers, with what is actually
        happening on the machine as context, so 'how's it going' gets a real
        answer rather than a chatbot one."""
        if not engine.AVAILABLE or not engine.brain.model_ready():
            return self._say("I'm here, but my brain isn't loaded yet.")
        sys_prompt = (
            "You are Friday, a calm, competent assistant for a developer. You "
            "coordinate their coding agents; you do not write code yourself. "
            "Answer in one or two short sentences, plainly, no lists, no "
            "markdown. If you do not know, say so.\n\n"
            f"What is running right now: {self.fleet_summary()}")
        recent = [{"role": "user" if m["role"] == "user" else "assistant",
                   "content": m["text"]} for m in self.history[-6:]]
        try:
            out = engine.brain._chat(
                [{"role": "system", "content": sys_prompt}] + recent,
                timeout=engine.brain.TIMEOUT_SLOW, max_tokens=120)
        except Exception:
            out = ""
        return self._say(engine.brain._clean(out) if out
                         else "I didn't catch that, say it another way?")

    # ---- helpers ----------------------------------------------------------
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
        try:
            hit = engine.sessions.find(name)
            return (hit, "fuzzy") if hit else (None, "")
        except Exception:
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


def _join(names: list) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def _is(n: int) -> str:
    return "is" if n == 1 else "are"
