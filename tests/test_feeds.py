"""One dispatcher for every tool, so noise control is written once.

Each of these is a way an always-on assistant becomes something you mute: the
backlog read out at startup, the same item repeated every poll, fifty
notifications delivered one at a time, one broken cron using the whole budget, a
dead repo reported as news, or an empty answer where the truth was "I was never
given access".
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import feeds  # noqa: E402

feeds.engine = type("E", (), {
    "attention": type("A", (), {"is_quiet": staticmethod(lambda: False)})})


class _Src:
    def __init__(self, items=None):
        self.items = items or []
        self.polls = 0

    def poll(self):
        self.polls += 1
        return list(self.items)

    def state(self):
        return "a source"


def _feeds(hushed=None):
    said = []
    f = feeds.Feeds(lambda text, items=None: said.append(text), hushed=hushed)
    return f, said


def _item(key, urgency=1, text=None):
    return {"key": key, "urgency": urgency, "text": text or key}


def test_the_backlog_at_startup_is_not_read_out():
    src = _Src([_item("a"), _item("b")])
    f, said = _feeds()
    f.add("s", src, period=0)
    f.start()
    time.sleep(0.1)
    assert said == [], said


def test_each_item_is_announced_once():
    src = _Src([_item("a")])
    f, said = _feeds()
    f.add("s", src, period=0)
    f._collect()
    f._collect()
    f._collect()
    assert len(said) == 1, said


def test_the_urgent_one_comes_first():
    src = _Src([_item("background", 2), _item("needs-you", 0),
                _item("worth-knowing", 1)])
    f, said = _feeds()
    f.add("s", src, period=0)
    f._collect()
    # Merged into one utterance, but the order inside it still matters.
    assert "1. needs-you" in said[0], said[0]


def test_a_flood_is_capped_and_what_was_held_back_is_admitted():
    """Fifty notifications must not become fifty announcements, and the ones
    dropped must be mentioned: a silent cap reads as "nothing happened"."""
    src = _Src([_item(f"i{n}", 0) for n in range(12)])
    f, said = _feeds()
    f.add("s", src, period=0)
    f._collect()
    # One utterance for one moment, plus the note about what was held. Three
    # arrivals used to be three separate interruptions, which costs three times
    # the attention for one moment's worth of news.
    assert len(said) == 2, said
    assert said[0].startswith(f"{feeds.PER_ROUND} things:"), said[0]
    assert "more I haven't read out" in said[-1], said[-1]
    assert "needing you" in said[-1], said[-1]


def test_the_ceiling_is_shared_and_what_it_stops_is_kept():
    """The ceiling lives in one budget now, shared with the watchtower and the
    inbox. And an item it stops is HELD: the note it prints says "say what did
    I miss for the list", and that list has to exist."""
    from friday import budget as budgets
    b = budgets.Budget(per_hour=1, burst=1)
    said = []
    f = feeds.Feeds(lambda text, items=None: said.append(text), budget=b)
    f.add("s", _Src([_item("first", 1), _item("second", 1),
                     _item("third", 1)]), period=0)
    f._collect()
    assert sum(1 for s in said if not s.startswith("And")) == 1, said
    assert any("haven't read out" in s for s in said), said
    held = [row[1] for row in b.held()]
    assert len(held) == 2, f"suppressed items were destroyed: {held}"


def test_quiet_holds_rather_than_deletes():
    """Quiet used to be a delete key: everything arriving during it was gone
    for good, while the product offered to list it."""
    from friday import budget as budgets
    b = budgets.Budget()
    said = []
    f = feeds.Feeds(lambda text, items=None: said.append(text),
                    hushed=lambda: True, budget=b)
    f.add("s", _Src([_item("while you were away", 0)]), period=0)
    f._collect()
    assert said == [], said
    assert [r[1] for r in b.held()] == ["while you were away"], b.held()


def test_quiet_covers_every_source():
    src = _Src([_item("a", 0)])
    f, said = _feeds(hushed=lambda: True)
    f.add("s", src, period=0)
    f._collect()
    assert said == [], said


def test_one_source_can_be_silenced_without_silencing_the_rest():
    noisy, quiet = _Src([_item("noisy")]), _Src([_item("useful")])
    f, said = _feeds()
    f.add("noisy", noisy, period=0)
    f.add("quiet", quiet, period=0)
    f.mute("noisy")
    f._collect()
    assert any("useful" in s for s in said), said
    assert not any("noisy" in s for s in said), said


def test_a_broken_source_does_not_take_the_others_down():
    class Dies:
        def poll(self):
            raise RuntimeError("the API is down")

    f, said = _feeds()
    f.add("dead", Dies(), period=0)
    f.add("alive", _Src([_item("still here")]), period=0)
    f._collect()
    assert any("still here" in s for s in said), said


def test_a_cron_failing_forever_is_one_line_not_forty():
    """A scheduled workflow fails on every run, so ungrouped it would spend the
    entire hourly budget on one broken thing."""
    gh = feeds.GitHubFeed()
    raw = "\n".join(
        '{"id": "%d", "reason": "ci_activity", "updated_at": "t%d", '
        '"title": "nightly workflow run failed for main branch", '
        '"repo": "me/app", "type": "CheckSuite"}' % (i, i) for i in range(20))
    feeds._sh = lambda *a, **k: raw
    items = gh.poll()
    assert len(items) == 1, [i["text"] for i in items]
    assert "nightly" in items[0]["text"], items[0]["text"]
    # Worth knowing, not a summons: nobody is blocked on your answer and it
    # will still be broken in an hour. Urgency 0 is reserved for something that
    # cannot continue without you, and a wide urgent tier crowds that out.
    assert items[0]["urgency"] == 1, items[0]["urgency"]


def test_a_person_asking_for_something_is_never_grouped_away():
    """Two people wanting two reviews are two things you have to do."""
    gh = feeds.GitHubFeed()
    feeds._sh = lambda *a, **k: "\n".join([
        '{"id":"1","reason":"review_requested","updated_at":"t",'
        '"title":"Fix the parser","repo":"me/app","type":"PullRequest"}',
        '{"id":"2","reason":"mention","updated_at":"t",'
        '"title":"Question about caching","repo":"me/api","type":"Issue"}'])
    items = gh.poll()
    assert len(items) == 2, items
    assert all(i["urgency"] == 0 for i in items), items


def test_a_repo_nobody_has_touched_in_a_year_is_not_news():
    """True and useless is what gets an assistant muted."""
    git = feeds.GitFeed(roots=["/fake/ancient"])
    git._read = lambda repo: {"dirty": 9, "ahead": 0,
                              "last": time.time() - 400 * 86400}
    assert git.poll() == []


def test_unpushed_work_is_reported_and_then_left_alone():
    git = feeds.GitFeed(roots=["/fake/live"])
    git._read = lambda repo: {"dirty": 0, "ahead": 3,
                              "last": time.time() - 3600}
    items = git.poll()
    assert len(items) == 1 and "3 commits" in items[0]["text"], items
    # the key carries the count, so the same state is not re-announced
    assert items[0]["key"] == git.poll()[0]["key"]


def test_no_calendar_access_is_never_reported_as_an_empty_day():
    """The silent version of this failure is one you would plan around."""
    cal = feeds.CalendarFeed()
    cal._kit = lambda: None
    cal._why = "macOS has not allowed calendar access."
    cal._fetched = time.time()          # skip the real fetch
    items = cal.poll()
    assert len(items) == 1, items
    assert "not allowed" in items[0]["text"], items[0]["text"]
    assert cal.poll()[0]["key"] == items[0]["key"], "would be said twice"
    assert "not allowed" in cal.state()


def test_a_failed_query_is_not_reported_as_a_refusal():
    """AppleScript took 75 seconds for one query, the timeout was read as "no
    permission", and Friday told a Mac that HAD granted access that it had
    not. "You never let me" and "it did not answer" need different things."""
    cal = feeds.CalendarFeed()
    cal._kit = lambda: object()
    cal._fetched = 0
    cal._events = []
    import friday.feeds as F
    real = F.__dict__.get("Foundation")
    cal._fetch()
    assert "not allowed" not in (cal._why or ""), cal._why
    assert "couldn't read" in (cal._why or "") or cal._why == "", cal._why


def test_one_meeting_in_three_calendars_is_one_notification():
    """Subscribed calendars duplicate holidays and shared events, and three
    buzzes for one meeting is how a feature gets muted."""
    cal = feeds.CalendarFeed()
    cal._fetched = time.time()
    when = time.time() + 300
    cal._events = sorted({(when, "Standup"), (when, "Standup"),
                          (when, "standup")})
    seen = {}
    for it in cal.poll():
        seen[it["key"]] = seen.get(it["key"], 0) + 1
    assert all(v == 1 for v in seen.values()), seen


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  feeds: once each, urgent first, capped, grouped, and honest")
