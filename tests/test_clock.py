from datetime import date, datetime, time, timedelta, timezone

import pytest

from care.clock import (
    FixedClock,
    QuietHours,
    Window,
    add_minutes,
    load_zone,
    local_datetime_for,
    make_window,
    parse_hhmm,
    times_wholly_in_quiet_hours,
    to_local,
)

KYIV = load_zone("Europe/Kyiv")


def test_parse_hhmm_accepts_valid_values():
    assert parse_hhmm("09:00") == time(9, 0)
    assert parse_hhmm("23:59") == time(23, 59)
    assert parse_hhmm("00:00") == time(0, 0)


@pytest.mark.parametrize("value", ["9:00", "24:00", "12:60", "abc", "", "12:5", "12:005"])
def test_parse_hhmm_rejects_malformed_values(value):
    with pytest.raises(ValueError):
        parse_hhmm(value)


def test_load_zone_rejects_unknown_timezone():
    with pytest.raises(ValueError):
        load_zone("Not/AZone")


def test_spring_forward_gap_resolves_to_first_valid_instant_after_gap():
    resolved = local_datetime_for(date(2026, 3, 29), time(3, 30), KYIV)
    assert resolved == datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
    assert to_local(resolved, KYIV) == datetime(2026, 3, 29, 4, 0, tzinfo=KYIV)


def test_spring_forward_gap_covers_whole_missing_hour():
    for minute in (0, 1, 30, 59):
        resolved = local_datetime_for(date(2026, 3, 29), time(3, minute), KYIV)
        assert resolved == datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)


def test_fall_back_overlap_resolves_to_first_occurrence():
    resolved = local_datetime_for(date(2026, 10, 25), time(3, 30), KYIV)
    assert resolved == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    local = to_local(resolved, KYIV)
    assert local.utcoffset() == timedelta(hours=3)


def test_ordinary_local_time_resolves_without_dst_adjustment():
    resolved = local_datetime_for(date(2026, 6, 15), time(9, 0), KYIV)
    assert resolved == datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)


def test_add_minutes_wraps_within_a_day():
    assert add_minutes(time(23, 45), 30) == time(0, 15)
    assert add_minutes(time(9, 0), 30) == time(9, 30)


def test_quiet_hours_wraps_past_midnight():
    quiet = QuietHours(start=time(21, 30), end=time(8, 0))
    assert quiet.contains(time(23, 0))
    assert quiet.contains(time(2, 0))
    assert quiet.contains(time(21, 30))
    assert not quiet.contains(time(8, 0))
    assert not quiet.contains(time(9, 0))


def test_quiet_hours_non_wrapping_range():
    quiet = QuietHours(start=time(1, 0), end=time(5, 0))
    assert quiet.contains(time(3, 0))
    assert not quiet.contains(time(6, 0))


def test_times_wholly_in_quiet_hours_true_case():
    quiet = QuietHours(start=time(21, 30), end=time(8, 0))
    assert times_wholly_in_quiet_hours(time(22, 0), time(22, 30), quiet)


def test_times_wholly_in_quiet_hours_false_case():
    quiet = QuietHours(start=time(21, 30), end=time(8, 0))
    assert not times_wholly_in_quiet_hours(time(9, 0), time(9, 30), quiet)


def test_fixed_clock_requires_aware_datetime():
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 1, 1, 0, 0))


def test_fixed_clock_returns_injected_moment():
    moment = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    clock = FixedClock(moment)
    assert clock.now_utc() == moment


def test_make_window_and_contains():
    opens = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    window = make_window(opens, timedelta(minutes=30))
    assert window == Window(opens, datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc))
    assert window.contains(opens)
    assert window.contains(opens + timedelta(minutes=29))
    assert not window.contains(opens + timedelta(minutes=30))
