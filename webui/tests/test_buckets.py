import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from common import buckets


@pytest.fixture
def tz(monkeypatch):
    def set_tz(name):
        monkeypatch.setenv("TZ", name)
        time.tzset()
        return ZoneInfo(name)
    yield set_tz
    monkeypatch.undo()
    time.tzset()


def ts(zone, *args):
    return int(datetime(*args, tzinfo=zone).timestamp())


def test_minute_boundaries():
    assert buckets.minute_start(119) == 60
    assert buckets.minute_start(120) == 120


def test_day_starts_at_local_midnight(tz):
    berlin = tz("Europe/Berlin")
    midnight = ts(berlin, 2026, 7, 1)
    assert buckets.day_start(midnight) == midnight
    assert buckets.day_start(midnight + 86399) == midnight
    assert buckets.day_start(midnight - 1) == ts(berlin, 2026, 6, 30)


def test_dst_change_day_is_one_bucket_of_25_hours(tz):
    berlin = tz("Europe/Berlin")
    day = ts(berlin, 2026, 10, 25)            # clocks go back from 03:00 to 02:00
    end = ts(berlin, 2026, 10, 26)
    assert end - day == 25 * 3600
    assert {buckets.day_start(t) for t in range(day, end, 600)} == {day}
    assert buckets.next_day(day) == end
    # The repeated hour 02:00-03:00 gives two separate hour buckets.
    hours = {buckets.hour_start(t) for t in range(day, end, 600)}
    assert len(hours) == 25


def test_spring_dst_day_has_23_hours(tz):
    berlin = tz("Europe/Berlin")
    day = ts(berlin, 2026, 3, 29)
    assert buckets.next_day(day) - day == 23 * 3600
    assert len(buckets.steps(day, buckets.next_day(day) - 1, buckets.HOUR)) == 23


def test_hours_follow_half_hour_offsets(tz):
    kolkata = tz("Asia/Kolkata")                # UTC+05:30
    t = ts(kolkata, 2026, 7, 1, 14, 45)
    assert buckets.hour_start(t) == ts(kolkata, 2026, 7, 1, 14, 0)
    assert buckets.align(t, 300) == ts(kolkata, 2026, 7, 1, 14, 45)


def test_steps(tz):
    berlin = tz("Europe/Berlin")
    start = ts(berlin, 2026, 10, 24, 12)
    days = buckets.steps(start, ts(berlin, 2026, 10, 27, 1), buckets.DAY)
    assert days == [ts(berlin, 2026, 10, d) for d in (24, 25, 26, 27)]
    assert buckets.steps(130, 300, 60) == [120, 180, 240, 300]
