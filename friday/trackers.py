"""Every place a developer's work is written down, behind one seam.

Friday is not a personal tool with one person's stack baked in, so no tracker
gets to be THE tracker. The selection used to be a hard-coded preference order,
`jira` then `linear` then `github`, which quietly decided for everybody: if you
had a work Jira and a side-project Linear, your side-project ticket went to
work. That is not a preference, that is a bug with an opinion.

So this file does three things and nothing else:

    available()   which trackers can actually answer right now
    preferred()   which one you said to use, if you said
    get()         the one to use, or None when it genuinely cannot be decided

The verbs come from the connectors themselves, which is deliberate: this is a
seam, not a layer. Jira defines the full set because it has the richest model,
and anything missing on another tracker is filled in here rather than being
faked there.

The long tail is real and it is not solved by writing more classes. There are
dozens of trackers and somebody uses each of them. The answer is that any
tracker with an MCP server already plugs in through `connectors.mcp_servers()`,
and `is_tracker()` below recognises one by the verbs it answers rather than by
its name, so a tracker Friday has never heard of works the moment you add it.
"""

from . import connectors

# The verbs something has to answer to be a tracker at all. Reading is the
# minimum: a tracker Friday can read and not write is still worth having, and
# refusing it because it cannot create would lose read-only Jira, which is a
# real and common setup.
MUST_READ = ("my_issues",)
CAN_WRITE = ("create", "comment", "move")

# Built-ins, in the order they are offered when you are asked to choose. Not a
# priority order: nothing picks from this list without either your preference or
# your answer.
BUILT_IN = ("jira", "linear", "github", "gitlab")

PREF_FILE = "tracker"


def is_tracker(c) -> bool:
    """Whether this connector is somewhere work is written down.

    By the verbs it answers, not by its name. That is what lets an MCP server
    for a tracker nobody here has heard of behave like a first-class one."""
    return bool(c) and all(callable(getattr(c, v, None)) for v in MUST_READ)


def writable(c) -> bool:
    return bool(c) and any(callable(getattr(c, v, None)) for v in CAN_WRITE)


def _ready(c) -> bool:
    try:
        return bool(c.ready())
    except Exception:
        return False


def available() -> list:
    """Every tracker that can answer, built-in or MCP, in offer order."""
    found, seen = [], set()
    everything = connectors.all_connectors()
    for name in BUILT_IN:
        c = everything.get(name)
        if is_tracker(c) and _ready(c):
            found.append(c)
            seen.add(name)
    for name, c in sorted(everything.items()):
        if name not in seen and is_tracker(c) and _ready(c):
            found.append(c)
    return found


def names() -> list:
    return [getattr(c, "name", "?") for c in available()]


# ---------------------------------------------------------- your choice ----
def preferred() -> str:
    return (connectors._secret(PREF_FILE) or "").strip().lower()


def prefer(name: str) -> bool:
    """Remember where your tickets go. Survives restarts, because being asked
    the same question every morning is its own kind of broken."""
    return connectors.save_secret(PREF_FILE, (name or "").strip().lower())


def forget() -> None:
    connectors.save_secret(PREF_FILE, "")


def get(want: str = ""):
    """The tracker to use, or None.

    `want` is a name said out loud ("file it in linear"), which always wins:
    naming one is a decision, and a decision beats a saved preference.

    With no name and no preference, one available tracker is used and several
    is None. Returning None looks unhelpful and is the point: a ticket filed in
    the wrong tracker is a ticket nobody reads, and it is worse than no ticket
    because everybody believes it exists."""
    live = available()
    if want:
        want = want.strip().lower()
        for c in live:
            if getattr(c, "name", "") == want:
                return c
        return None
    saved = preferred()
    if saved:
        for c in live:
            if getattr(c, "name", "") == saved:
                return c
        # A preference for something no longer connected is stale, not binding.
    if len(live) == 1:
        return live[0]
    return None


def ambiguous() -> bool:
    """Whether Friday would have to guess. True means ask."""
    return not preferred() and len(available()) > 1


# ------------------------------------------------------- filling the gaps ----
def can(c, verb: str) -> bool:
    return callable(getattr(c, verb, None))


def describe(c) -> str:
    """What to call it when speaking. `name` is a slug; some of these have a
    nicer form and none of them should be read out as a slug."""
    name = getattr(c, "name", "")
    return {"github": "GitHub Issues", "gitlab": "GitLab",
            "jira": "Jira", "linear": "Linear"}.get(name, name or "your tracker")
