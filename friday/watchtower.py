"""Watch every session, and say what each one said.

voicebridge's attention engine already decides when an agent NEEDS you: it is
blocked, it asked a question, it wants permission. That is the interrupt case,
and it is deliberately quiet, because a machine that speaks constantly gets
muted.

This is the other half, and it is the reason a fleet is hard to hold in your
head: an agent finishes a piece of work and says something, and unless you
happen to be looking at that window, you never learn it. So you go round the
windows checking, which is the job Friday is supposed to take.

So: watch all of them, and when one actually says something, report it here, in
a couple of sentences, without dropping the parts that decide what you do next
(numbers, file names, failures, and anything it is asking you).

Three rules keep this from becoming noise, which is the only way a thing like
this fails:

  - Wait for the agent to STOP talking. An answer arrives in stages, and
    reporting the first stage means reporting "Let me look at that".
  - Say each thing once. Same session, same words, one report.
  - Never speak over yourself. One report at a time, oldest first, and a
    session that is waiting on you jumps the queue.
"""

import re
import threading
import time

from . import engine, fleetcache, replies

POLL = 3.0            # seconds between looks at the fleet
SETTLE = 4.0          # an agent must be quiet this long before it is reported
MAX_REPORT = 600      # characters of raw reply kept for "say more"


class Watchtower:
    def __init__(self, announce, log=None, hushed=None):
        self.announce = announce
        # Asked separately from voicebridge's attention engine, so "quiet" works
        # even when that engine is unreachable.
        self._own_hush = hushed or (lambda: False)
        self._log = log or (lambda *_: None)
        self.seen = {}            # sid -> last reported text
        self.pending = {}         # sid -> (text, first_seen_at)
        self.expecting = set()    # sids Friday asked something, so it can say so
        self.last = {}            # sid -> full text, for "say more"
        self.muted = set()        # sids you have asked not to hear about
        self._stop = threading.Event()
        self._started = False

    @property
    def running(self) -> bool:
        return self._started and not self._stop.is_set()

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def mute(self, sid: str, on: bool = True) -> None:
        """Stop (or resume) reporting one session.

        A muted session is still WATCHED, so its replies are marked as seen
        rather than piling up to be recited the moment you unmute."""
        if on:
            self.muted.add(sid)
        else:
            self.muted.discard(sid)

    def expect(self, sid: str) -> None:
        """Friday asked this session something, so its next reply is an answer
        rather than an aside, and can be introduced as one."""
        if sid:
            self.expecting.add(sid)

    def prime(self, rows=None) -> None:
        """Treat everything already on screen as old news.

        Without this, starting Friday reads out the last thing every session
        happened to say, which is a wall of text about work you already know
        about."""
        for r in (rows if rows is not None else self._fleet()):
            path = r.get("path", "")
            if path:
                try:
                    self.seen[r["sid"]] = replies.mark(path)
                except Exception:
                    pass

    # ---- the loop --------------------------------------------------------
    def _fleet(self) -> list:
        try:
            return [r for r in fleetcache.snapshot().values() if r.get("sid")]
        except Exception:
            return []

    def _loop(self) -> None:
        self.prime()
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                self._log(f"friday watchtower: {e}")
            self._stop.wait(POLL)

    def _tick(self) -> None:
        if self._hushed():
            return
        rows = {r["sid"]: r for r in self._fleet()}
        now = time.time()

        for sid, r in rows.items():
            path = r.get("path", "")
            if not path:
                continue
            try:
                # The agent's own words only. Reading the last message of any
                # role reported Friday's own injected prompt as the answer.
                text = replies.last_said(path)
            except Exception:
                continue
            if not text or text == self.seen.get(sid):
                self.pending.pop(sid, None)
                continue
            prev = self.pending.get(sid)
            if not prev or prev[0] != text:
                self.pending[sid] = (text, now)      # still talking, restart
        # A session that is waiting on you matters more than one that merely
        # finished a thought, so it goes first.
        ready = [(sid, t) for sid, (t, at) in self.pending.items()
                 if now - at >= SETTLE and sid in rows]
        ready.sort(key=lambda p: 0 if (rows[p[0]].get("question") or "")
                   else 1)
        for sid, text in ready:
            self.pending.pop(sid, None)
            self.seen[sid] = text
            if sid in self.muted:
                continue          # watched, marked seen, not mentioned
            self._report(rows[sid], text)

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
    def _report(self, row: dict, text: str) -> None:
        label = row.get("label") or row.get("sid", "a session")
        self.last[row["sid"]] = text[:MAX_REPORT]
        answering = row["sid"] in self.expecting
        self.expecting.discard(row["sid"])
        short = summarise(text, label)
        lead = f"{label} answered" if answering else f"{label} says"
        q = (row.get("question") or "").strip()
        if q and q[:40] not in short:
            short = short.rstrip(".") + f". It's asking: {q}"
        self.announce(f"{lead}: {short}",
                      items=[{"sid": row["sid"], "label": label,
                              "kind": "blocked" if q else "spoke"}])


# The specifics a summary exists to carry, and therefore the specifics it must
# not make up: paths and filenames, shouty identifiers like error codes, and
# numbers big enough to be a line number or a count that matters.
_FILE = re.compile(r"\b[\w./-]*\.[A-Za-z]{1,5}\b")
_CODE = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")
_BIGNUM = re.compile(r"\b\d{3,}\b")


def _invented(summary: str, source: str) -> str:
    """A specific in the summary that is not in the message, or "".

    A 4B model asked to compress "the parser broke on page 3 of the PDF"
    produced "retry with the file named report_2024_q3.pdf ... error code
    PDF_PARSE_003". Both are inventions, and both are exactly the kind of detail
    you would act on. A summary that fabricates specifics is worse than no
    summary at all, so anything it could not have read is grounds to throw the
    whole thing away."""
    low = (source or "").lower()
    for pat in (_FILE, _CODE, _BIGNUM):
        for tok in pat.findall(summary or ""):
            if len(tok) > 2 and tok.lower() not in low:
                return tok
    return ""


def summarise(text: str, label: str = "") -> str:
    """Short enough to hear, complete enough to act on.

    A summary that drops the file name, the number or the failure is worse than
    no summary: it sounds like an answer and cannot be used, so you go and open
    the window anyway, which is the thing this exists to avoid."""
    clean = " ".join((text or "").split())
    if len(clean) <= 240:
        return clean
    if not (engine.AVAILABLE and engine.brain.up()):
        return clean[:240].rsplit(" ", 1)[0] + "…"
    try:
        out = engine.brain._chat(
            [{"role": "system", "content":
              "Compress this message from a coding agent to two short "
              "sentences for someone who did not see it. Keep every specific "
              "that decides what to do next: file names, numbers, errors, "
              "what was decided, and anything it is asking. Drop pleasantries "
              "and restatements of the task. Use names, never he or she. Plain "
              "sentences, no markdown, no lists."},
             {"role": "user", "content": clean[:4000]}],
            timeout=engine.brain.TIMEOUT_SLOW, max_tokens=140)
        out = engine.brain._clean(out) if out else ""
    except Exception:
        out = ""
    if out:
        made_up = _invented(out, clean)
        if made_up:
            # Say the agent's own words instead. Losing brevity is a small cost;
            # reporting a file or an error code that does not exist is not.
            try:
                engine.log(f"friday: dropped an invented summary "
                                f"({made_up})")
            except Exception:
                pass
            out = ""
    return out or (clean[:240].rsplit(" ", 1)[0] + "…")
