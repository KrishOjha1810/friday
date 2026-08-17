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
_GREETING = re.compile(r"^\s*(?:hey|hi|ok|okay|yo)\s+friday\b[\s,:!.-]*", re.I)
_LEADING_FRIDAY = re.compile(r"^\s*friday\b[\s,:!.\-']*", re.I)
# Any OTHER day named in the sentence. If there is one, a leading "friday" was
# how you addressed the assistant; if there is not, it was the day you meant.
_SOME_OTHER_DAY = re.compile(
    r"\b(?:today|tonight|tomorrow|tmrw|monday|tuesday|wednesday|thursday|"
    r"saturday|sunday|mon|tue|tues|wed|weds|thu|thur|thurs|sat|sun)\b", re.I)


def _unwake(text: str) -> str:
    """Drop the assistant's name when it is being addressed, not scheduled.

    It is called Friday and Friday is also a weekday, and this has been wrong
    in both directions. Reading the wake word as a day booked "hey friday,
    schedule a call tomorrow at 3pm" three days early. Stripping it always
    booked "friday works, book the call for 4" today, four days early, which is
    worse because that is how people accept a proposed slot.

    One rule covers both: a leading "friday" is the wake word when the sentence
    greets it, or when some OTHER day is named. Otherwise it is the day."""
    got = text or ""
    if _GREETING.match(got):
        return _GREETING.sub("", got, count=1)
    m = _LEADING_FRIDAY.match(got)
    if m and _SOME_OTHER_DAY.search(got[m.end():]):
        return got[m.end():]
    return got


def _start_of(d: _dt.date) -> float:
    return _dt.datetime.combine(d, _dt.time.min).timestamp()


def _end_of(d: _dt.date) -> float:
    return _dt.datetime.combine(d, _dt.time.max).timestamp()


# Addressing the assistant and then asking it something. In `moment` a bare
# leading "friday" is the day, because "friday at 4" is a booking; when READING,
# "friday, what did sam say?" is a question with no timeframe, and answering it
# with a one-day window over last Friday is the failure this module opens by
# describing. The two entry points differ because the sentences differ.
# Not the possessive: "friday's messages" is about the day, and "friday, what
# did sam say?" is somebody talking to the assistant. The apostrophe is the
# whole difference.
# The word after it decides. Somebody addressing the assistant follows the name
# with a question or an instruction; somebody naming the day follows it with a
# noun. "Friday, what did sam say?" is a question with no timeframe; "friday
# standup" and "friday's messages" are both about the day.
_ADDRESSED = re.compile(
    r"^\s*(?:hey|hi|ok|okay|yo|thanks|thank you)?\s*friday\b(?!'s)"
    r"[\s,:!.\-]*(?=(?:what|whats|what's|who|when|where|why|how|any|anything|"
    r"did|do|does|is|are|was|were|can|could|would|will|show|tell|give|read|"
    r"summari[sz]e|catch|remind|find|check)\b)", re.I)


def parse(text: str, today: _dt.date = None) -> tuple:
    """(oldest, latest, label) or (0, 0, "") when no time was named."""
    # The wake word first: it is called Friday and that is also a weekday, so
    # "friday, what did sam say?" searched last Friday and labelled the answer
    # as though the question had been understood.
    t = " " + _ADDRESSED.sub("", (text or "").lower(), count=1) + " "
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


# "4.30" as well as "4:30". The dot form is at least as common in writing, and
# without it the minutes were dropped and a meeting was quietly booked on the
# hour, half an hour before the one you asked for.
_CLOCK = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b",
    re.I)
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
    # Through the same wake-word handling as moment(), or the two disagree
    # about whether the word "friday" is a day, and the refusal tells you your
    # day "may have gone already" when you never named one.
    low = _unwake((text or "").lower())
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

    day, said_today = None, False
    # Which half of the day was named, in words rather than by am/pm. Only the
    # single word "tonight" was ever read, so "tomorrow at 8 in the evening"
    # booked eight in the morning and "at 7 in the morning" booked seven at
    # night: twelve hours out, on the most ordinary phrasing there is, and read
    # back as a confident confirmation.
    half = ""
    if re.search(r"\b(?:evening|tonight|at night|tonite)\b", low):
        half = "pm"
    elif re.search(r"\b(?:morning)\b", low):
        half = "am"
    elif re.search(r"\b(?:afternoon)\b", low):
        half = "pm"
    # Named separately from the clock match below, which reuses `m` in its own
    # loop: sharing the name meant a later check read the clock match and
    # silently did nothing.
    # Every candidate, not just the first, so a suppressed abbreviation does
    # not hide a real day later in the sentence: "we sat at 4 on friday" means
    # Friday. Suppressing and stopping turned a named day into today, which is
    # the failure this file says is worse than making you type it.
    if half and re.search(r"\b(?:this|tonight|tonite)\b", low) and \
            not _SOME_OTHER_DAY.search(low):
        # "This evening at 8" and "tonight at 8" are today.
        day, said_today = now.date(), True
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
    # Minutes checked as well as hours. Only the hour was, so "at 4.99" and
    # "at 4:99" reached datetime.time() and raised, which meant a plausible
    # typo crashed the request instead of being refused.
    found = [m for m in _CLOCK.finditer(low)
             if int(m.group(1)) <= 23 and int(m.group(2) or 0) <= 59
             # "at 22 baker street" is an address. A number followed by a word
             # that belongs to a place is not the time, however much "at" in
             # front of it looks like one.
             and not re.match(r"\s*(?:street|st\b|road|rd\b|avenue|ave\b|"
                              r"lane|drive|place|square|baker|floor|room|"
                              r"people|minutes|mins|hours|hrs)",
                              low[m.end():])]
    # A number is a TIME when it carries am or pm, or when "at" introduces it.
    # The "at" test used to look at the text before the match, and the pattern
    # itself starts with an optional "at", so the match already contained the
    # word and the text before it never ended in one: the whole middle tier was
    # dead, and every unmarked sentence fell through to the last bare number.
    # A room number, a duration or a head count trailing the time then won.
    strong = [m for m in found
              if (m.group(3) or "")
              or m.group(0).lower().lstrip().startswith("at")]
    # A move names two times and only the ORIGIN usually carries the am or pm:
    # "push the 3pm back to 4" left the meeting where it was and read that back
    # as agreement. Where the sentence moves something, the destination is
    # whatever comes last, marked or not.
    if re.search(r"\b(?:push|move|shift|reschedule|bump|change|make it|"
                 r"back to|instead)\b", low) and len(found) > 1:
        strong = []
    # LAST, in both tiers. The destination of a move and the correction in a
    # self-correcting sentence are both the last time named: "at 3pm, actually
    # make it 4pm" was booking three.
    ordered = list(reversed(strong or found))
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
        elif not mer and half == "pm" and h < 12:
            h += 12          # "8 in the evening" is not eight in the morning
        elif not mer and half == "am":
            if h == 12:
                h = 0        # "12 in the morning" is midnight
        elif not mer and h <= 7:
            # No half named. A bare small number means the afternoon to anybody
            # arranging something: nobody says "four" and means 04:00.
            h += 12
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
