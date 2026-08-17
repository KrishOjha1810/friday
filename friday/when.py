"""Turn "yesterday", "on Friday", "this week" into a real range of time.

Asked what happened yesterday, Friday read the last fifteen messages whatever
their dates and then answered as though they were yesterday's. That is the worst
kind of wrong: the shape of the answer says the question was understood.

Everything here returns (oldest, latest, label) in local time, where the label
is what Friday will SAY it read, so the window and the claim can never disagree.
"""

import datetime as _dt
import re

# Every spelling anyone uses, to the weekday it means.
_DAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
         "friday": 4, "saturday": 5, "sunday": 6,
         "mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2,
         "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
# Spellings that mean a day but not a WEEKDAY, kept apart from the lookup above
# so that adding one cannot crash the parser. "tmrw" was listed as a day word
# and not as a weekday, so it fell through to `_DAYS[word]` and raised a
# KeyError straight out of an unguarded caller: the request thread died and the
# page got no reply at all, for a common spelling of "tomorrow".
_RELATIVE = {"today": 0, "tonight": 0, "tomorrow": 1, "tmrw": 1}
# Which spellings are abbreviations, stated rather than inferred from length. A
# length test stood in for this, so any four-letter abbreviation added later
# silently got bare matching and the wrong-day bug came back.
_SHORT_DAYS = {"mon", "tue", "tues", "wed", "weds", "thu", "thur", "thurs",
               "fri", "sat", "sun"}


# Addressing the assistant. It is called Friday, which is also a weekday, and
# `search` returns the FIRST match: "hey friday, schedule a call tomorrow at
# 3pm" booked Friday, and "friday, what did sam say?" searched last Friday. The
# most natural way to talk to it was the one way guaranteed to get the wrong
# day.
_WAKE = re.compile(r"^\s*(?:hey|hi|ok|okay|yo)?\s*friday\b[\s,:!.-]*", re.I)
# ...unless a time follows it, in which case "friday" is the day after all.
# Stripping unconditionally ate the day out of "friday at 4" and booked today.
_WAKE_IS_A_DAY = re.compile(
    r"^\s*friday\s*(?:at\s+)?\d|^\s*friday\s+(?:morning|afternoon|evening|"
    r"night)\b", re.I)


def _unwake(text: str) -> str:
    """Drop the assistant's name when it is being addressed, not scheduled.

    It is called Friday and Friday is also a weekday, so the first match in
    "hey friday, schedule a call tomorrow at 3pm" was the wake word and the
    meeting went in three days early."""
    if _WAKE_IS_A_DAY.match(text or ""):
        return text or ""
    return _WAKE.sub("", text or "", count=1)


def _start_of(d: _dt.date) -> float:
    return _dt.datetime.combine(d, _dt.time.min).timestamp()


def _end_of(d: _dt.date) -> float:
    return _dt.datetime.combine(d, _dt.time.max).timestamp()


def parse(text: str, today: _dt.date = None) -> tuple:
    """(oldest, latest, label) or (0, 0, "") when no time was named."""
    # The wake word first: it is called Friday and that is also a weekday, so
    # "friday, what did sam say?" searched last Friday and labelled the answer
    # as though the question had been understood.
    t = " " + _unwake((text or "").lower()) + " "
    today = today or _dt.date.today()

    # Both named at once is a two-day span, not the older of the two: "yesterday
    # and the day before yesterday" was answered with Saturday only.
    if (re.search(r"\bday before yesterday\b", t)
            and re.search(r"\byesterday\b.*\bday before\b|\bday before "
                          r"yesterday\b.*\byesterday\b", t)):
        a, b = today - _dt.timedelta(days=2), today - _dt.timedelta(days=1)
        return _start_of(a), _end_of(b), "yesterday and the day before"
    if re.search(r"\bday before yesterday\b", t):
        d = today - _dt.timedelta(days=2)
        return _start_of(d), _end_of(d), "the day before yesterday"
    if re.search(r"\byesterday\b", t):
        d = today - _dt.timedelta(days=1)
        return _start_of(d), _end_of(d), "yesterday"
    if re.search(r"\b(?:today|this morning|so far today)\b", t):
        return _start_of(today), _end_of(today), "today"
    if re.search(r"\blast night\b", t):
        d = today - _dt.timedelta(days=1)
        return (_dt.datetime.combine(d, _dt.time(18, 0)).timestamp(),
                _dt.datetime.combine(today, _dt.time(6, 0)).timestamp(),
                "last night")

    m = re.search(r"\blast (\d+) (hour|day|week)s?\b", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        secs = {"hour": 3600, "day": 86400, "week": 604800}[unit]
        now = _dt.datetime.now().timestamp()
        return now - n * secs, now, f"the last {n} {unit}{'s' if n != 1 else ''}"

    if re.search(r"\bthis week\b", t):
        start = today - _dt.timedelta(days=today.weekday())
        return _start_of(start), _end_of(today), "this week"
    if re.search(r"\blast week\b", t):
        end = today - _dt.timedelta(days=today.weekday() + 1)
        start = end - _dt.timedelta(days=6)
        return _start_of(start), _end_of(end), "last week"

    # "on Friday", "on Monday": the most recent one that has already happened,
    # because you are asking about something that was said.
    # Full names only, and abbreviations solely after "on" or "last". Looking
    # for every spelling bare meant "what did it say when I sat down" reported
    # "Sat (15 Aug)" and searched the wrong day, with a label that told you it
    # had understood.
    for name, idx in _DAYS.items():
        pattern = (r"\b(?:on|last)\s+" + name + r"\b" if name in _SHORT_DAYS
                   else r"\b(?:on |last )?" + name + r"\b")
        if re.search(pattern, t):
            back = (today.weekday() - idx) % 7
            if back == 0:
                back = 7          # "on Friday" said on a Friday means last one
            d = today - _dt.timedelta(days=back)
            return _start_of(d), _end_of(d), f"{name.capitalize()} ({d:%d %b})"
    return 0, 0, ""


_CLOCK = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.I)
# Full names match anywhere, because nothing else is spelled like them.
#
# Abbreviations need day-context, because several of them are ordinary English
# words. Matching them bare booked "sat down at 4" for Saturday and "the sun is
# out at 4" for Sunday: the wrong DAY, stated as a confident confirmation. So an
# abbreviation counts only where a day actually goes, which is next to a time or
# after the words that introduce one, and never straight after a subject, where
# "we sat at 4" and "i wed at 4" are past-tense verbs rather than days.
_DAY_WORD = re.compile(
    r"\b(today|tonight|tomorrow|tmrw"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|(?<![\w-])(?:on|by|next|this|come)\s+"
    r"(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)(?![\w-])"
    r"|(?<![\w-])(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)"
    r"(?![\w-])\s*(?=(?:at\s+)?\d|at\s|morning|afternoon|evening|night)",
    re.I)
# What cannot come immediately before a weekday abbreviation. A determiner or a
# subject means the word is doing its ordinary English job: "the sun at 4" is
# not Sunday and "we sat at 4" is not Saturday. Listing the pronouns alone left
# every other subject walking through, so this is the grammatical rule rather
# than an enumeration of the examples somebody happened to try.
_NOT_BEFORE_A_DAY = {
    "the", "a", "an", "my", "your", "our", "their", "its", "his", "her",
    "we", "i", "he", "she", "they", "you", "it",
}
# A day that was named and NOT understood. "the 31st of February", "on the
# 14th", "next month", "yesterday": each of these says plainly which day is
# meant, and none of them is a day word. With no day word found the parser fell
# through to today, so "the 31st of February at 4" and "yesterday at 4" both
# quietly became today at 16:00. A meeting in the wrong slot is worse than one
# you had to type yourself, and a date in the PAST is not a slot at all.
# Narrower than it first was. "last \w+" matched "let's do the last one at 4",
# and a bare "may" matched "we may as well meet at 4", so ordinary English with
# a perfectly clear time was refused and the error insisted you had named a day.
# Only "last <weekday>" counts, and a month only when a number is near it.
_A_DAY_WAS_MEANT = re.compile(
    r"\byesterday\b"
    r"|\blast\s+(?:mon|tue|tues|wed|weds|wednes|thu|thur|thurs|fri|sat|satur"
    r"|sun)(?:day)?\b"
    r"|\bnext\s+(?:week|month|year)\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)\b"
    r"|\b\d{1,2}/\d{1,2}\b"
    r"|\b(?:jan(?:uary)?|feb(?:ruary)?|march|april|june|july|august"
    r"|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
    r"|\b(?:may|mar|apr|jun|jul|aug)\s+\d{1,2}\b"
    r"|\b\d{1,2}\s+(?:may|mar|apr|jun|jul|aug)\b", re.I)


def names_a_day(text: str) -> bool:
    """Whether the sentence tried to say which day.

    So a refusal can tell you which kind it was. "When?" in answer to
    "yesterday at 4" is baffling, because you did say when: Friday read it and
    would not use it."""
    low = (text or "").lower()
    return bool(_DAY_WORD.search(low) or _A_DAY_WAS_MEANT.search(low))


def moment(text: str, now: _dt.datetime = None) -> tuple:
    """(epoch, how_it_reads) for a spoken time, or (0, "").

    Deliberately narrow. It handles the shapes a person actually says when
    arranging something ("Thursday at 4", "tomorrow 10:30am") and refuses
    everything else, because putting a meeting in the wrong slot is worse than
    admitting you did not follow."""
    if not text:
        return 0, ""
    now = now or _dt.datetime.now()
    low = _unwake(text.lower())

    day, said_today, evening = None, False, False
    # Named separately from the clock match below, which reuses `m` in its own
    # loop: sharing the name meant a later check read the clock match and
    # silently did nothing.
    # Every candidate, not just the first, so a suppressed abbreviation does
    # not hide a real day later in the sentence: "we sat at 4 on friday" means
    # Friday. Suppressing and stopping turned a named day into today, which is
    # the failure this file says is worse than making you type it.
    dm = None
    for cand in _DAY_WORD.finditer(low):
        abbrev = cand.group(3) or ""
        if abbrev:
            before = low[:cand.start(3)].split()
            if before and before[-1].strip(",.;:") in _NOT_BEFORE_A_DAY:
                continue
        dm = cand
        break
    if dm:
        # Whichever alternative matched: the full name, or an abbreviation with
        # a day-shaped context in front of it or a time behind it.
        word = dm.group(1) or dm.group(2) or dm.group(3)
        if word in _RELATIVE:
            day = now.date() + _dt.timedelta(days=_RELATIVE[word])
            said_today = _RELATIVE[word] == 0
            evening = word == "tonight"
        elif word in _DAYS and re.search(r"\blast\s+" + word + r"\b", low):
            # "last Tuesday" is a day that has gone. Reading it as next Tuesday
            # puts the meeting a week from the one you meant.
            return 0, ""
        elif word not in _DAYS:
            # A day word nobody taught the lookup. Refusing beats raising, and
            # beats guessing.
            return 0, ""
        else:
            want = _DAYS[word]
            ahead = (want - now.weekday()) % 7
            day = now.date() + _dt.timedelta(days=ahead or 7)

    # Prefer a number that is actually a time: one carrying am/pm, or one
    # introduced by "at". Only if there is neither does a bare number count,
    # and then the LAST one, because the first is usually a quantity. "Grab 15
    # minutes tomorrow at 4" booked a quarter past three, and "book 1:1
    # tomorrow at 4" booked one in the afternoon.
    found = [m for m in _CLOCK.finditer(low) if int(m.group(1)) <= 23]
    marked = [m for m in found if (m.group(3) or "")]
    at_ones = [m for m in found
               if re.search(r"\bat\s*$", low[:m.start()])]
    ordered = marked or at_ones or list(reversed(found))
    hour = minute = None
    for m in ordered:
        h = int(m.group(1))
        mer = (m.group(3) or "").replace(".", "")
        # A bare number is only a time if it could be one and the sentence is
        # about arranging something. 4 means 4pm to everybody arranging a
        # meeting; 04:00 is not what anyone means.
        if mer.startswith("p") and h < 12:
            h += 12
        elif mer.startswith("a") and h == 12:
            h = 0
        elif not mer and h <= 7:
            h += 12
        elif not mer and evening and h < 12:
            h += 12          # "tonight at 8" is not eight in the morning
        hour, minute = h, int(m.group(2) or 0)
        break

    if hour is None:
        return 0, ""
    if day is None and _A_DAY_WAS_MEANT.search(low):
        # You named a day and Friday could not read it. Refusing is the only
        # honest answer: assuming today puts the meeting on a day nobody asked
        # for, and it looks exactly like a correct answer.
        return 0, ""
    day = day or now.date()
    when = _dt.datetime.combine(day, _dt.time(hour, minute))
    if when < now:
        # Only a bare time rolls forward to tomorrow, which is what "at 4" said
        # in the evening means. A day that was named explicitly and has already
        # gone is a mistake, not tomorrow.
        if said_today or day != now.date():
            # "Today at 9" said at six in the evening is a mistake, not
            # tomorrow morning, and neither is a day you named that has gone.
            return 0, ""
        when += _dt.timedelta(days=1)
    return when.timestamp(), when.strftime("%A %-d %B at %H:%M")
