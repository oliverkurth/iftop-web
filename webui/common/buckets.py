"""Start times of the minute, hour and day buckets, in local time.

Hours and shorter steps follow the local UTC offset, so they line up with
local clock time also in zones with half-hour offsets, and every real hour
stays a separate bucket across DST changes. Days start at local midnight, so
a day bucket is 23 or 25 hours long when DST changes.
"""

import time

MINUTE = 60
HOUR = 3600
DAY = 86400


def _gmtoff(ts):
    return time.localtime(ts).tm_gmtoff


def align(ts, step):
    """Start of the step-long bucket containing ts, for steps up to an hour."""
    return ts - (ts + _gmtoff(ts)) % step


def minute_start(ts):
    return ts - ts % MINUTE


def hour_start(ts):
    return align(ts, HOUR)


def day_start(ts):
    t = time.localtime(ts)
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))


def next_day(day):
    # Days are 23 to 25 hours long; jumping 26 hours always lands in the next.
    return day_start(day + DAY + 2 * HOUR)


def bucket_start(ts, step):
    return day_start(ts) if step == DAY else align(ts, step)


def steps(start, end, step):
    """Bucket starts from the bucket containing start up to the one containing end."""
    t = bucket_start(start, step)
    result = []
    while t <= end:
        result.append(t)
        t = next_day(t) if step == DAY else t + step
    return result
