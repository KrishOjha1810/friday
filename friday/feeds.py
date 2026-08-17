"""Everything that wants your attention, in one place, with one set of rules.

Friday already watches agents (watchtower) and Slack (inbox). Adding GitHub, then
git, then a calendar, then Jira, each with its own polling loop and its own idea
of what is worth saying, is how an assistant becomes a notification firehose. Each
source would get the rate limiting slightly wrong, in a different way, and the
result is something you mute.

So sources are dumb and the dispatcher is smart. A source knows only how to
answer "what is new?" with a list of items. Everything that decides whether you
hear about it lives here, once:

    say each thing once            a stable key per item
    the urgent first               needs-you outranks worth-knowing
    never more than a few          a cap per round and per hour
    quiet means quiet              including per-source
    admit what was held back       a silent cap reads as "nothing happened"

An item is a dict: key, source, urgency (0 needs you, 1 worth knowing, 2
background), text, and offers, which are the things you can actually do about it.
Offers are strings, and a source must never write one Friday cannot perform.
"""

import datetime as _dt
import json
import subprocess
import threading
import time

from . import engine

PER_ROUND = 3         # most items announced in one pass. The hourly ceiling
                      # lives in budget.Budget now, shared with the watchtower
                      # and the inbox, because three private counters produced
                      # three separate "and N more" notes in one second.
TIMEOUT = 12          # seconds any one source gets to answer


class _Seen:
    """The keys already announced, oldest dropped first past a cap.

    Deliberately large: the cost of forgetting a key is announcing something
    twice, which is annoying, and the cost of a small cap is doing that
    regularly. Ten thousand keys is a few hundred kilobytes and more than any
    real day produces."""

    MAX = 10_000

    def __init__(self):
        self._d = {}

    def add(self, key):
        self._d[key] = None
        if len(self._d) > self.MAX:
            for k in list(self._d)[:len(self._d) - self.MAX]:
                self._d.pop(k, None)

    def discard(self, key):
        self._d.pop(key, None)

    def __contains__(self, key):
        return key in self._d

    def __len__(self):
        return len(self._d)

    def __iter__(self):
        return iter(self._d)


class Feeds:
    def __init__(self, announce, log=None, hushed=None, budget=None):
        self.announce = announce
        # Shared with the watchtower and the inbox. Three separate counters
        # produced three separate "and N more" notes in the same second.
        self.budget = budget
        self._log = log or (lambda *_: None)
        self._own_hush = hushed or (lambda: False)
        self.sources = {}         # name -> (source, period, last_polled)
        # Keys already announced, newest last. A set, until a soak showed it
        # only ever growing: every calendar event, every Sentry issue and every
        # broken workflow adds a key and nothing ever removes one. A dict keeps
        # insertion order, so trimming drops the oldest rather than whichever
        # the hash happened to put first.
        self.seen = _Seen()
        # What was actually SAID under each key, so a reused key carrying
        # different words can be told apart from a genuine repeat.
        self.said = {}
        self.muted = set()        # source names you asked to be spared
        self.held = 0             # items not announced, so it can say so
        self._stop = threading.Event()
        self._started = False

    def add(self, name: str, source, period: float = 120.0) -> None:
        self.sources[name] = [source, period, 0.0]

    def mute(self, name: str, on: bool = True) -> None:
        self.muted.add(name) if on else self.muted.discard(name)

    @property
    def running(self) -> bool:
        return self._started and not self._stop.is_set()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        # Prime on the loop's own thread, not here. Priming asks every source
        # what it has, which shells out to `gh`, scans your repos and wakes
        # Calendar: measured at 3.2 seconds, and it was happening BEFORE the
        # server bound its port, so the page could not load until it finished.
        # A source that hangs would have meant a Friday that never starts.
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ---- polling ---------------------------------------------------------
    def _loop(self) -> None:
        # Everything present at startup is history, not news.
        try:
            self._collect(prime=True)
        except Exception as e:
            self._log(f"friday feeds prime: {e}")
        while not self._stop.is_set():
            try:
                self._collect()
            except Exception as e:
                self._log(f"friday feeds: {e}")
            self._stop.wait(15)

    def _collect(self, prime: bool = False) -> None:
        now = time.time()
        fresh = []
        for name, entry in self.sources.items():
            source, period, last = entry
            if now - last < period:
                continue
            entry[2] = now
            if name in self.muted and not prime:
                continue
            try:
                items = source.poll() or []
            except Exception as e:
                self._log(f"friday feed {name}: {e}")
                continue
            for it in items:
                it.setdefault("source", name)
                key = it.get("key") or (name + ":" + it.get("text", "")[:60])
                it["key"] = key
                if key in self.seen:
                    # Same key, different words, is not a duplicate. Feed keys
                    # get reused (the shipped GitHub one is per repo and count),
                    # so "deploy finished: all green" and "deploy ROLLED BACK,
                    # prod is down" could share a key, and the second was
                    # dropped with no record at all. This was the only path in
                    # the proactive layer that destroyed an item silently, and
                    # it could destroy an urgency 0 one.
                    # `now` is the clock in this function. Reusing the name for
                    # a string turned the next source's "now - last < period"
                    # into a TypeError that killed the whole collect, so one
                    # repeated GitHub notification, which is the normal steady
                    # state, permanently silenced git, the calendar and Sentry
                    # for the life of the process: one log line and no
                    # user-visible sign at all.
                    was = self.said.get(key)
                    words = " ".join((it.get("text") or "").split())
                    if was is not None and words and words != was:
                        if self.budget:
                            self.budget.hold(words, it.get("source", name))
                    continue
                fresh.append(it)
        if prime:
            for it in fresh:
                self.seen.add(it["key"])
            return
        if not fresh or self._hushed():
            for it in fresh:
                self.seen.add(it["key"])
                # Quiet is a silence, not a delete key. Alertmanager's rule:
                # a silence suppresses the notification, never the alert.
                if self.budget:
                    self.budget.hold(it.get("text", ""), it.get("source", ""))
            return
        fresh.sort(key=lambda it: (it.get("urgency", 1), it.get("key")))
        said, rest, batch = 0, [], []
        for it in fresh:
            self.seen.add(it["key"])
            self.said[it["key"]] = " ".join((it.get("text") or "").split())
            # Bounded with `seen`, or this becomes exactly the unbounded
            # dictionary the soak was written to catch.
            if len(self.said) > _Seen.MAX:
                for old in list(self.said)[:len(self.said) - _Seen.MAX]:
                    self.said.pop(old, None)
            # What you actually do about this kind of thing. It can only push
            # an item DOWN a tier, so the worst it does is make something wait
            # for "what did I miss" instead of interrupting. It never touches
            # urgency 0.
            from . import learn
            urgency = learn.adjust(it.get("urgency", 1),
                                   learn.key_for({"kind": it.get("source", ""),
                                                  "label": it.get("source", "")}))
            spendable = (self.budget.allow(urgency) if self.budget
                         else said < PER_ROUND)
            if said < PER_ROUND and spendable:
                batch.append(it)
                said += 1
            else:
                rest.append(it)
        if batch:
            self._say_all(batch)
        for it in rest:
            # HELD, not dropped. The line below promises a list; it has to
            # exist.
            if self.budget:
                self.budget.hold(it.get("text", ""), it.get("source", ""))
        if rest:
            urgent = [it for it in rest if it.get("urgency", 1) == 0]
            note = (f"And {len(rest)} more I haven't read out"
                    + (f", {len(urgent)} of them needing you" if urgent else "")
                    + '. Say "what did I miss" for the list.')
            self.announce(note)

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

    def _say_all(self, items: list) -> None:
        """One utterance for one moment, however many things arrived in it."""
        if len(items) == 1:
            self._say(items[0])
            return
        from .budget import compose
        lines = []
        for it in items:
            text = it.get("text", "")
            offers = it.get("offers") or []
            lines.append(text + ("  " + ", ".join(offers) if offers else ""))
        self.announce(compose(lines),
                      items=[{"sid": i.get("sid", ""),
                              "label": i.get("source", ""),
                              "kind": ("blocked" if i.get("urgency") == 0
                                       else i.get("source", ""))}
                             for i in items[:1]] or None)

    def _say(self, item: dict) -> None:
        text = item.get("text", "")
        offers = item.get("offers") or []
        if offers:
            text += "\n" + ", ".join(offers)
        self.announce(text, items=[{"sid": item.get("sid", ""),
                                    "label": item.get("source", ""),
                                    "kind": ("blocked" if item.get("urgency") == 0
                                             else item.get("source", ""))}])

    # ---- the on-demand version ------------------------------------------
    def brief(self) -> list:
        """Where everything stands right now, asked rather than pushed.

        The unprompted stream is deliberately sparse, so there has to be a way
        to ask for the whole picture without waiting for it to arrive."""
        out = []
        for name, (source, _p, _l) in self.sources.items():
            if not hasattr(source, "state"):
                continue
            try:
                line = source.state()
            except Exception as e:
                line = f"I couldn't read {name} ({e})"
            if line:
                out.append((name, line))
        return out


def _ago(hours: float) -> str:
    """Hours are unreadable past a day: "13261 hours ago" tells you nothing."""
    if hours < 48:
        return f"{int(hours)} hours ago"
    days = hours / 24
    if days < 60:
        return f"{int(days)} days ago"
    return f"{int(days / 30)} months ago"


def _sh(args: list, timeout: float = TIMEOUT) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# ------------------------------------------------------------- GitHub ----
class GitHubFeed:
    """Review requests, mentions and broken builds, from the CLI you already
    signed in to. No new token, and exactly your own permissions."""

    # What each of GitHub's reasons actually means for your day. Anything not
    # listed is background: being subscribed to a repo is not a summons.
    #
    # Only a request aimed at YOU is urgent. A team mention is a message to a
    # room, and treating it the same as "review this" is how the urgent tier
    # fills with things nobody is waiting on you for. Since sorting is
    # urgency-first, a wide urgent tier does not just add noise, it crowds out
    # the thing that genuinely needed you.
    URGENT = {"review_requested": "wants your review",
              "mention": "mentioned you",
              "assign": "assigned you"}
    WORTH_KNOWING = {"team_mention": "mentioned your team"}
    WORTH = {"ci_activity": "build", "state_change": "changed",
             "comment": "commented", "author": "on your thread"}

    def poll(self) -> list:
        raw = _sh(["gh", "api", "notifications", "--jq",
                   ".[] | {id, reason, updated_at, "
                   "title: .subject.title, type: .subject.type, "
                   "repo: .repository.full_name, url: .subject.url}"])
        out, broken, quiet = [], {}, {}
        for line in raw.splitlines():
            try:
                n = json.loads(line)
            except Exception:
                continue
            reason = n.get("reason", "")
            title = " ".join((n.get("title") or "").split())
            repo = n.get("repo", "")
            if reason in self.URGENT:
                # A person asking something of you is never grouped: each one is
                # a different thing you have to answer.
                out.append({
                    "key": f"gh:{n.get('id')}:{n.get('updated_at')}",
                    "urgency": 0,
                    "text": f"GitHub: somebody {self.URGENT[reason]} on "
                            f"{repo}: {title}",
                    "offers": ['say "open github" for the list',
                               'or "ask <session> about this"']})
            elif reason == "ci_activity" and "fail" in title.lower():
                # A workflow failing on a schedule fails over and over, so one
                # broken cron would have used the whole hourly budget by itself.
                # Group per repo and name the workflows.
                job = title.split(" workflow")[0].strip() or "a workflow"
                broken.setdefault(repo, set()).add(job)
            elif reason in self.WORTH_KNOWING:
                out.append({
                    "key": f"gh:{n.get('id')}:{n.get('updated_at')}",
                    "urgency": 1,
                    "text": f"GitHub: somebody {self.WORTH_KNOWING[reason]} in "
                            f"{repo}: {title}",
                    "offers": []})
            else:
                quiet.setdefault(repo, 0)
                quiet[repo] += 1
        for repo, jobs in broken.items():
            names = ", ".join(sorted(jobs)[:4])
            out.append({
                "key": f"gh:broken:{repo}:{len(jobs)}:{names}",
                # A broken build is worth knowing, not a summons. Nobody is
                # blocked on your answer, and it will still be broken in an
                # hour. Urgency 0 is reserved for something that cannot
                # continue without you.
                "urgency": 1,
                "text": (f"GitHub: {len(jobs)} workflow"
                         f"{'s' if len(jobs) != 1 else ''} failing in {repo} "
                         f"({names})."),
                "offers": ['say "what\'s broken" for detail',
                           'or "ask <session> to fix it"']})
        for repo, n in quiet.items():
            out.append({
                "key": f"gh:quiet:{repo}:{n}",
                "urgency": 2,
                "text": (f"GitHub: {n} update{'s' if n != 1 else ''} in {repo} "
                         f"you are subscribed to."),
                "offers": []})
        return out

    def state(self) -> str:
        raw = _sh(["gh", "api", "notifications", "--jq", ".[] | .reason"])
        reasons = [r for r in raw.split() if r]
        if not reasons:
            return "GitHub: nothing waiting."
        mine = sum(1 for r in reasons if r in self.URGENT)
        return (f"GitHub: {len(reasons)} notifications"
                + (f", {mine} of them for you personally." if mine else "."))


# ---------------------------------------------------------------- git ----
class GitFeed:
    """Work on this machine that exists nowhere else.

    Uncommitted changes and unpushed commits are the quietest way to lose a
    day's work, and nothing tells you about them because nothing is watching."""

    STALE_HOURS = 6          # dirty for this long is worth a mention
    ABANDONED_DAYS = 30      # untouched this long is an archive, not work

    def __init__(self, roots=None):
        self.roots = roots or []

    def _repos(self) -> list:
        if self.roots:
            return self.roots
        from pathlib import Path
        home = Path.home()
        found = []
        for child in sorted(home.iterdir()) if home.exists() else []:
            try:
                if child.is_dir() and (child / ".git").exists():
                    found.append(str(child))
            except OSError:
                continue
        self.roots = found[:25]
        return self.roots

    def _read(self, repo: str) -> dict:
        dirty = [l for l in _sh(["git", "-C", repo, "status",
                                 "--porcelain"]).splitlines() if l.strip()]
        ahead = [l for l in _sh(["git", "-C", repo, "log", "--oneline",
                                 "@{u}.."]).splitlines() if l.strip()]
        last = _sh(["git", "-C", repo, "log", "-1", "--format=%ct"]).strip()
        return {"dirty": len(dirty), "ahead": len(ahead),
                "last": float(last) if last.isdigit() else 0.0}

    def poll(self) -> list:
        out = []
        for repo in self._repos():
            st = self._read(repo)
            name = repo.rstrip("/").rsplit("/", 1)[-1]
            hours = (time.time() - st["last"]) / 3600 if st["last"] else 0
            # A repo nobody has touched in a month is not news, however messy.
            # Reporting an eighteen-month-old dirty tree is the kind of true,
            # useless thing that gets an assistant muted.
            if hours > self.ABANDONED_DAYS * 24:
                continue
            if st["ahead"]:
                out.append({
                    # The key includes the count, so it speaks again when the
                    # number changes and stays quiet while it does not.
                    "key": f"git:{name}:ahead:{st['ahead']}",
                    "urgency": 2,
                    "text": f"{name} has {st['ahead']} commit"
                            f"{'s' if st['ahead'] != 1 else ''} not pushed "
                            f"anywhere.",
                    "offers": []})
            if st["dirty"] and hours >= self.STALE_HOURS:
                out.append({
                    "key": f"git:{name}:dirty:{st['dirty']}:{int(hours) // 24}",
                    "urgency": 2,
                    "text": f"{name} has {st['dirty']} uncommitted change"
                            f"{'s' if st['dirty'] != 1 else ''}, and its last "
                            f"commit was {_ago(hours)}.",
                    "offers": []})
        return out

    def state(self) -> str:
        dirty, ahead = [], []
        for repo in self._repos():
            st = self._read(repo)
            name = repo.rstrip("/").rsplit("/", 1)[-1]
            hours = (time.time() - st["last"]) / 3600 if st["last"] else 0
            if hours > self.ABANDONED_DAYS * 24:
                continue
            if st["dirty"]:
                dirty.append(name)
            if st["ahead"]:
                ahead.append(f"{name} ({st['ahead']})")
        bits = []
        if dirty:
            bits.append("uncommitted work in " + ", ".join(dirty[:5]))
        if ahead:
            bits.append("unpushed commits in " + ", ".join(ahead[:5]))
        return ("Your repos: " + "; ".join(bits) + ".") if bits else \
            "Your repos are all clean and pushed."


# -------------------------------------------------------------- sentry ----
class SentryFeed:
    """Production, when it breaks.

    Urgency 0, which exempts it from the budget, and that is a deliberate and
    slightly uncomfortable choice: urgency 0 is meant for an agent that cannot
    continue without you, and everything else is rationed. A new unhandled
    exception reaching real users earns it. What makes it safe is that the
    connector's own filter is narrow enough that this cannot flood, plus the
    hard cap below: at most two exempt in a poll, and the rest wait their turn
    like everything else."""

    EXEMPT = 2

    def poll(self, remember: bool = True) -> list:
        try:
            from . import connectors
            s = connectors.get("sentry")
            if not s or not hasattr(s, "news") or not s.ready():
                return []
            rows = s.news(limit=4, remember=remember)
        except Exception:
            return []
        out = []
        for n, r in enumerate(rows):
            out.append({
                "key": f"sentry:{r['id']}",
                "source": "sentry",
                "urgency": 0 if n < self.EXEMPT else 1,
                "text": s.describe(r),
                "url": r.get("url", ""),
                # So the panel can offer the two things you actually do about a
                # production error: file it, or put an agent on it.
                "items": [{"sid": "", "label": r.get("project") or "production",
                           "kind": "sentry", "url": r.get("url", "")}],
            })
        return out


# ------------------------------------------------------------ calendar ----
class CalendarFeed:
    """The next thing you are supposed to be at.

    Through EventKit, which is the API macOS actually intends for this. The
    obvious route, AppleScript, took SEVENTY-FIVE SECONDS for one query on this
    machine, because `every event whose start date > x` makes Calendar
    materialise its entire history. EventKit answers the same question in twenty
    milliseconds.

    That slowness also caused a lie: the query timed out, the timeout was read
    as a refusal, and Friday reported "no calendar access" on a Mac that had
    granted it. Two separate failures now stay separate, because "you never let
    me" and "it did not answer" need different things from you.
    """

    LEAD_MINUTES = 15
    FETCH_EVERY = 300

    def __init__(self):
        self._events = []      # [(epoch, title)]
        self._fetched = 0.0
        self._store = None
        self._why = ""         # why it cannot read, in words

    # ---- reaching the calendar ------------------------------------------
    def _kit(self):
        """The EventKit store, asking for permission the first time."""
        if self._store is not None:
            return self._store
        try:
            import EventKit
        except ImportError:
            self._why = ("calendar support needs one package: "
                         "pip3 install --user pyobjc-framework-EventKit")
            return None
        try:
            import threading as _t
            store = EventKit.EKEventStore.alloc().init()
            done, got = _t.Event(), {}

            def _cb(granted, err):
                got["ok"] = bool(granted)
                done.set()
            # The newer call exists from macOS 14; the older one is the
            # fallback rather than an error.
            try:
                store.requestFullAccessToEventsWithCompletion_(_cb)
            except AttributeError:
                store.requestAccessToEntityType_completion_(0, _cb)
            done.wait(20)
            if not got.get("ok"):
                self._why = ("macOS has not allowed calendar access. System "
                             "Settings, Privacy & Security, Calendars.")
                return None
            self._store = store
            self._why = ""
            return store
        except Exception as e:
            self._why = f"calendar unavailable ({str(e)[:80]})"
            return None

    def available(self) -> bool:
        return self._kit() is not None

    def _fetch(self) -> None:
        if time.time() - self._fetched < self.FETCH_EVERY:
            return
        self._fetched = time.time()
        store = self._kit()
        if store is None:
            return
        try:
            import Foundation
            start = Foundation.NSDate.date()
            end = start.dateByAddingTimeInterval_(36 * 3600)
            pred = store.predicateForEventsWithStartDate_endDate_calendars_(
                start, end, None)
            rows, seen = [], set()
            for e in (store.eventsMatchingPredicate_(pred) or []):
                title = str(e.title() or "").strip()
                when = e.startDate().timeIntervalSince1970()
                # The same event lives in several calendars when they are
                # subscribed twice, and three notifications for one meeting is
                # how a feature gets muted.
                key = (title.lower(), int(when))
                if not title or key in seen:
                    continue
                seen.add(key)
                rows.append((float(when), title))
            self._events = sorted(rows)
            self._why = ""
        except Exception as e:
            # A failed query is NOT a refusal, and must not be reported as one.
            self._why = f"I couldn't read the calendar ({str(e)[:60]})"

    # ---- the feed --------------------------------------------------------
    def poll(self) -> list:
        self._fetch()
        if self._why:
            return [{"key": "cal:" + self._why[:24], "urgency": 1,
                     "text": self._why, "offers": []}]
        now = time.time()
        out = []
        for when, title in self._events:
            left = (when - now) / 60
            if 0 < left <= self.LEAD_MINUTES:
                out.append({
                    "key": f"cal:{title}:{int(when)}",
                    "urgency": 0,
                    "text": f"{title} starts in {int(left)} minute"
                            f"{'s' if int(left) != 1 else ''}.",
                    "offers": []})
        return out

    def add(self, title: str, start_epoch: float, minutes: int = 30) -> dict:
        """Put something in the calendar.

        Read-only was the right default while Friday was still learning to be
        honest, but it meant it could tell you Sam wants Thursday and do nothing
        about it, which is half a sentence. EventKit writes to the default
        calendar, which is the one a person means when they say "put it in"."""
        store = self._kit()
        if store is None:
            return {"error": self._why or "no calendar access"}
        try:
            import EventKit
            import Foundation
            ev = EventKit.EKEvent.eventWithEventStore_(store)
            ev.setTitle_(title[:200])
            start = Foundation.NSDate.dateWithTimeIntervalSince1970_(
                float(start_epoch))
            ev.setStartDate_(start)
            ev.setEndDate_(start.dateByAddingTimeInterval_(minutes * 60))
            cal = store.defaultCalendarForNewEvents()
            if cal is None:
                return {"error": "this Mac has no default calendar to write to"}
            ev.setCalendar_(cal)
            ok, err = store.saveEvent_span_error_(ev, 0, None)
            if not ok:
                return {"error": str(err)[:160] if err else "the save failed"}
            self._fetched = 0          # so the next read sees it
            return {"ok": True, "calendar": str(cal.title() or "")}
        except Exception as e:
            return {"error": str(e)[:160]}

    def state(self) -> str:
        self._fetch()
        if self._why:
            return "Calendar: " + self._why
        now = time.time()
        later = [(w, t) for w, t in self._events if w > now]
        if not later:
            return "Calendar: nothing else today."
        when, title = later[0]
        mins = int((when - now) / 60)
        soon = (f"in {mins} minutes" if mins < 90
                else _dt.datetime.fromtimestamp(when).strftime("at %H:%M"))
        return f"Calendar: next is {title} {soon} ({len(later)} coming up)."
