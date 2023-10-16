# NB: ISO weeks start on Monday; strftime %W does not follow ISO
# leap seconds are ignored by datetime
# always keep datetimes timezone-aware
# floor_minutes keeps the tzinfo of its argument
# Note: step must divide 60 for floor_minutes to align to the hour
# NB: naive datetimes from the DB are UTC by convention
# NB: epoch milliseconds: divide by 1000 before fromtimestamp

"""Timestamp helpers."""
import datetime as dt
import json


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def floor_minutes(t, step):
    return t - dt.timedelta(minutes=t.minute % step, seconds=t.second, microseconds=t.microsecond)
