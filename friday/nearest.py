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

# How the scoring works, and why it is not the obvious thing.
#
# The obvious thing is difflib.SequenceMatcher, which is what this used to be.
# Measured against every mishearing this project has actually produced, it got
# 16 of 19 and produced a false match ("stop" scoring 0.73 against "Desktop",
# which offered to reopen the wrong project and had to be patched with a
# blocklist of command words).
#
# What the field settled on for names is a pair, and it beats the ratio on the
# same corpus at 18 of 19 with no false matches:
#
#   Jaro-Winkler   weights agreement at the START of a word, which is where a
#                  heard name is most likely to be right, and is far less
#                  impressed by a short word sharing letters with a long one.
#                  Implemented here, so it costs no dependency.
#   Metaphone      encodes how a word SOUNDS, so "jenral" and "general" collapse
#                  to one code. This is the only thing that catches a genuine
#                  speech error rather than a typo, and it needs `jellyfish`.
#                  Without it Friday still works and simply catches less.
try:                                          # optional, and said so out loud
    import jellyfish as _phon
except Exception:                             # pragma: no cover
    _phon = None

# Tuned on that corpus: high enough that no negative gets through, low enough
# that every real mishearing does. A winner must also be clearly ahead, because
# acting on a near-tie is a coin toss dressed up as understanding.
ACT = 0.86
GAP = 0.04
# Worth putting to you as a question, even though it is too weak to act on.
# "Munsheer" scores 0.53 against "moonshot": no basis for reading a channel,
# every basis for asking.
# Set below where a real mishearing lands ("Munsheer" is 0.70 against
# "moonshot") and above where ordinary words do ("stop" against "Desktop" is
# 0.46, "session" is 0.51). Below this, silence: offering a guess about every
# word you say is its own kind of noise.
OFFER = 0.66


def flat(s: str) -> str:
    """Letters and digits only, lowercased."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    reach = max(la, lb) // 2 - 1
    fa, fb = [False] * la, [False] * lb
    m = 0
    for i in range(la):
        for j in range(max(0, i - reach), min(lb, i + reach + 1)):
            if not fb[j] and a[i] == b[j]:
                fa[i] = fb[j] = True
                m += 1
                break
    if not m:
        return 0.0
    t = k = 0
    for i in range(la):
        if fa[i]:
            while not fb[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    return (m / la + m / lb + (m - t // 2) / m) / 3


def jaro_winkler(a: str, b: str, p: float = 0.1) -> float:
    """Jaro, with a bonus for a matching start. A misheard name usually begins
    correctly and goes wrong later, so the start is worth more."""
    j = _jaro(a, b)
    pre = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        pre += 1
    return j + pre * p * (1 - j)


def sounds_same(a: str, b: str) -> bool:
    """Whether two words are pronounced the same. False when unavailable, which
    costs accuracy and never correctness."""
    if _phon is None or not (a and b):
        return False
    try:
        return _phon.metaphone(a) == _phon.metaphone(b)
    except Exception:
        return False


def rank(heard: str, names) -> list:
    """[(score, name)] best first. Scores are similarity, 0 to 1.

    A phonetic match is treated as near-certainty rather than a bonus: if two
    words are pronounced identically and one of them is a name you have, that is
    almost always what was said."""
    q = flat(heard)
    if not q:
        return []
    out = []
    for n in names:
        if not n:
            continue
        f = flat(n)
        score = jaro_winkler(q, f)
        if sounds_same(q, f):
            score = max(score, 0.94)
        out.append((score, n))
    return sorted(out, key=lambda t: -t[0])


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
SPAN_ACT = 0.90
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
