"""Watch Slack, and bring a message to you with something you can do about it.

The watchtower does this for agents. This is the same idea for people, and it is
the difference between an assistant and a dashboard: nobody wants to be told
"you have 3 unread". What is useful is "Sam is asking for a meeting on
Thursday, want me to draft a reply, or should I ask the api session about the
numbers he wants?"

So a report has three parts, and the third is the point:

    who and where      Sam, in #moonshot
    what they want      a concise overview of your technical work
    what you can do     draft a reply / ask a session / open the thread

What Friday can honestly offer depends on what it can do RIGHT NOW, and it must
never offer something it cannot perform. Posting is off until you turn it on, so
by default "reply" means Friday drafts and you send, and it says exactly that.
Once you have allowed posting, the offer changes to match, because understating
what it can do is its own kind of wrong: you would go and paste it yourself for
no reason.

It only reads channels you are already in, it never reports your own messages
back to you, and it says each thing once.
"""

import re
import threading
import time

from . import connectors, engine

POLL = 45.0           # seconds between looks at Slack
MAX_PER_ROUND = 3     # never more than a few at once, however much arrived
QUIET_FIRST_RUN = True   # the backlog on startup is not news


class Inbox:
    def __init__(self, announce, log=None, hushed=None):
        self.announce = announce
        self._log = log or (lambda *_: None)
        self._own_hush = hushed or (lambda: False)
        self.seen = {}            # channel id -> newest ts reported
        self.me = ""
        self.muted = set()        # channel ids you asked to be spared
        self.last = {}            # channel id -> the message, for "say more"
        self._stop = threading.Event()
        self._started = False

    @property
    def running(self) -> bool:
        return self._started and not self._stop.is_set()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def mute(self, channel: str, on: bool = True) -> None:
        self.muted.add(channel) if on else self.muted.discard(channel)

    # ---- the loop --------------------------------------------------------
    def _loop(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                self._tick(quiet=first and QUIET_FIRST_RUN)
                first = False
            except Exception as e:
                self._log(f"friday inbox: {e}")
            self._stop.wait(POLL)

    def _tick(self, quiet: bool = False) -> None:
        sl = connectors.get("slack")
        if not (sl and getattr(sl, "ready", lambda: False)()):
            return
        if self._hushed():
            return
        if not self.me:
            who = ""
            try:
                who = sl._call("auth.test").get("user_id", "")
            except Exception:
                who = ""
            self.me = who
        fresh = []
        for ch in self._channels(sl):
            cid = ch.get("id", "")
            if not cid or cid in self.muted:
                continue
            since = self.seen.get(cid)
            try:
                rows = sl.read_channel(cid, limit=8,
                                       oldest=(float(since) if since else 0))
            except Exception:
                continue
            if not rows:
                continue
            newest = max((r.get("when") or 0) for r in rows)
            self.seen[cid] = newest
            if quiet or since is None:
                continue          # first sighting of this channel is not news
            for r in rows:
                # Your own messages are not news, and neither is a message you
                # have already been told about.
                if (r.get("when") or 0) <= float(since or 0):
                    continue
                if self._is_me(r):
                    continue
                fresh.append((ch, r))
        fresh.sort(key=lambda p: p[1].get("when") or 0)
        for ch, r in fresh[:MAX_PER_ROUND]:
            self._report(ch, r)
        if len(fresh) > MAX_PER_ROUND:
            n = len(fresh) - MAX_PER_ROUND
            self.announce(f"And {n} more Slack message{'s' if n != 1 else ''} "
                          f"I haven't read out. Say \"what did I miss\" for the "
                          f"list.")

    def _is_me(self, row: dict) -> bool:
        who = (row.get("who") or "").strip()
        return bool(self.me and who == self.me)

    def _channels(self, sl) -> list:
        try:
            rows = sl.channels()
        except Exception:
            return []
        # DMs first: a message addressed only to you is more likely to be for
        # you than one in a channel of forty people.
        return sorted(rows, key=lambda c: 0 if c.get("name", "").startswith("dm")
                      else 1)[:25]

    def _hushed(self) -> bool:
        try:
            if self._own_hush():
                return True
        except Exception:
            pass
        try:
            return bool(engine.attention.is_quiet())
        except Exception:
            return False

    # ---- saying it -------------------------------------------------------
    def _report(self, ch: dict, row: dict) -> None:
        where = "#" + (ch.get("name") or "slack")
        who = row.get("who") or "someone"
        text = " ".join((row.get("text") or "").split())
        # The channel id travels with it: a draft with no destination is a
        # draft you have to send yourself, which is the thing being fixed.
        self.last[ch.get("id", "")] = {"where": where, "who": who,
                                       "text": text,
                                       "channel": ch.get("id", "")}
        gist = text if len(text) <= 200 else text[:200].rsplit(" ", 1)[0] + "…"
        offer = ", ".join(self._offers(text, where))
        self.announce(f"{who} in {where}: {gist}\n{offer}",
                      items=[{"sid": "", "label": where, "kind": "slack"}])

    def _offers(self, text: str, where: str) -> list:
        """What Friday can actually do about this message, right now.

        Never offer what cannot be performed, and never understate it either:
        promising a draft you have to paste, when Friday could send it, sends
        you off to do something by hand for no reason."""
        try:
            can_send = connectors.can_write()
        except Exception:
            can_send = False
        reply = ('say "draft a reply" and I\'ll write one; "send it" and it goes'
                 if can_send else
                 'say "draft a reply" and I\'ll write one you can paste')
        out = [reply]
        if _WANTS_TIME.search(text):
            out.append("tell me a time and I'll put it in the draft")
        out.append('or "ask <session> about this" to put it to an agent')
        return out


# A meeting request, a deadline, a "when are you free": the messages where the
# answer is a time rather than a sentence.
_WANTS_TIME = re.compile(
    r"\b(?:meet|meeting|call|sync|catch\s?up|standup|schedule|calendar|"
    r"invite|when are you (?:free|available)|are you free|book (?:a|some) "
    r"time|jump on)\b", re.I)
