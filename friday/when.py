"""Turn "yesterday", "on Friday", "this week" into a real range of time.

Asked what happened yesterday, Friday read the last fifteen messages whatever
their dates and then answered as though they were yesterday's. That is the worst
kind of wrong: the shape of the answer says the question was understood.

Everything here returns (oldest, latest, label) in local time, where the label
is what Friday will SAY it read, so the window and the claim can never disagree.
"""

import datetime as _dt
import re

_DAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
         "friday": 4, "saturday": 5, "sunday": 6}


def _start_of(d: _dt.date) -> float:
    return _dt.datetime.combine(d, _dt.time.min).timestamp()


def _end_of(d: _dt.date) -> float:
    return _dt.datetime.combine(d, _dt.time.max).timestamp()


def parse(text: str, today: _dt.date = None) -> tuple:
    """(oldest, latest, label) or (0, 0, "") when no time was named."""
    t = " " + (text or "").lower() + " "
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
    for name, idx in _DAYS.items():
        if re.search(r"\b(?:on |last )?" + name + r"\b", t):
            back = (today.weekday() - idx) % 7
            if back == 0:
                back = 7          # "on Friday" said on a Friday means last one
            d = today - _dt.timedelta(days=back)
            return _start_of(d), _end_of(d), f"{name.capitalize()} ({d:%d %b})"
    return 0, 0, ""


_CLOCK = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.I)
_DAY_WORD = re.compile(
    r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|"
    r"sunday)\b", re.I)


def moment(text: str, now: _dt.datetime = None) -> tuple:
    """(epoch, how_it_reads) for a spoken time, or (0, "").

    Deliberately narrow. It handles the shapes a person actually says when
    arranging something ("Thursday at 4", "tomorrow 10:30am") and refuses
    everything else, because putting a meeting in the wrong slot is worse than
    admitting you did not follow."""
    if not text:
        return 0, ""
    now = now or _dt.datetime.now()
    low = text.lower()

    day = None
    m = _DAY_WORD.search(low)
    if m:
        word = m.group(1)
        if word == "today":
            day = now.date()
        elif word == "tomorrow":
            day = now.date() + _dt.timedelta(days=1)
        else:
            want = _DAYS[word]
            ahead = (want - now.weekday()) % 7
            day = now.date() + _dt.timedelta(days=ahead or 7)

    hour = minute = None
    for m in _CLOCK.finditer(low):
        h = int(m.group(1))
        if h > 23:
            continue
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
        hour, minute = h, int(m.group(2) or 0)
        break

    if hour is None:
        return 0, ""
    day = day or now.date()
    when = _dt.datetime.combine(day, _dt.time(hour, minute))
    if when < now:
        when += _dt.timedelta(days=1)
    return when.timestamp(), when.strftime("%A %-d %B at %H:%M")
