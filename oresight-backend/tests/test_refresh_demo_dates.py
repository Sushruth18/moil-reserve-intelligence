"""Pure-logic tests for scripts/refresh_demo_dates.py (no database, no Neo4j)."""

from datetime import date, datetime, timedelta, timezone

from scripts.refresh_demo_dates import make_window, shift_date, shift_description, shift_ts

UTC = timezone.utc
TODAY = date(2026, 11, 24)


def _w(anchor=date(2026, 9, 22), override=None):
    return make_window(TODAY, anchor, override)


def test_offset_puts_latest_production_record_on_yesterday():
    w = _w()
    assert w.cap_date == date(2026, 11, 23)
    assert w.offset_days == 62
    assert date(2026, 9, 22) + timedelta(days=w.offset_days) == w.cap_date


def test_offset_is_zero_when_already_current_so_a_second_run_is_a_no_op():
    w = _w(anchor=date(2026, 11, 23))
    assert w.offset_days == 0
    d, t = date(2026, 11, 1), datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    assert shift_date(d, w) == d and shift_ts(t, w) == t
    assert shift_description("on 2026-09-09.", t, w) == "on 2026-09-09."


def test_explicit_offset_overrides_the_computed_one():
    assert _w(override=10).offset_days == 10


def test_dates_never_land_in_the_future_and_keep_their_order():
    w = _w()
    a, b = date(2026, 10, 5), date(2026, 10, 6)
    assert shift_date(a, w) == w.cap_date and shift_date(b, w) == w.cap_date  # both capped, order not inverted
    assert shift_date(date(2026, 9, 1), w) == date(2026, 11, 2)  # uncapped rows shift by exactly the offset


def test_timestamps_shift_by_whole_days_keep_time_of_day_and_cap_at_end_of_yesterday():
    w = _w()
    t = datetime(2026, 9, 5, 3, 51, 34, tzinfo=UTC)
    assert shift_ts(t, w) == datetime(2026, 11, 6, 3, 51, 34, tzinfo=UTC)
    late = datetime(2026, 10, 6, 14, 51, tzinfo=UTC)
    assert shift_ts(late, w) == datetime(2026, 11, 23, 23, 59, 59, tzinfo=UTC)  # < today 00:00: outside the Watcher's 6 h window


def test_resolved_never_precedes_detected_after_capping():
    w = _w()
    detected, resolved = datetime(2026, 9, 24, 10, 20, tzinfo=UTC), datetime(2026, 9, 26, 17, 27, tzinfo=UTC)
    assert shift_ts(resolved, w) >= shift_ts(detected, w)


def test_iso_dates_inside_descriptions_move_with_the_event():
    w = _w()
    detected = datetime(2026, 9, 9, 15, 34, tzinfo=UTC)
    out = shift_description("Nagpur actual output (665t) is 30% below target (950t) on 2026-09-09.", detected, w)
    assert out.endswith("on 2026-11-10.")


def test_forecast_window_text_keeps_its_position_relative_to_a_capped_detection():
    w = _w()
    detected = datetime(2026, 9, 25, 7, 6, tzinfo=UTC)  # capped: 25 Sep + 62 d would be 26 Nov
    desc = "24 h (25 Sep 07:00-26 Sep 06:00 UTC), above the IMD heavy-rain threshold"
    out = shift_description(desc, detected, w)
    delta = (shift_ts(detected, w).date() - detected.date()).days  # 59 days, not 62
    assert delta == 59
    assert "(23 Nov 07:00-24 Nov 06:00 UTC)" in out  # end still after start; not collapsed onto one day


def test_forecast_window_year_is_inferred_across_new_year():
    w = make_window(date(2027, 3, 1), date(2026, 12, 31))  # offset 59 -> cap 2027-02-28
    detected = datetime(2026, 12, 30, 22, 0, tzinfo=UTC)  # not capped: 30 Dec + 59 d = 27 Feb
    out = shift_description("window (31 Dec 00:00-01 Jan 06:00 UTC)", detected, w)
    assert out == "window (28 Feb 00:00-01 Mar 06:00 UTC)"  # '01 Jan' resolves to 2027, not 2025/2026


def test_descriptions_without_dates_are_returned_unchanged():
    w = _w()
    s = "Haul Truck HT-302 engine overheating has taken the unit fully offline."
    assert shift_description(s, datetime(2026, 9, 9, tzinfo=UTC), w) == s
    assert shift_description(None, None, w) is None
