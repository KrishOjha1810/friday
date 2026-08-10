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

PER_ROUND = 3         # most items announced in one pass
PER_HOUR = 15         # a ceiling on how much Friday may say unprompted
TIMEOUT = 12          # seconds any one source gets to answer


class Feeds:
    def __init__(self, announce, log=None, hushed=None):
        self.announce = announce
        self._log = log or (lambda *_: None)
        self._own_hush = hushed or (lambda: False)
        self.sources = {}         # name -> (source, period, last_polled)
        self.seen = set()         # keys already announced
        self.muted = set()        # source names you asked to be spared
        self.spoken = []          # timestamps, for the hourly ceiling
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
        # Everything present at startup is history, not news.
        try:
            self._collect(prime=True)
        except Exception as e:
            self._log(f"friday feeds prime: {e}")
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ---- polling ---------------------------------------------------------
    def _loop(self) -> None:
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
                    continue
                fresh.append(it)
        if prime:
            for it in fresh:
                self.seen.add(it["key"])
            return
        if not fresh or self._hushed():
            for it in fresh:
                self.seen.add(it["key"])     # heard about, chose not to say
            return
        fresh.sort(key=lambda it: (it.get("urgency", 1), it.get("key")))
        room = min(PER_ROUND, max(0, PER_HOUR - self._said_this_hour()))
        for it in fresh[:room]:
            self.seen.add(it["key"])
            self._say(it)
        rest = fresh[room:]
        for it in rest:
            self.seen.add(it["key"])
        if rest:
            urgent = [it for it in rest if it.get("urgency", 1) == 0]
            note = (f"And {len(rest)} more I haven't read out"
                    + (f", {len(urgent)} of them needing you" if urgent else "")
                    + '. Say "what did I miss" for the list.')
            self.announce(note)

    def _said_this_hour(self) -> int:
        cut = time.time() - 3600
        self.spoken = [t for t in self.spoken if t > cut]
        return len(self.spoken)

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

    def _say(self, item: dict) -> None:
        self.spoken.append(time.time())
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
    URGENT = {"review_requested": "wants your review",
              "mention": "mentioned you",
              "assign": "assigned you",
              "team_mention": "mentioned your team"}
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
            else:
                quiet.setdefault(repo, 0)
                quiet[repo] += 1
        for repo, jobs in broken.items():
            names = ", ".join(sorted(jobs)[:4])
            out.append({
                "key": f"gh:broken:{repo}:{len(jobs)}:{names}",
                "urgency": 0,
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


# ------------------------------------------------------------ calendar ----
class CalendarFeed:
    """The next thing you are supposed to be at.

    Read from the Calendar app on this Mac, so it covers whichever accounts you
    have already added there and needs no OAuth of its own.

    Two things this has to get right. It must know the difference between "your
    day is empty" and "I was never given access", because reporting the second
    as the first is a silent failure you would plan around. And it must not
    launch Calendar every couple of minutes: one fetch of today, then the
    arithmetic is done here.
    """

    LEAD_MINUTES = 15
    FETCH_EVERY = 900          # today's events change rarely

    def __init__(self):
        self._events = []      # [(epoch, title)]
        self._fetched = 0.0
        self._access = None    # None unknown, True granted, False refused

    # ---- reading the app -------------------------------------------------
    def _ask(self, script: str, timeout: float = 25) -> tuple:
        """(output, ok). ok is False when the app refused or was not there."""
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=timeout)
            return (r.stdout or "").strip(), r.returncode == 0
        except Exception:
            return "", False

    def available(self) -> bool:
        """Whether this Mac will let Friday read the calendar at all."""
        if self._access is not None:
            return self._access
        out, ok = self._ask('tell application "Calendar" to return count of '
                            'calendars', timeout=20)
        self._access = bool(ok and out.strip().isdigit())
        return self._access

    def _fetch(self) -> None:
        if time.time() - self._fetched < self.FETCH_EVERY:
            return
        self._fetched = time.time()
        if not self.available():
            return
        out, ok = self._ask('''
tell application "Calendar"
  set n to current date
  set dayStart to n - (time of n)
  set dayEnd to dayStart + (1 * days)
  set output to ""
  repeat with c in calendars
    repeat with e in (every event of c whose start date is greater than dayStart and start date is less than dayEnd)
      set output to output & ((start date of e) as «class isot» as string) & "|" & (summary of e) & linefeed
    end repeat
  end repeat
  return output
end tell''')
        if not ok:
            self._access = False
            return
        rows = []
        for line in out.splitlines():
            iso, _, title = line.partition("|")
            try:
                when = _dt.datetime.fromisoformat(iso.strip()).timestamp()
            except Exception:
                continue
            if title.strip():
                rows.append((when, title.strip()))
        self._events = sorted(rows)

    # ---- the feed --------------------------------------------------------
    def poll(self) -> list:
        self._fetch()
        if not self.available():
            # Said exactly once, because the key never changes. Silence here
            # would look like an empty diary.
            return [{"key": "cal:no-access", "urgency": 1,
                     "text": "I can't read your calendar yet, so I won't warn "
                             "you about meetings. macOS needs to allow it: "
                             "System Settings, Privacy & Security, Automation, "
                             "and turn on Calendar for your terminal.",
                     "offers": []}]
        now = time.time()
        out = []
        for when, title in self._events:
            left = (when - now) / 60
            if 0 < left <= self.LEAD_MINUTES:
                out.append({
                    # Rounded to five minutes, so one meeting is announced once
                    # rather than every poll as the number ticks down.
                    "key": f"cal:{title}:{int(when)}",
                    "urgency": 0,
                    "text": f"{title} starts in {int(left)} minute"
                            f"{'s' if int(left) != 1 else ''}.",
                    "offers": []})
        return out

    def state(self) -> str:
        self._fetch()
        if not self.available():
            return ("Calendar: no access yet (System Settings, Privacy & "
                    "Security, Automation, allow Calendar).")
        now = time.time()
        later = [(w, t) for w, t in self._events if w > now]
        if not later:
            return "Calendar: nothing else today."
        when, title = later[0]
        mins = int((when - now) / 60)
        soon = (f"in {mins} minutes" if mins < 90
                else _dt.datetime.fromtimestamp(when).strftime("at %H:%M"))
        return (f"Calendar: {len(later)} thing"
                f"{'s' if len(later) != 1 else ''} left today, next is "
                f"{title} {soon}.")
