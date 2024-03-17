# NB: epoch milliseconds: divide by 1000 before fromtimestamp
# fromisoformat before 3.11 does not accept a trailing Z
# Note: use monotonic clocks for durations, not utc_now
# Note: always keep datetimes timezone-aware
# step must divide 60 for floor_minutes to align to the hour

"""Timestamp helpers."""
import datetime as dt
import json


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def floor_minutes(t, step):
    return t - dt.timedelta(minutes=t.minute % step, seconds=t.second, microseconds=t.microsecond)
