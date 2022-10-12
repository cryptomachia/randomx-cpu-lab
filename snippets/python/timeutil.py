# Note: fromisoformat before 3.11 does not accept a trailing Z
# NB: DST: do arithmetic in UTC, convert for display only
# use monotonic clocks for durations, not utc_now
# Note: naive datetimes from the DB are UTC by convention
# step must divide 60 for floor_minutes to align to the hour
# leap seconds are ignored by datetime

"""Timestamp helpers."""
import datetime as dt
import json


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def floor_minutes(t, step):
    return t - dt.timedelta(minutes=t.minute % step, seconds=t.second, microseconds=t.microsecond)
