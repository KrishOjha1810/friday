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


def _friday(writing=True):
    connectors.allow_write(writing)
    connectors.gh_allow_write(writing)
    jira = _Jira()
    real = connectors.get
    connectors.get = lambda n: jira if n == "jira" else real(n)
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


def test_a_ticket_goes_wherever_you_actually_track_things():
    """Jira and Linear answer the same questions, so nothing above the
    connector should know which one it got. Plenty of teams replaced Jira with
    Linear entirely."""
    connectors.allow_write(True)
    real = connectors.get
    linear = _Jira()          # same shape, different product

    def only_linear(n):
        if n == "linear":
            return linear
        if n == "jira":
            class Off(_Jira):
                def ready(self):
                    return False
            return Off()
        return real(n)

    connectors.get = only_linear
    try:
        f = Friday()
        f.announce = lambda *a, **k: None
        f.handle("file a ticket: the parser dies")
        f.handle("yes")
        assert linear.created, "it never reached the tracker that was connected"
    finally:
        connectors.get = real
        connectors.allow_write(False)


def test_no_tracker_connected_says_so_rather_than_failing_oddly():
    connectors.allow_write(True)
    real = connectors.get

    class Off(_Jira):
        def ready(self):
            return False

    connectors.get = lambda n: Off() if n == "jira" else real(n)
    try:
        f = Friday()
        f.announce = lambda *a, **k: None
        r = f.handle("file a ticket: something")
        assert "no ticket tracker is connected" in r["reply"].lower(), r["reply"]
    finally:
        connectors.get = real
        connectors.allow_write(False)
        connectors.gh_allow_write(False)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    connectors.allow_write(False)
    connectors.gh_allow_write(False)
    print("ok  tickets: filed, moved, and never without you reading it first")
