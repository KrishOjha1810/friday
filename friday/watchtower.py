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
from .budget import compose

POLL = 3.0            # seconds between looks at the fleet
SETTLE = 4.0          # an agent must be quiet this long before it is reported
MAX_REPORT = 600      # characters of raw reply kept for "say more"
# How long a session that has left the fleet is still remembered, and how many
# are kept at the very most. Everything below is keyed by session id and every
# value is TEXT, so without this the watchtower holds the last words of every
# agent you have ever run. A soak measured it growing linearly and never once
# going down: a day of work makes dozens of ids, a month makes thousands.
#
# Not dropped the instant a session vanishes, because a snapshot can miss one
# for a tick and forgetting its mark means re-reporting what it already said.
FORGET_AFTER = 1800   # half an hour gone is gone
MAX_TRACKED = 200


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
        self.asked = {}           # sid -> the question it is currently blocked on
        self.last = {}            # sid -> full text, for "say more"
        self.muted = set()        # sids you have asked not to hear about
        self._gone = {}           # sid -> when it left the fleet
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
            # Through the vendor seam, and NOT truncated. Two bugs, one line.
            #
            # It called the Claude parser directly, so a Codex or Antigravity
            # session was never primed and Friday recited its entire walkthrough
            # at startup, which is the wall of text prime() exists to prevent.
            #
            # And it stored mark(), which is the first 200 characters, while
            # _tick compares the whole thing. Almost every real agent reply is
            # longer than 200 characters, so prime() suppressed essentially
            # nothing; the existing test passed only because its fixture message
            # was short.
            try:
                from . import agents
                self.seen[r["sid"]] = agents.last_said(r)
            except Exception:
                pass
            q = (r.get("question") or r.get("permission") or "").strip()
            if q:
                # Already blocked when Friday started. Recorded so it is not
                # announced as though it just happened, but it is still there
                # and "who needs me" will say so.
                self.asked[r["sid"]] = q

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
        self._forget(rows, now)

        for sid, r in rows.items():
            path = r.get("path", "")
            try:
                # The agent's own words only, parsed by whoever made it: a
                # Codex rollout and a Claude transcript are different files
                # saying the same thing.
                from . import agents
                text = agents.last_said(r) if path else ""
            except Exception:
                continue
            asking = (r.get("question") or r.get("permission") or "").strip()
            # BECOMING BLOCKED IS ITSELF NEWS. Everything here keyed off the
            # transcript text changing, and a permission prompt is not text: it
            # is a tool_use block Claude is waiting on, or a [!QUESTION] in a
            # file Antigravity writes elsewhere. So the one category this whole
            # thing exists to interrupt for could arrive, sit there, and never
            # be mentioned or even held. It was invisible.
            if asking and self.asked.get(sid) != asking:
                self.asked[sid] = asking
                # The question alone when it has said nothing else. The
                # fallback used to be "needs you: <question>", which the lead
                # then prefixed again: "api needs you: needs you: Continue?".
                self.pending[sid] = (text or asking, now)
                continue
            if not asking:
                self.asked.pop(sid, None)
            if asking and sid in self.pending and not text:
                # A session that hit a permission prompt before saying anything
                # has no text at all. The line below would overwrite the
                # "needs you" fallback with an empty string AND restart the
                # settle timer every tick, so the one case that fallback exists
                # for was announced late and with an empty body.
                continue
            if not text or text == self.seen.get(sid):
                # UNLESS it is waiting on you and has not been reported yet.
                # The line above deleted the entry the blocked branch had just
                # created, on the very next tick, three seconds into a four
                # second settle. So the fix worked only when SETTLE was zero,
                # which is every test and no real run: under stock settings the
                # one category this exists for was still silently swallowed.
                if not (asking and sid in self.pending):
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
        # Blocked ones first, and summarised first, because _prepare calls the
        # local model and that is seconds per session. With fifty sessions
        # talking, one permission prompt waited nearly two minutes behind
        # forty-nine summaries, most of which were then held and never spoken.
        # A prompt answered two minutes late is an agent idle for two minutes,
        # which is the cost this exists to remove.
        batch, deferred = [], []
        for sid, text in ready:
            self.pending.pop(sid, None)
            self.seen[sid] = text
            if sid in self.muted:
                continue          # watched, marked seen, not mentioned
            row = rows[sid]
            if (row.get("question") or row.get("permission") or "").strip():
                got = self._prepare(row, text)
                if got:
                    batch.append(got)
            else:
                deferred.append((row, text))
        # What is left is rationed BEFORE it is summarised, so no model time is
        # spent on something nobody will hear.
        for row, text in self._affordable(deferred):
            got = self._prepare(row, text)
            if got:
                batch.append(got)
        self._say_all(batch)

    def _affordable(self, deferred: list) -> list:
        """Which of the unblocked ones there is budget to say, best first.

        Sorted by urgency, which is what makes learn.py's demotion mean
        anything: it returns a lower tier for a source you never act on, and
        nothing consulted that ordering, so the ignored session spent the last
        token and the one you do act on was held behind it.

        Held here rather than after summarising, in the agent's own words. A
        held item you never hear is not worth a model call, and the raw text is
        a better record of what was said than a summary of it anyway."""
        from . import learn
        scored = []
        for row, text in deferred:
            label = row.get("label") or row.get("sid", "")
            urgency = learn.adjust(1, learn.key_for({"kind": "spoke",
                                                     "label": label}))
            scored.append((urgency, label, row, text))
        scored.sort(key=lambda x: x[0])
        out = []
        for urgency, label, row, text in scored:
            if self.budget and not self.budget.allow(urgency):
                self.budget.hold(f"{label} says: {' '.join(text.split())[:300]}",
                                 label)
                continue
            out.append((row, text))
        return out

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

    def _forget(self, rows: dict, now: float) -> None:
        """Let go of sessions that are over.

        Everything here is keyed by session id and holds text, and session ids
        are created all day. Without this the watchtower is a log of every
        agent you have ever run, kept in memory, scanned on every tick.

        A hard cap on top of the timer, because a timer only helps if time
        passes: something that churns sessions faster than they expire would
        still climb, and a cap that is never reached costs nothing."""
        for sid in list(self._gone):
            if sid in rows:
                self._gone.pop(sid, None)
        # Every dict, not just `seen`. Deriving the departed set from one of
        # them left entries stranded in the others: a session evicted from
        # `seen` was never looked for in `pending`, so `pending` went on
        # growing after the fix that was supposed to stop exactly this.
        tracked = (set(self.seen) | set(self.last) | set(self.pending)
                   | set(self.asked))
        for sid in tracked:
            if sid not in rows:
                self._gone.setdefault(sid, now)
        dead = [sid for sid, when in self._gone.items()
                if now - when > FORGET_AFTER]
        # Over the cap, the longest-gone go first. Only ones that have actually
        # left: a live session is never dropped, however many there are.
        if len(tracked) > MAX_TRACKED:
            extra = sorted(self._gone.items(), key=lambda kv: kv[1])
            want = len(tracked) - MAX_TRACKED
            dead += [sid for sid, _ in extra[:want] if sid not in dead]
        for sid in dead:
            self.seen.pop(sid, None)
            self.last.pop(sid, None)
            self.asked.pop(sid, None)
            self.pending.pop(sid, None)
            self._gone.pop(sid, None)
            self.expecting.discard(sid)
            self.muted.discard(sid)

    # ---- saying it -------------------------------------------------------
    def _prepare(self, row: dict, text: str) -> dict:
        """One report, worked out but not yet said.

        Split from the saying so that several arriving together can be weighed
        against each other and delivered as one thing. Five agents finishing
        within the same second used to be five separate interruptions, which
        costs five times the attention for one moment's worth of news."""
        label = row.get("label") or row.get("sid", "a session")
        self.last[row["sid"]] = text[:MAX_REPORT]
        answering = row["sid"] in self.expecting
        self.expecting.discard(row["sid"])
        short = summarise(text, label)
        q = (row.get("question") or "").strip()
        # "says" understates a session that cannot continue without you, and in
        # a merged list the lead is the only part read at a glance.
        lead = (f"{label} needs you" if q else
                f"{label} answered" if answering else f"{label} says")
        if q and q[:40] not in short:
            short = short.rstrip(".") + f". It's asking: {q}"
        urgency = 0 if q else 1
        # A session you never do anything about after being told costs a tier,
        # so it waits for "what did I miss" rather than interrupting. A session
        # that is ASKING you something is untouchable, which learn.adjust
        # enforces rather than trusting this caller.
        from . import learn
        urgency = learn.adjust(urgency, learn.key_for(
            {"kind": "blocked" if q else "spoke", "label": label}))
        if row.get("sid") and row["sid"] == self._looking_at():
            # You are watching this window. It said it on your screen a moment
            # ago; repeating it here is noise with extra steps. Not held
            # either: you have already seen it.
            return {}
        return {"sid": row["sid"], "label": label, "text": f"{lead}: {short}",
                "urgency": urgency, "asking": bool(q)}

    def _say_all(self, batch: list) -> None:
        """Everything that came in together, as one thing to read.

        Three rules, in this order:

        Anything ASKING you goes first and is never merged away, because the
        whole reason to interrupt is that somebody cannot continue without you.
        Those are what you act on; the rest is news.

        The rest is spent against the budget as before, so a busy minute does
        not become a monologue, and what does not fit is HELD rather than
        dropped. "and four others finished" is a count you can ask about.

        And it is one announcement, not several. The items ride along so the
        page can still offer each of them separately: merging is about the
        interruption, not about what you can act on afterwards."""
        if not batch:
            return
        asking = [b for b in batch if b["asking"]]
        rest = [b for b in batch if not b["asking"]]
        lines, items, held = [], [], 0
        for b in asking:
            lines.append(b["text"])
            items.append({"sid": b["sid"], "label": b["label"],
                          "kind": "blocked"})
        for b in rest:
            # The budget was already spent in _affordable, before any of these
            # were summarised. Spending it twice would hold things that had
            # already paid.
            lines.append(b["text"])
            items.append({"sid": b["sid"], "label": b["label"], "kind": "spoke"})
        held = self.budget.waiting() if self.budget else 0
        if not lines:
            return
        if held:
            lines.append(f"{held} other{'s' if held > 1 else ''} finished too; "
                         f"say \"what did I miss\" for those.")
        body = compose(lines)
        # More than one thing waiting on you is the case where you need telling
        # how many, because you are about to answer one of them and the others
        # have to still exist afterwards. Outside compose() rather than inside
        # it, or the header counts itself as one of the things.
        if len(asking) > 1:
            body = (f"{len(asking)} sessions are waiting on you.\n" + body)
        self.announce(body, items=items)


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
_NUM = re.compile(r"\b(?:0[xX][0-9a-fA-F]+|\d[\d,]*(?:\.\d+)?)\b")
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
    # Words, before the digit and code checks, because neither of those can see
    # them. _NUM matches digits only, so "it broke on page seven" invented a
    # page number and passed untouched; and an instruction is the most
    # actionable thing a summary can carry and the most dangerous to invent, so
    # "run make deploy" and "force-push to main" both got through.
    said = set(re.findall(r"[a-z][a-z\-]*", (summary or "").lower()))
    had = set(re.findall(r"[a-z][a-z\-]*", (source or "").lower()))
    for tok in sorted((said & _WORD_NUM) - had):
        return tok
    for tok in sorted((said & _IMPERATIVE) - had):
        return tok
    src_nums = _numbers(source)
    for tok in _NUM.findall(summary):
        if tok.lower().startswith("0x"):
            # An error code the source never gave is exactly the kind of
            # specific somebody acts on.
            if tok.lower() not in (source or "").lower():
                return tok
            continue
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


# What a message is FOR. If the source says one of these and the summary that
# survives the invention check no longer carries any of them, the summary has
# stopped saying what happened.
_OUTCOME = {
    "bad": ("failed", "failing", "failure", "error", "errors", "broke",
            "broken", "crashed", "aborted", "rejected", "blocked", "refused",
            "timed out", "cannot", "could not", "couldn't", "unable"),
    "good": ("passed", "passing", "finished", "done", "succeeded", "complete",
             "completed", "merged", "deployed", "green", "fixed", "works"),
}
# Numbers written as words. _NUM only sees digits, so "it broke on page seven"
# invented a page number and passed the check untouched.
_WORD_NUM = {"one", "two", "three", "four", "five", "six", "seven", "eight",
             "nine", "ten", "eleven", "twelve", "first", "second", "third",
             "fourth", "fifth", "dozen", "hundred", "thousand"}
# An instruction is the most actionable thing a summary can contain and the
# most dangerous to invent: "run make deploy" and "force-push to main" both got
# through, because neither is digit-shaped or code-shaped.
_IMPERATIVE = {"run", "delete", "remove", "drop", "force-push", "push",
               "merge", "revert", "rollback", "deploy", "restart", "kill",
               "reset", "rebase", "overwrite", "truncate", "disable"}


def _outcome_of(text: str) -> set:
    """Which way the message went: badly, well, or unstated.

    On word boundaries. It tested for the word surrounded by spaces or followed
    by a full stop, so "passed," and "failed," matched neither: ordinary
    punctuation decided whether the anti-fabrication guard fired at all. It
    threw away true summaries ("All 12 tests passed, and the branch is ready")
    and waved through the reassuring rewrite it exists to catch."""
    low = " ".join((text or "").lower().split())
    out = set()
    for kind, words in _OUTCOME.items():
        for w in words:
            if re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", low):
                out.add(kind)
                break
    return out


def _drop_invented(summary: str, source: str) -> str:
    """Remove the sentences that are not supported, keep the rest.

    Throwing the whole summary away for one bad token costs you every true
    sentence in it, and the fallback is a blunt truncation of the source. The
    parts that ARE grounded are still worth having."""
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", summary or ""):
        if sentence.strip() and not _invented(sentence, source):
            kept.append(sentence.strip())
    out = " ".join(kept)
    # Dropping the unsupported sentences can change what the message MEANS.
    # "The nightly build finished. It failed with LNK2019 in main_x64.obj."
    # loses its second sentence to the invented symbol and ships as "The
    # nightly build finished", which is the opposite of what the agent said.
    # A guard against fabrication must not manufacture the reassuring half.
    was, now_ = _outcome_of(source), _outcome_of(out)
    if was and not (was & now_):
        return ""
    return out


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
