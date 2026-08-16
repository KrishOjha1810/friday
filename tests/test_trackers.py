"""No tracker gets to be THE tracker.

Friday is not a personal tool with one person's stack baked in. The selection
used to be a hard-coded order, jira then linear then github, which quietly
decided for everybody: with a work Jira and a side-project Linear connected,
the side-project ticket went to the employer's board. That is not a preference,
it is a bug with an opinion.

The claim being tested is that a tracker is recognised by the verbs it answers
rather than by its name, because there are dozens of trackers and somebody uses
each of them. If that holds, one Friday has never heard of works the moment it
is connected.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

import json  # noqa: E402

from friday import connectors, trackers  # noqa: E402


class _Any:
    """A tracker nobody wrote a class for: an MCP server, or the next Linear."""

    name = "shortcut"

    def ready(self):
        return True

    def setup_hint(self):
        return "connect it"

    def my_issues(self, limit=8):
        return [{"key": "sc-7", "summary": "a story", "status": "Ready"}]

    def create(self, summary, project="", body="", kind=""):
        return {"ok": True, "key": "sc-8"}


class _ReadOnly:
    """Read-only Jira, which is a real and common setup."""

    name = "readonly"

    def ready(self):
        return True

    def setup_hint(self):
        return ""

    def my_issues(self, limit=8):
        return []


class _NotATracker:
    name = "spotify"

    def ready(self):
        return True

    def setup_hint(self):
        return ""

    def play(self, what):
        return True


def _only(*cs):
    trackers.forget()
    connectors.all_connectors = lambda: {c.name: c for c in cs}


# ---- what counts as a tracker ---------------------------------------------
def test_it_is_recognised_by_its_verbs_not_its_name():
    """The whole point. There are dozens of trackers and somebody uses each of
    them; a list of names would be out of date the day it was written."""
    assert trackers.is_tracker(_Any())
    assert not trackers.is_tracker(_NotATracker())


def test_read_only_still_counts():
    """A tracker Friday can read and not write is worth having, and refusing it
    would lose read-only Jira, which is a real and common setup."""
    assert trackers.is_tracker(_ReadOnly())
    assert not trackers.writable(_ReadOnly())


def test_a_tracker_it_has_never_heard_of_is_used_like_any_other():
    _only(_Any())
    assert trackers.names() == ["shortcut"]
    got = trackers.get()
    assert got and got.my_issues()[0]["key"] == "sc-7"


def test_the_built_in_ones_are_offered_in_a_stable_order():
    """Not a priority order. Nothing picks from it without your say-so; it only
    decides what a list reads like when you ARE asked."""
    a, b = _Any(), _Any()
    a.name, b.name = "github", "jira"
    _only(a, b)
    assert trackers.names() == ["jira", "github"]


# ---- who decides ----------------------------------------------------------
def test_one_tracker_needs_no_decision():
    _only(_Any())
    assert not trackers.ambiguous()
    assert trackers.get() is not None


def test_two_trackers_and_no_preference_is_a_question_not_a_guess():
    a, b = _Any(), _Any()
    a.name, b.name = "jira", "linear"
    _only(a, b)
    assert trackers.ambiguous()
    assert trackers.get() is None, "guessed between two trackers"


def test_a_saved_preference_settles_it():
    a, b = _Any(), _Any()
    a.name, b.name = "jira", "linear"
    _only(a, b)
    trackers.prefer("linear")
    try:
        assert not trackers.ambiguous()
        assert trackers.get().name == "linear"
    finally:
        trackers.forget()


def test_naming_one_beats_the_saved_preference():
    """Saying it out loud is a decision, and a decision beats a setting."""
    a, b = _Any(), _Any()
    a.name, b.name = "jira", "linear"
    _only(a, b)
    trackers.prefer("linear")
    try:
        assert trackers.get("jira").name == "jira"
    finally:
        trackers.forget()


def test_a_preference_for_something_disconnected_is_stale_not_binding():
    """Otherwise removing a tracker leaves Friday pointing at nothing, with no
    way to tell that is what happened."""
    a = _Any()
    a.name = "jira"
    _only(a)
    trackers.prefer("linear")           # no longer connected
    try:
        assert trackers.get().name == "jira"
    finally:
        trackers.forget()


def test_naming_one_that_is_not_connected_returns_nothing():
    """Rather than falling back to a different tracker, which would file your
    ticket somewhere you did not name."""
    _only(_Any())
    assert trackers.get("jira") is None


# ---- the real four --------------------------------------------------------
def test_every_built_in_tracker_answers_the_same_questions():
    """The seam is only real if the connectors actually line up behind it.
    Three could share a shape by coincidence; the fourth is the proof."""
    want = ("my_issues", "create", "comment", "move", "transitions", "projects")
    for name in ("jira", "linear", "github", "gitlab"):
        c = connectors.REGISTRY[name]
        missing = [v for v in want if not trackers.can(c, v)]
        assert not missing, f"{name} is missing {missing}"


def test_they_are_named_the_way_a_person_would_say_them():
    assert trackers.describe(connectors.REGISTRY["github"]) == "GitHub Issues"
    assert trackers.describe(connectors.REGISTRY["gitlab"]) == "GitLab"


def test_github_issues_come_back_in_the_shared_shape():
    """It returned raw `gh` JSON while everything above read key, summary and
    status, so the tickets rendered as a list of empty brackets: present,
    counted, and unreadable."""
    gh = connectors.REGISTRY["github"]
    if not gh.ready():
        return
    rows = gh.my_issues(2)
    for r in rows:
        assert set(("key", "summary", "status")) <= set(r), r
        assert r["key"] and r["summary"], r


def test_writing_is_off_until_you_say_otherwise_on_every_tracker():
    """The gate is per tracker, and a new one added later must not arrive with
    it already open."""
    connectors.allow_write(False)
    connectors.gh_allow_write(False)
    for name in ("jira", "linear", "github", "gitlab"):
        c = connectors.REGISTRY[name]
        assert c.create("x")["error"] == "writing_not_enabled", name
        assert c.move("K-1", "done")["error"] == "writing_not_enabled", name


def test_a_state_nobody_has_is_refused_rather_than_guessed():
    """GitHub and GitLab have two states, so there is no room to be clever, and
    writing a wrong one onto somebody's tracker is not recoverable by saying
    sorry."""
    connectors.allow_write(True)
    connectors.gh_allow_write(True)
    try:
        for name in ("github", "gitlab"):
            r = connectors.REGISTRY[name].move("owner/repo#1", "banana")
            assert r.get("error") == "which_state", (name, r)
    finally:
        connectors.allow_write(False)
        connectors.gh_allow_write(False)


# ---- a tracker nobody wrote code for --------------------------------------
class _MCP(connectors.MCPConnector):
    """An MCP server, with its tool list and answer under our control."""

    def __init__(self, tools, answer=None):
        super().__init__("someserver")
        self._tools = tools
        self._answer = answer or {}
        self.called = []

    def ready(self):
        return True

    def tools(self):
        return self._tools

    def call(self, tool, **args):
        self.called.append((tool, args))
        return self._answer


def _payload(rows):
    return {"content": [{"type": "text", "text": json.dumps(rows)}]}


def test_an_mcp_server_that_lists_issues_is_a_tracker():
    """The claim that any tracker with an MCP server plugs in unwritten was
    false when it was first written: the wrapper had no read verb at all, so
    no MCP server was ever recognised as a tracker."""
    c = _MCP([{"name": "list_issues", "inputSchema": {}}],
             _payload([{"identifier": "ENG-4", "title": "Fix the parser",
                        "state": {"name": "In Progress"}}]))
    assert trackers.is_tracker(c)
    got = c.my_issues()
    assert got == [{"key": "ENG-4", "summary": "Fix the parser",
                    "status": "In Progress", "url": ""}], got


def test_it_will_not_call_a_tool_that_writes():
    """Calling a tool nobody wrote code for is only safe because the names it
    matches cannot create or change anything."""
    c = _MCP([{"name": "create_issue", "inputSchema": {}},
              {"name": "update_issue", "inputSchema": {}},
              {"name": "delete_ticket", "inputSchema": {}},
              {"name": "issues_create", "inputSchema": {}}])
    assert c._issue_tool() == {}, c._issue_tool()
    assert c.my_issues() == []
    assert c.called == [], c.called


def test_it_will_not_guess_required_arguments():
    """Filling in a required field by guessing means asking the wrong question
    and being told an answer, which is worse than asking nothing."""
    c = _MCP([{"name": "search_issues",
               "inputSchema": {"required": ["jql"]}}],
             _payload([{"title": "something"}]))
    assert c.my_issues() == []
    assert c.called == [], "called it anyway"


def test_it_prefers_the_tool_that_needs_nothing():
    c = _MCP([{"name": "search_tickets", "inputSchema": {"required": ["q"]}},
              {"name": "list_my_tasks", "inputSchema": {}}],
             _payload([{"title": "a task"}]))
    assert c._issue_tool()["name"] == "list_my_tasks"


def test_an_answer_it_cannot_read_is_nothing_not_noise():
    """A list of stringified JSON read out loud is worse than being told there
    is nothing."""
    c = _MCP([{"name": "list_issues", "inputSchema": {}}],
             _payload([{"nothing": "ticket shaped"}, "just a string", 7]))
    assert c.my_issues() == []


def test_it_reads_the_shapes_servers_actually_use():
    """Every layer here is somewhere servers differ: the wrapper, whether the
    payload is a list or a dict with the list inside, and what the fields are
    called."""
    rows = [{"key": "ENG-5", "fields": {"summary": "Ship it",
                                        "status": {"name": "Todo"}}}]
    for wrapped in ({"structuredContent": rows},
                    _payload(rows),
                    {"structuredContent": {"issues": rows}},
                    {"structuredContent": {"nodes": rows}}):
        got = connectors._as_issues(wrapped)
        assert got and got[0]["summary"] == "Ship it", (wrapped, got)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    trackers.forget()
    print("ok  trackers: yours, not whichever one Friday preferred")
