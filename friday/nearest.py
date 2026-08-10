"""Match what someone said to the names that actually exist.

Every name a person says to Friday is a proper noun, and proper nouns are
exactly what speech recognition gets wrong: a Slack channel called moonshot
arrives as "Munsheer", "moon shot" or "moon of shot", and a session
called krishojha-7f arrives however whisper feels about hyphens that day.

Friday always knows the real list. So a name it does not recognise is never a
reason to stop, and it is never a reason to guess either. There are three honest
outcomes, and this module is here so that every part of Friday reaches the same
one for the same input:

    a clear winner   ->  act on it
    a plausible one  ->  "did you mean X?"
    nothing close    ->  say what does exist

The comparison strips everything but letters and digits before measuring,
because a heard compound name is split into words at the wrong place, and
comparing word by word compares the wrong things.
"""

import difflib

# A winner has to be close, and it has to be clearly ahead. Acting on a
# 0.62-against-0.61 pair is a coin toss dressed up as understanding, and doing
# the wrong thing to the wrong session is the failure that matters here.
ACT = 0.62
GAP = 0.08
# Worth putting to you as a question, even though it is too weak to act on.
# "Munsheer" scores 0.53 against "moonshot": no basis for reading a channel,
# every basis for asking.
OFFER = 0.34


def flat(s: str) -> str:
    """Letters and digits only, lowercased."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def rank(heard: str, names) -> list:
    """[(score, name)] best first. Scores are similarity, 0 to 1."""
    q = flat(heard)
    if not q:
        return []
    return sorted(((difflib.SequenceMatcher(None, q, flat(n)).ratio(), n)
                   for n in names if n), key=lambda t: -t[0])


def pick(heard: str, names, act: float = ACT, gap: float = GAP) -> str:
    """The one name clearly meant, or "" if that cannot be said honestly."""
    scored = rank(heard, names)
    if not scored or scored[0][0] < act:
        return ""
    if len(scored) > 1 and scored[0][0] - scored[1][0] < gap:
        return ""                      # too close to call: ask instead
    return scored[0][1]


def suggest(heard: str, names, floor: float = OFFER) -> str:
    """The closest name worth offering as a question, or ""."""
    scored = rank(heard, names)
    if scored and scored[0][0] >= floor:
        return scored[0][1]
    return ""


def resolve(heard: str, names) -> tuple:
    """('exact'|'sounds-like'|'maybe'|'', name).

    One call, so no caller has to reimplement the thresholds and quietly get
    them slightly different from every other caller."""
    q = flat(heard)
    for n in names:
        if flat(n) == q:
            return "exact", n
    got = pick(heard, names)
    if got:
        return "sounds-like", got
    got = suggest(heard, names)
    if got:
        return "maybe", got
    return "", ""


def best_window(text: str, names, act: float = ACT, words: int = 3) -> str:
    """The name only. See best_span for where in the sentence it was found."""
    name, _, _, _ = best_span(text, names, act, words)
    return name


# Pulling a name OUT of a sentence is a weaker signal than being handed one, so
# it needs a higher bar and a blocklist. "stop" scores 0.73 against "Desktop",
# which is how "tell nonexistent to stop" became an offer to reopen Desktop.
SPAN_ACT = 0.72
_NEVER_A_NAME = {
    "stop", "open", "tell", "ask", "send", "say", "more", "quiet", "resume",
    "the", "that", "this", "them", "and", "for", "with", "what", "who", "when",
    "about", "session", "sessions", "claude", "friday", "please", "also",
    "chat", "message", "reply", "read", "here", "there", "some", "test",
}


def best_span(text: str, names, act: float = None, words: int = 3) -> tuple:
    """(name, first_word_index, last_word_index_exclusive, score).

    Knowing WHERE the name sat matters as much as knowing it is there: the rest
    of the sentence is the message, and splitting it in the wrong place sent an
    agent the literal words "bridge session in claude for a summary of changes"."""
    act = SPAN_ACT if act is None else act
    toks = [w.strip(".,?!:;#") for w in (text or "").split()]
    toks = [w for w in toks if w]
    best, score, span = "", 0.0, (0, 0)
    for n in range(words, 0, -1):
        for i in range(len(toks) - n + 1):
            chunk = toks[i:i + n]
            # A window made only of ordinary command words is not a name,
            # however well it happens to score against one.
            if all(w.lower() in _NEVER_A_NAME for w in chunk):
                continue
            window = " ".join(chunk)
            for sc, name in rank(window, names)[:1]:
                # Short windows are only trusted when they match exactly: "api"
                # is a real session name, "sto" is a fragment of a word.
                if len(flat(window)) < 4 and flat(window) != flat(name):
                    continue
                if sc > score + 1e-9:
                    best, score, span = name, sc, (i, i + n)
    if score < act:
        return "", 0, 0, score
    return best, span[0], span[1], score
