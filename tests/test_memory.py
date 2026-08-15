"""Finding a past session, and the length problem that decides it.

Friday used to score by counting how many distinct query words appeared. On
1,200 known-item queries built from real transcripts that scored 0.302 MRR;
BM25 with b=1.0 scores 0.380. But the number that matters is b, the length
normalisation, and the published defaults are wrong here: sessions differ in
length by a factor of several hundred, so a very long one contains nearly every
term by coincidence and has to be penalised in full.

It was also reading 5.4% of its own corpus, because a 400KB head-and-tail read
skipped the middle of every large session. Worse, it made every large session
report the SAME length, which blinds the normaliser exactly where it is needed.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import memory  # noqa: E402


def _session(name: str, turns) -> Path:
    d = memory.PROJECTS / "-Users-someone-proj"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.jsonl"
    with open(p, "w") as f:
        for role, text in turns:
            f.write(json.dumps({"type": role, "message": {"content": [
                {"type": "text", "text": text}]}}) + "\n")
    return p


def _fresh():
    memory._index.clear()
    for f in memory.PROJECTS.glob("*/*.jsonl"):
        f.unlink()


def test_the_session_that_is_actually_about_it_wins():
    _fresh()
    _session("redis", [("user", "set up redis for the session cache"),
                       ("assistant", "redis is configured and the cache works")])
    _session("other", [("user", "fix the login page"),
                       ("assistant", "the login page is fixed")])
    hits = memory.search("redis cache")
    assert hits and hits[0]["sid"] == "redis", [h["sid"] for h in hits]


def test_a_huge_session_does_not_win_by_containing_everything():
    """This is what b=1.0 buys. A long session mentions nearly every word by
    coincidence, and without full length normalisation it outranks the short
    session that is genuinely about the thing."""
    _fresh()
    _session("short", [("user", "set up redis for the cache")])
    _session("huge", [("user", "redis " + " ".join(
        f"word{i} topic{i} thing{i}" for i in range(4000)))])
    hits = memory.search("redis cache")
    assert hits[0]["sid"] == "short", \
        f"the sprawling session won: {[(h['sid'], round(h['score'], 2)) for h in hits]}"


def test_a_common_word_never_counts_against_a_session():
    """With a few dozen sessions, textbook IDF goes NEGATIVE for any term in
    more than half of them, so a common word would actively subtract."""
    _fresh()
    for i in range(6):
        _session(f"s{i}", [("user", "deploy the thing"),
                           ("assistant", "deployed")])
    _session("target", [("user", "deploy the redis thing"),
                        ("assistant", "deployed redis")])
    hits = memory.search("deploy redis")
    assert hits[0]["sid"] == "target", [h["sid"] for h in hits]
    assert all(h["score"] > 0 for h in hits), [h["score"] for h in hits]


def test_the_whole_session_is_searchable_not_just_its_ends():
    """The old read took the first and last 200KB and skipped the middle, which
    is where most of a long session lives."""
    _fresh()
    filler = " ".join(f"padding{i}" for i in range(60000))
    _session("buried", [("assistant", filler),
                        ("user", "the answer is elephantine"),
                        ("assistant", filler)])
    hits = memory.search("elephantine")
    assert hits and hits[0]["sid"] == "buried", "the middle was never read"


def test_your_own_words_count_for_more_in_a_real_session():
    """You remember your own phrasing, not the assistant's.

    Worth being precise about how little this does: the weight scales both the
    term count and the document length, so for a session that is ENTIRELY one
    voice it cancels exactly. It only separates anything in a realistic session,
    which is mostly the agent talking. Measured against a proper benchmark it is
    worth about +0.004 MRR, roughly a twentieth of what the length normalisation
    is worth. It is kept because it is free, not because it carries results."""
    _fresh()
    filler = " ".join(f"noise{i}" for i in range(200))
    _session("yours", [("user", "the webhook retry thing"),
                       ("assistant", filler)])
    _session("theirs", [("user", "have a look"),
                        ("assistant", "the webhook retry thing " + filler)])
    hits = memory.search("webhook retry")
    assert hits[0]["sid"] == "yours", [(h["sid"], round(h["score"], 3))
                                       for h in hits]


def test_quoting_it_exactly_beats_scattered_words():
    _fresh()
    _session("exact", [("user", "the login bug in checkout")])
    _session("scattered", [("user", "login"), ("assistant", "bug"),
                           ("user", "checkout"), ("assistant", "login bug")])
    hits = memory.search("the login bug in checkout")
    assert hits[0]["sid"] == "exact", [(h["sid"], h["score"]) for h in hits]
    assert hits[0]["phrase"] is True


def test_it_says_which_words_matched():
    """A relevance score nobody can check is worse than useless when Friday
    says "it was this one"."""
    _fresh()
    _session("a", [("user", "redis cache setup for checkout")])
    hits = memory.search("redis checkout")
    assert set(hits[0]["matched"]) == {"redis", "checkout"}, hits[0]["matched"]
    assert hits[0]["terms"] == 2


def test_a_second_search_does_not_reparse_anything():
    """A full pass costs about a second, so it must happen once. Transcripts
    only grow, which is why an mtime is enough."""
    _fresh()
    for i in range(4):
        _session(f"s{i}", [("user", f"session {i} about redis and caching")])
    memory.search("redis")
    reads = []
    real = memory._texts

    def counting(path, limit=0):
        reads.append(str(path))
        return real(path, limit)

    memory._texts = counting
    try:
        memory.search("caching")
        # the peek for snippets still reads, but the token counts must not
        assert len(reads) <= 4, f"reparsed {len(reads)} times for 4 sessions"
    finally:
        memory._texts = real


def test_a_changed_session_is_picked_up():
    _fresh()
    p = _session("live", [("user", "nothing here yet")])
    assert not memory.search("gravitational")
    time.sleep(0.01)
    with open(p, "a") as f:
        f.write(json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": "gravitational lensing"}]}}) + "\n")
    hits = memory.search("gravitational")
    assert hits and hits[0]["sid"] == "live", "a cached parse went stale"


def test_nothing_matching_returns_nothing():
    _fresh()
    _session("a", [("user", "redis")])
    assert memory.search("xylophone quantum") == []
    assert memory.search("ab") == []          # too short to be a query


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  memory: BM25, whole sessions, and length that actually counts")
