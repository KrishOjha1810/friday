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
