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
    def __init__(self, announce, log=None, hushed=None, budget=None):
        self.announce = announce
        # This is the highest-volume source of the three and had no cap
        # whatsoever, while the other two rationed themselves carefully.
        self.budget = budget
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
                # The agent's own words only, parsed by whoever made it: a
                # Codex rollout and a Claude transcript are different files
                # saying the same thing.
                from . import agents
                text = agents.last_said(r)
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

    def _looking_at(self) -> str:
        """The session whose window is in front of you right now, or "".

        Telling somebody what is on their own screen is the cheapest possible
        way to be annoying, and voicebridge already works this out from the
        frontmost terminal's tty. Friday had the signal available and never
        asked for it."""
        try:
            from vb import signals
            return signals.gather().get("focused_sid") or ""
        except Exception:
            return ""

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
        urgency = 0 if q else 1
        if row.get("sid") and row["sid"] == self._looking_at():
            # You are watching this window. It said it on your screen a moment
            # ago; repeating it here is noise with extra steps. Not held
            # either: you have already seen it.
            return
        if self.budget and not self.budget.allow(urgency):
            self.budget.hold(f"{lead}: {short}", label)
            return
        self.announce(f"{lead}: {short}",
                      items=[{"sid": row["sid"], "label": label,
                              "kind": "blocked" if q else "spoke"}])


# What a summary exists to carry, and therefore what it must not make up.
#
# The first version of this checked filenames, ALL-CAPS words, and numbers of
# three digits or more. Measured on 18 cases it caught 2 of 9 inventions and
# falsely rejected 2 of 9 good summaries, and on benign paraphrases it threw
# away 8 of 10: "e.g.", "HTTP", "JSON", "TODO", a year, "100 percent", and
# "4096" when the source had written "4,096". Each of those discarded a
# perfectly good summary. Meanwhile it missed the exact failure in its own
# docstring whenever the fabricated number was small, because "page 3" becoming
# "page 7" is under the three-digit floor.
#
# So: every number counts, compared by VALUE rather than as text, and an
# identifier counts only when it is actually code-shaped. Shouting is not a
# code.
_NUM = re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")
# A dot with an extension after it, a slash, an underscore, or digits glued to
# letters. That is report_2024_q3.pdf and PDF_PARSE_003; it is not HTTP, and it
# is not TODO merely because a sentence ended. It must begin with a letter, so
# bare numbers are left to the number check rather than being judged twice.
_IDENT = re.compile(
    r"\b(?=[A-Za-z0-9._/\-]*(?:\.[A-Za-z0-9]|[_/]|\d[A-Za-z]|[A-Za-z]\d))"
    r"[A-Za-z][A-Za-z0-9._/\-]{2,}\b")
# Ordinary English that happens to look like an identifier.
_NOT_A_CODE = {"e.g", "e.g.", "i.e", "i.e.", "etc", "etc.", "vs.", "v1", "v2",
               "no.", "u.s"}


def _numbers(text: str) -> set:
    """Numeric VALUES, so 4,096 and 4096 are the same fact."""
    out = set()
    for tok in _NUM.findall(text or ""):
        try:
            out.add(float(tok.replace(",", "")))
        except ValueError:
            continue
    return out


def _invented(summary: str, source: str) -> str:
    """The first specific in the summary that the message does not support.

    A 4B model asked to compress "the parser broke on page 3 of the PDF"
    produced "retry with the file named report_2024_q3.pdf ... error code
    PDF_PARSE_003". Both invented, both exactly the kind of detail you would act
    on."""
    if not (summary and source):
        return ""
    src_nums = _numbers(source)
    for tok in _NUM.findall(summary):
        try:
            val = float(tok.replace(",", ""))
        except ValueError:
            continue
        if val not in src_nums:
            return tok
    low = (source or "").lower()
    for tok in _IDENT.findall(summary):
        t = tok.lower()
        if t in _NOT_A_CODE or len(t) < 4:
            continue
        # Tolerate a plural of something the source really said.
        if t in low or (t.endswith("s") and t[:-1] in low):
            continue
        return tok
    return ""


def _drop_invented(summary: str, source: str) -> str:
    """Remove the sentences that are not supported, keep the rest.

    Throwing the whole summary away for one bad token costs you every true
    sentence in it, and the fallback is a blunt truncation of the source. The
    parts that ARE grounded are still worth having."""
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", summary or ""):
        if sentence.strip() and not _invented(sentence, source):
            kept.append(sentence.strip())
    return " ".join(kept)


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
              "sentences, no markdown, no lists.\n"
              # Measured: without this the model invented a specific in 4 of 20
              # summaries. With it, 0 of 20, and slightly FASTER, because it
              # stops padding. Prevention beats detection and costs nothing.
              "Every file name, number, identifier and error code you write "
              "must appear in the message itself. Do not add a file name, a "
              "number, an error code, or an action the message does not "
              "state."},
             {"role": "user", "content": clean[:4000]}],
            timeout=engine.brain.TIMEOUT_SLOW, max_tokens=140)
        out = engine.brain._clean(out) if out else ""
    except Exception:
        out = ""
    if out:
        made_up = _invented(out, clean)
        if made_up:
            try:
                engine.log(f"friday: unsupported specific in a summary "
                           f"({made_up})")
            except Exception:
                pass
            # Drop only what is unsupported. One bad token used to cost every
            # true sentence alongside it.
            out = _drop_invented(out, clean)
    return out or (clean[:240].rsplit(" ", 1)[0] + "…")
