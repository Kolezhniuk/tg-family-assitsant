from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str) -> time:
    match = _HHMM_RE.match(value)
    if not match:
        raise ValueError(f"invalid HH:MM value: {value!r}")
    return time(int(match.group(1)), int(match.group(2)))


def load_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"invalid timezone: {name!r}") from exc


def add_minutes(value: time, minutes: int) -> time:
    combined = datetime.combine(date(2000, 1, 1), value) + timedelta(minutes=minutes)
    return combined.time()


class Clock:
    def now_utc(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    def __init__(self, moment: datetime):
        if moment.tzinfo is None:
            raise ValueError("FixedClock requires an aware datetime")
        self._moment = moment.astimezone(timezone.utc)

    def now_utc(self) -> datetime:
        return self._moment


def resolve_local_wall_clock(naive: datetime, tz: ZoneInfo) -> datetime:
    aware_first = naive.replace(tzinfo=tz, fold=0)
    aware_second = naive.replace(tzinfo=tz, fold=1)
    offset_first = aware_first.utcoffset()
    offset_second = aware_second.utcoffset()
    if offset_first == offset_second:
        return aware_first.astimezone(timezone.utc)

    utc_first = aware_first.astimezone(timezone.utc)
    if utc_first.astimezone(tz).replace(tzinfo=None) == naive:
        return utc_first

    utc_second = aware_second.astimezone(timezone.utc)
    if utc_second.astimezone(tz).replace(tzinfo=None) == naive:
        return utc_second

    day_start = naive.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    lo = day_start.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
    hi = day_end.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)
    offset_before = day_start.replace(tzinfo=tz, fold=0).utcoffset()
    for _ in range(60):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == offset_before:
            lo = mid
        else:
            hi = mid
    return hi


def local_datetime_for(day: date, wall_time: time, tz: ZoneInfo) -> datetime:
    naive = datetime.combine(day, wall_time)
    return resolve_local_wall_clock(naive, tz)


def to_local(instant_utc: datetime, tz: ZoneInfo) -> datetime:
    return instant_utc.astimezone(tz)


@dataclass(frozen=True)
class QuietHours:
    start: time
    end: time

    def contains(self, moment: time) -> bool:
        if self.start <= self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end


def times_wholly_in_quiet_hours(start: time, end: time, quiet: QuietHours) -> bool:
    return quiet.contains(start) and quiet.contains(end)


@dataclass(frozen=True)
class Window:
    opens_at_utc: datetime
    closes_at_utc: datetime

    def contains(self, instant_utc: datetime) -> bool:
        return self.opens_at_utc <= instant_utc < self.closes_at_utc


def make_window(opens_at_utc: datetime, duration: timedelta) -> Window:
    return Window(opens_at_utc=opens_at_utc, closes_at_utc=opens_at_utc + duration)
