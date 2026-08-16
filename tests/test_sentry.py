"""Production, and the discipline not to talk about it constantly.

Sentry is the one source where the connector is easy and the restraint is the
whole feature. A busy Sentry produces thousands of events an hour. Something
that reports thousands of events an hour is a feed you check, and `docs/what-else`
refuses to build feeds. So most of what is tested here is what Friday stays
quiet about.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import connectors, feeds  # noqa: E402
from friday.conversation import Friday, classify  # noqa: E402


def _issue(id_, title="boom", count=9, users=3, unhandled=True, project="web"):
    return {"id": id_, "title": title, "culprit": "app/views.py in get",
            "project": {"slug": project}, "count": count, "userCount": users,
            "level": "error", "isUnhandled": unhandled,
            "permalink": f"https://sentry.io/x/{id_}/"}


def _sentry(issues, org="acme"):
    s = connectors.Sentry()
    s.token = lambda: "sntryu_" + "a" * 40
    s.ready = lambda: True
    s._get = lambda path, params=None: (
        [{"slug": org, "name": org}] if path == "/organizations/" else issues)
    return s


# ---- the words ------------------------------------------------------------
def test_the_question_is_understood():
    for said in ("what's on fire?", "sentry", "any new errors",
                 "is production ok", "are there any new exceptions"):
        assert classify(said)[0] == "fire", said


def test_a_build_question_is_not_a_production_question():
    """"any errors in the build" means CI, and answering it with production
    exceptions is confidently wrong."""
    assert classify("any errors in the build?")[0] != "fire"


def test_the_token_is_recognised_when_pasted():
    intent, p = classify("sntryu_" + "b" * 40)
    assert intent == "connect" and p["which"] == "sentry", (intent, p)


# ---- what it refuses to say -----------------------------------------------
def test_the_first_look_says_nothing():
    """Connecting Sentry to a real project surfaces months of backlog. Reading
    all of it out is the worst possible introduction to the feature."""
    s = _sentry([_issue("1"), _issue("2"), _issue("3")])
    assert s.news() == [], "announced the backlog on first connect"


def test_only_things_it_has_never_seen():
    s = _sentry([_issue("1"), _issue("2")])
    s.news()                                    # first look, learns 1 and 2
    s._get = lambda path, params=None: (
        [{"slug": "acme"}] if path == "/organizations/"
        else [_issue("1"), _issue("2"), _issue("3", title="new one")])
    got = s.news()
    assert [r["id"] for r in got] == ["3"], got


def test_an_error_that_happened_once_is_not_news():
    """The long tail of one-off failures is where an error tracker becomes
    noise, and it is mostly other people's browser extensions."""
    s = _sentry([_issue("0")])
    s.news()
    s._get = lambda path, params=None: (
        [{"slug": "acme"}] if path == "/organizations/"
        else [_issue("solo", count=1)])
    assert s.news() == []


def test_a_handled_error_is_not_news():
    """Somebody already wrote a try/except around it. It is being dealt with."""
    s = _sentry([_issue("0")])
    s.news()
    s._get = lambda path, params=None: (
        [{"slug": "acme"}] if path == "/organizations/"
        else [_issue("caught", unhandled=False)])
    assert s.news() == []


def test_the_same_error_is_not_announced_twice():
    s = _sentry([_issue("0")])
    s.news()
    later = [{"slug": "acme"}], [_issue("9")]
    s._get = lambda path, params=None: (
        later[0] if path == "/organizations/" else later[1])
    assert len(s.news()) == 1
    assert s.news() == [], "said it again"


# ---- what it does say -----------------------------------------------------
def test_asking_what_to_work_on_does_not_eat_the_news():
    """Reading the feed marks everything seen. "What should I work on?"
    consults the same feed, so asking it silently consumed the news and the
    announcement that would have interrupted you never came."""
    s = _sentry([_issue("0")])
    s.news()                                    # first look
    s._get = lambda path, params=None: (
        [{"slug": "acme"}] if path == "/organizations/"
        else [_issue("hot", title="the new one")])
    real = connectors.get
    connectors.get = lambda n: s if n == "sentry" else real(n)
    try:
        from friday.conversation import Friday
        f = Friday()
        f.announce = lambda *a, **k: None
        f.handle("what should I work on?")      # the read path
        after = feeds.SentryFeed().poll()       # the announcing path
    finally:
        connectors.get = real
    assert after and after[0]["urgency"] == 0, "the question ate the alert"


def test_it_forgets_in_order_not_at_random():
    """Trimming a set takes arbitrary members, so past the cap you forget
    issues at random and announce them again as though they were new."""
    s = _sentry([])
    s._remember([str(i) for i in range(s.MAX_SEEN + 20)])
    kept = s._seen_ordered()
    assert len(kept) == s.MAX_SEEN
    assert kept[-1] == str(s.MAX_SEEN + 19), kept[-3:]


def test_it_says_how_many_people_it_hit():
    """The count alone hides the thing that matters: 4000 events hitting one
    bot matters less than 40 hitting 40 people."""
    s = _sentry([])
    line = s.describe({"title": "TypeError: undefined is not a function",
                       "count": 40, "users": 40, "project": "web"})
    assert "40 people" in line and "web" in line, line


def test_only_two_may_jump_the_queue():
    """Urgency 0 skips the budget, and it exists for an agent that cannot
    continue without you. Production earns it; four at once does not."""
    rows = [_issue(str(i)) for i in range(4)]
    s = _sentry(rows)
    s.news()
    s._get = lambda path, params=None: (
        [{"slug": "acme"}] if path == "/organizations/"
        else [_issue(f"new{i}") for i in range(4)])
    real = connectors.get
    connectors.get = lambda n: s if n == "sentry" else real(n)
    try:
        items = feeds.SentryFeed().poll()
    finally:
        connectors.get = real
    exempt = [i for i in items if i["urgency"] == 0]
    assert len(items) >= 3, items
    assert len(exempt) == 2, f"{len(exempt)} skipped the budget"


def test_asking_shows_more_than_it_volunteers():
    """The feed only reports what is new. When you ASK, the month-old error is
    exactly what you wanted."""
    s = _sentry([_issue("1", title="old and known", count=900, users=50)])
    s.news()                                    # everything is now "seen"
    f = Friday()
    f.announce = lambda *a, **k: None
    real = connectors.get
    connectors.get = lambda n: s if n == "sentry" else real(n)
    try:
        r = f.handle("what's on fire?")
    finally:
        connectors.get = real
    assert "old and known" in r["reply"], r["reply"]
    assert "50 people" in r["reply"], r["reply"]


def test_a_missing_scope_is_reported_as_a_missing_scope():
    """403 means the token is right and the permissions are not, which is a
    different thing to fix than a wrong token."""
    import urllib.error
    s = connectors.Sentry()
    s.token = lambda: "sntryu_" + "c" * 40

    def _boom(*a, **k):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    import urllib.request
    real = urllib.request.urlopen
    urllib.request.urlopen = _boom
    try:
        got = s._get("/organizations/")
    finally:
        urllib.request.urlopen = real
    assert "scope" in got.get("error", ""), got


def test_a_quiet_production_is_said_plainly():
    s = _sentry([])
    f = Friday()
    f.announce = lambda *a, **k: None
    real = connectors.get
    connectors.get = lambda n: s if n == "sentry" else real(n)
    try:
        r = f.handle("what's on fire?")
    finally:
        connectors.get = real
    assert "quiet" in r["reply"].lower(), r["reply"]


def test_it_outranks_a_broken_build_but_not_a_blocked_agent():
    """Production is broken for people who are not you. An agent waiting on you
    is still first, because nothing else can unblock it."""
    import friday.conversation as C
    src = Path(C.__file__).read_text()
    prod = src.index('"look at production"')
    build = src.index('"fix the build"')
    assert prod < build, "the build is ranked above production"
    assert src[:prod].count("candidates.append((0,") == 1, "no blocked-agent tier"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  sentry: production first, and quiet about everything else")
