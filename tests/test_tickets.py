"""Tickets: read, file, comment, move.

The front half of the conductor idea. Reading Slack only matters if a thread can
become work, and a thread becomes work by becoming a ticket. But a ticket is
something colleagues read with your name on it, so it sits behind the same
show-me-first confirmation as sending a message.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import connectors, conversation as C  # noqa: E402
from friday.conversation import Friday, classify  # noqa: E402


class _Jira:
    name = "jira"
    """A Jira that answers from a script."""
    def __init__(self):
        self.created, self.moved, self.states = [], [], ["To Do", "In Progress",
                                                         "Done"]

    def ready(self):
        return True

    def setup_hint(self):
        return "connect it"

    def projects(self):
        return [{"key": "PROJ", "name": "Project"}]

    def my_issues(self, limit=8):
        # Required to be recognised as a tracker at all: reading is the
        # minimum, because a tracker Friday can write to and not read is not
        # something anybody has.
        return []

    def create(self, summary, project="", body="", kind="Task"):
        if not connectors.can_write() and not connectors.gh_can_write():
            return {"error": "writing_not_enabled"}
        self.created.append((project, summary))
        return {"ok": True, "key": "PROJ-9", "url": "https://x/browse/PROJ-9"}

    def transitions(self, key):
        return [{"id": str(i), "name": n} for i, n in enumerate(self.states)]

    def move(self, key, to):
        if not connectors.can_write() and not connectors.gh_can_write():
            return {"error": "writing_not_enabled"}
        from friday import nearest
        want = nearest.pick(to, self.states)
        if not want:
            return {"error": "which_state", "states": self.states}
        self.moved.append((key, want))
        return {"ok": True, "state": want}


def _only(**kinds):
    """Make these the only trackers on the machine.

    Patching `connectors.get` is not enough any more: the tracker seam asks
    `all_connectors()` which trackers can answer, so a real `gh` on the test
    machine counted as a second tracker and every ticket test started getting
    "which tracker?" instead of doing the thing."""
    from friday import trackers
    trackers.forget()
    real_all, real_get = connectors.all_connectors, connectors.get
    connectors.all_connectors = lambda: dict(kinds)
    connectors.get = lambda n: kinds.get((n or "").lower()) or real_get(n)
    return real_all, real_get


def _friday(writing=True):
    connectors.allow_write(writing)
    connectors.gh_allow_write(writing)
    jira = _Jira()
    _only(jira=jira)
    f = Friday()
    f.announce = lambda *a, **k: None
    return f, jira


def test_the_words_are_understood():
    for said, intent in (("file a ticket: the parser breaks on PDFs", "ticket"),
                         ("create an issue in PROJ: login is broken", "ticket"),
                         ("raise a bug", "ticket"),
                         ("move PROJ-12 to done", "move"),
                         ("mark ABC-3 as in progress", "move")):
        assert classify(said)[0] == intent, (said, classify(said)[0])


def test_nothing_is_filed_without_you_seeing_the_words():
    """A ticket carries your name into a tracker other people read."""
    f, jira = _friday()
    r = f.handle("file a ticket: the parser breaks on PDFs")
    assert jira.created == [], "filed it without asking"
    assert r["needs_confirm"] is True
    assert "parser breaks on PDFs" in r["reply"], r["reply"]


def test_saying_yes_files_exactly_that():
    f, jira = _friday()
    f.handle("file a ticket in PROJ: the parser breaks on PDFs")
    r = f.handle("yes")
    assert jira.created == [("PROJ", "the parser breaks on PDFs")], jira.created
    assert "PROJ-9" in r["reply"], r["reply"]


def test_with_writing_off_it_shows_what_it_would_file():
    """Refusing is not enough: showing the wording is what makes the offer to
    turn writing on a real one."""
    f, jira = _friday(writing=False)
    r = f.handle("file a ticket: the parser breaks on PDFs")
    assert jira.created == []
    assert "parser breaks on PDFs" in r["reply"], r["reply"]
    assert "let yourself post" in r["reply"], r["reply"]


def test_a_slack_thread_can_become_a_ticket_without_retyping_it():
    """This is the whole reason for reading Slack: the thread becomes work."""
    f, jira = _friday()
    f.inbox.last = {"C1": {"where": "#eng", "who": "Sam", "channel": "C1",
                           "text": "the PDF parser dies on page 3"}}
    r = f.handle("file a ticket")
    assert "PDF parser" in r["reply"], r["reply"]
    f.handle("yes")
    assert jira.created and "PDF parser" in jira.created[0][1], jira.created


def test_moving_a_ticket_is_confirmed_too():
    f, jira = _friday()
    r = f.handle("move PROJ-12 to done")
    assert jira.moved == [], "moved it without asking"
    assert r["needs_confirm"] is True
    f.handle("yes")
    assert jira.moved == [("PROJ-12", "Done")], jira.moved


def test_a_state_that_does_not_exist_lists_the_ones_that_do():
    """Jira states are per-project and per-workflow, so guessing a name is how
    you get a 400 that explains nothing."""
    f, jira = _friday()
    jira.states = ["Backlog", "Selected", "Shipped"]
    f.handle("move PROJ-12 to done")
    r = f.handle("yes")
    assert "Backlog" in r["reply"] and "Shipped" in r["reply"], r["reply"]
    assert jira.moved == [], "moved it to a state that does not exist"


def test_a_misnamed_state_still_lands():
    """"in progress" against "In Progress" is the same request."""
    f, jira = _friday()
    f.handle("move PROJ-4 to in progress")
    f.handle("yes")
    assert jira.moved == [("PROJ-4", "In Progress")], jira.moved


def test_it_asks_rather_than_choosing_between_two_trackers():
    """A ticket filed in the wrong tracker is worse than no ticket, because
    everybody believes it exists. With a work Jira and a side-project Linear
    both connected, the old hard-coded order silently sent side-project work to
    the employer's board."""
    from friday import trackers
    connectors.allow_write(True)
    jira, linear = _Jira(), _Jira()
    jira.name, linear.name = "jira", "linear"
    _only(jira=jira, linear=linear)
    f = Friday()
    f.announce = lambda *a, **k: None
    r = f.handle("file a ticket: the parser breaks")
    assert "more than one tracker" in r["reply"], r["reply"]
    assert jira.created == [] and linear.created == [], "picked one anyway"


def test_naming_the_tracker_settles_it():
    from friday import trackers
    connectors.allow_write(True)
    jira, linear = _Jira(), _Jira()
    jira.name, linear.name = "jira", "linear"
    _only(jira=jira, linear=linear)
    f = Friday()
    f.announce = lambda *a, **k: None
    f.handle("file a linear ticket: the parser breaks")
    f.handle("yes")
    assert linear.created and not jira.created, (linear.created, jira.created)


def test_saying_where_tickets_go_is_remembered():
    """Being asked the same question every morning is its own kind of
    broken."""
    from friday import trackers
    connectors.allow_write(True)
    jira, linear = _Jira(), _Jira()
    jira.name, linear.name = "jira", "linear"
    _only(jira=jira, linear=linear)
    f = Friday()
    f.announce = lambda *a, **k: None
    r = f.handle("use linear for tickets")
    assert "Linear" in r["reply"], r["reply"]
    f.handle("file a ticket: the parser breaks")
    f.handle("yes")
    assert linear.created and not jira.created, (linear.created, jira.created)
    trackers.forget()


def test_the_tracker_you_approved_is_the_one_it_files_in():
    """Re-resolving at confirm time could file it somewhere you never agreed
    to, which is exactly what the confirmation exists to prevent."""
    from friday import trackers
    connectors.allow_write(True)
    jira, linear = _Jira(), _Jira()
    jira.name, linear.name = "jira", "linear"
    _only(jira=jira, linear=linear)
    f = Friday()
    f.announce = lambda *a, **k: None
    f.handle("file a jira ticket: the parser breaks")
    trackers.prefer("linear")          # changes underfoot before you say yes
    try:
        f.handle("yes")
    finally:
        trackers.forget()
    assert jira.created and not linear.created, (jira.created, linear.created)


def test_reading_shows_every_tracker_at_once():
    """Writing has to pick one, because a ticket goes somewhere. Reading has no
    such constraint, and being the single place is the whole promise."""
    jira, linear = _Jira(), _Jira()
    jira.name, linear.name = "jira", "linear"
    jira.my_issues = lambda n=8: [{"key": "PROJ-1", "summary": "work thing",
                                   "status": "Open"}]
    linear.my_issues = lambda n=8: [{"key": "SID-2", "summary": "side thing",
                                     "status": "Todo"}]
    _only(jira=jira, linear=linear)
    f = Friday()
    f.announce = lambda *a, **k: None
    reply = f.handle("what are my tickets?")["reply"]
    assert "PROJ-1" in reply and "SID-2" in reply, reply


def test_a_broken_tracker_is_not_reported_as_an_empty_one():
    """Those look identical and only one of them means you have no work."""
    jira, linear = _Jira(), _Jira()
    jira.name, linear.name = "jira", "linear"
    jira.my_issues = lambda n=8: [{"error": "token expired"}]
    linear.my_issues = lambda n=8: [{"key": "SID-2", "summary": "side thing",
                                     "status": "Todo"}]
    _only(jira=jira, linear=linear)
    f = Friday()
    f.announce = lambda *a, **k: None
    reply = f.handle("what are my tickets?")["reply"]
    assert "couldn't reach" in reply and "token expired" in reply, reply


def test_a_ticket_goes_wherever_you_actually_track_things():
    """Jira and Linear answer the same questions, so nothing above the
    connector should know which one it got. Plenty of teams replaced Jira with
    Linear entirely."""
    connectors.allow_write(True)
    linear = _Jira()          # same shape, different product
    linear.name = "linear"
    _only(linear=linear)
    try:
        f = Friday()
        f.announce = lambda *a, **k: None
        f.handle("file a ticket: the parser dies")
        f.handle("yes")
        assert linear.created, "it never reached the tracker that was connected"
    finally:
        connectors.allow_write(False)


def test_no_tracker_connected_says_so_rather_than_failing_oddly():
    connectors.allow_write(True)
    # Every tracker off, including the GitHub fallback, which is genuinely
    # connected on a machine where `gh` is signed in.
    _only()
    try:
        f = Friday()
        f.announce = lambda *a, **k: None
        r = f.handle("file a ticket: something")
        assert "no ticket tracker is connected" in r["reply"].lower(), r["reply"]
    finally:
        connectors.allow_write(False)
        connectors.gh_allow_write(False)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    connectors.allow_write(False)
    connectors.gh_allow_write(False)
    print("ok  tickets: filed, moved, and never without you reading it first")


def test_github_is_the_tracker_that_needs_no_setup():
    """`gh` is already signed in, so this works on a fresh machine with nothing
    connected. Making somebody connect Jira to file a note about their own repo
    is ceremony for nothing."""
    connectors.allow_write(True)
    connectors.gh_allow_write(True)
    real = connectors.get

    class NotConnected(_Jira):
        def ready(self):
            return False

    gh = _Jira()
    gh.name = "github"
    connectors.get = lambda n: (gh if n == "github" else NotConnected())
    try:
        f = Friday()
        f.announce = lambda *a, **k: None
        assert f._tracker() is gh, "fell through to nothing"
        f.handle("file a ticket: the parser dies")
        f.handle("yes")
        assert gh.created, "the issue never got filed"
    finally:
        connectors.get = real
        connectors.allow_write(False)
        connectors.gh_allow_write(False)


def test_a_real_tracker_beats_the_fallback():
    """If Jira or Linear is connected, that is where colleagues will look, so
    GitHub issues must not quietly win."""
    connectors.allow_write(True)
    connectors.gh_allow_write(True)
    real = connectors.get
    jira, gh = _Jira(), _Jira()
    gh.name = "github"
    connectors.get = lambda n: (jira if n == "jira" else
                                gh if n == "github" else real(n))
    try:
        f = Friday()
        f.announce = lambda *a, **k: None
        assert f._tracker() is jira, "the fallback beat the real tracker"
    finally:
        connectors.get = real
        connectors.allow_write(False)
        connectors.gh_allow_write(False)
