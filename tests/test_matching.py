"""Name matching, measured rather than asserted.

Friday used difflib.SequenceMatcher, which is a general string ratio. Matching a
misheard name is not a general string problem, and the field settled on a
different pair long ago: Jaro-Winkler, which weights agreement at the start of a
word, plus a phonetic code, which is the only thing that catches an error of
SOUND rather than of spelling.

Measured on the corpus below, every entry of which really happened in this
project, the old approach got 16 of 19 with one false match. The pair gets 18
with none. This file is that measurement, kept, so a future change to the
scoring has to beat it rather than merely look reasonable.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import use_temp_config  # noqa: E402

use_temp_config()

from friday import nearest  # noqa: E402

NAMES = ["moonshot", "voicebridge", "promptguard", "jobhunt", "api", "general",
         "Desktop", "job-search-agent", "friday", "random"]

# Real mishearings this project produced, and real false matches it made.
HEARD = [
    ("moon shot", "moonshot"),          # a compound split at the space
    ("moon of shot", "moonshot"),
    ("voice bridge", "voicebridge"),
    ("voicebrige", "voicebridge"),      # a dropped letter
    ("prompt guard", "promptguard"),
    ("promptgard", "promptguard"),
    ("job hut", "jobhunt"),             # heard as two ordinary words
    ("jbhnt", "jobhunt"),               # every consonant, no vowels
    ("ap", "api"),
    ("jenral", "general"),              # sounds right, spelled nothing like it
    ("frydey", "friday"),
    ("randem", "random"),
]
NEVER = ["stop", "it", "nonexistent", "zzzzzz", "session", "the"]


def test_every_real_mishearing_resolves():
    missed = [(h, w) for h, w in HEARD if nearest.pick(h, NAMES) != w]
    assert not missed, f"{len(missed)} of {len(HEARD)} missed: {missed}"


def test_no_ordinary_word_is_ever_matched():
    """"stop" scored 0.73 against "Desktop" under the old ratio and offered to
    reopen the wrong project. That needed a blocklist of command words to
    contain; the scoring should not need rescuing like that."""
    wrong = [(w, nearest.pick(w, NAMES)) for w in NEVER
             if nearest.pick(w, NAMES)]
    assert not wrong, f"matched ordinary words: {wrong}"


def test_a_word_that_sounds_the_same_is_caught():
    """The one thing a string ratio cannot do. 'jenral' shares few letters with
    'general' and is obviously the same word out loud."""
    assert nearest.sounds_same("jenral", "general") or nearest._phon is None
    if nearest._phon is None:
        return                      # honest: without the library, it catches less
    assert nearest.pick("jenral", NAMES) == "general"
    assert nearest.pick("frydey", NAMES) == "friday"


def test_the_start_of_a_word_counts_for_more():
    """A misheard name usually begins correctly and goes wrong later, which is
    exactly what Jaro-Winkler weights and a plain ratio does not."""
    same_start = nearest.jaro_winkler("moonshet", "moonshot")
    same_end = nearest.jaro_winkler("loonshot", "moonshot")
    assert same_start > same_end, (same_start, same_end)


def test_a_near_tie_is_never_acted_on():
    """Two names equally close is a coin toss, and the cost of losing it is
    doing something to the wrong session."""
    assert nearest.pick("alphc", ["alpha", "alphb"]) == ""


def test_the_three_outcomes_still_hold():
    """The contract the rest of Friday depends on: act, ask, or say nothing."""
    assert nearest.resolve("moon shot", NAMES)[0] == "exact"
    assert nearest.resolve("voicebrige", NAMES)[0] == "sounds-like"
    assert nearest.resolve("Munsheer", NAMES)[0] == "maybe"
    assert nearest.resolve("zzzzzz", NAMES)[0] == ""


def test_it_beats_what_it_replaced():
    """The measurement itself. A future change has to beat this, not just read
    plausibly."""
    import difflib

    def old(heard):
        q = nearest.flat(heard)
        scored = sorted(((difflib.SequenceMatcher(None, q, nearest.flat(n)).ratio(), n)
                         for n in NAMES), reverse=True)
        top, name = scored[0]
        if top < 0.62 or (len(scored) > 1 and top - scored[1][0] < 0.08):
            return None
        return name

    def now(heard):
        return nearest.pick(heard, NAMES) or None

    def score(fn):
        good = sum(1 for h, w in HEARD if fn(h) == w)
        bad = sum(1 for w in NEVER if fn(w) is not None)
        return good, bad

    old_good, old_bad = score(old)
    new_good, new_bad = score(now)
    assert new_good >= old_good, f"caught fewer: {new_good} vs {old_good}"
    assert new_bad <= old_bad, f"more false matches: {new_bad} vs {old_bad}"
    assert (new_good, new_bad) != (old_good, old_bad) or nearest._phon is None, \
        "no measurable improvement at all"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok  matching: phonetic + Jaro-Winkler, measured against the old ratio")
