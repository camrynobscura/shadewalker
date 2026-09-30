"""Tests for pipeline/sun.py -- the 12 x 24 sun table behind building shade.

What is pinned, and why:

  - Against published solar geometry (NOAA's calculator; the same numbers
    fall out of 90 - latitude +/- 23.44 at the solstices): June 21 peaks
    ~72.7 deg due south, December 21 ~25.9 deg. Catches a wrong observer,
    a wrong unit, or the naive-datetime trap (a UTC-read noon would peak
    hours late and degrees low).
  - One table serves the city. Measured 2026-09-24 over every daylight
    anchor slot: the four CITY_BBOX corners differ from the observer point
    by at most 0.31 deg elevation / 1.00 deg azimuth. The 0.5 / 1.5 deg
    tolerances are the physical bar, not the measurement: a 1.5 deg
    bearing error moves a 100 m shadow 2.6 m sideways (about one 2 m
    raster cell) and a 3 m shadow 8 cm. A bbox change that broke this
    would need per-area tables, so it must fail loudly here.
  - Anchors are plain clock hours: the 15th sits after both DST
    transitions, so no slot lands in the March gap or the November fold.
  - Night is None, and the daylight count is exactly 146 for the fixed
    anchor year -- a change here means the year, the observer, or the
    refraction setting moved, all of which change every shade table.
  - Naive datetimes are refused, not silently read as UTC.
  - The table is plain JSON-able lists and floats (export meta).
"""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from astral import Observer
from astral import sun as astral_sun

from pipeline import config, sun

TZ = ZoneInfo(config.SUN_TIMEZONE)


def _peak_elevation(date: datetime) -> tuple[float, float]:
    """(peak elevation, azimuth at that peak) scanning 10:00-15:00 by the
    minute -- solar noon in New York is around 12:00 EST / 13:00 EDT."""
    best = None
    for hour in range(10, 15):
        for minute in range(60):
            when = date.replace(hour=hour, minute=minute)
            azimuth, elevation = sun.sun_position(when)
            if best is None or elevation > best[0]:
                best = (elevation, azimuth)
    return best


def test_june_solstice_peak_matches_noaa():
    elevation, azimuth = _peak_elevation(datetime(2026, 6, 21, tzinfo=TZ))
    assert elevation == pytest.approx(72.7, abs=1.0)
    assert azimuth == pytest.approx(180.0, abs=1.0)


def test_december_solstice_peak_matches_noaa():
    elevation, azimuth = _peak_elevation(datetime(2026, 12, 21, tzinfo=TZ))
    assert elevation == pytest.approx(25.9, abs=1.0)
    assert azimuth == pytest.approx(180.0, abs=1.0)


def test_morning_sun_is_east_afternoon_sun_is_west():
    morning, _ = sun.sun_position(sun.anchor_datetime(7, 9))
    afternoon, _ = sun.sun_position(sun.anchor_datetime(7, 16))
    assert 60 < morning < 180
    assert 180 < afternoon < 300


def test_one_table_serves_the_whole_city():
    bbox = config.CITY_BBOX
    corners = [Observer(latitude=lat, longitude=lon)
               for lat in (bbox.lat_min, bbox.lat_max)
               for lon in (bbox.lon_min, bbox.lon_max)]
    worst_elevation = 0.0
    worst_azimuth = 0.0
    for month, hour in sun.daylight_slots(sun.sun_table()):
        when = sun.anchor_datetime(month, hour)
        azimuth, elevation = sun.sun_position(when)
        for corner in corners:
            corner_elevation = astral_sun.elevation(corner, when)
            corner_azimuth = astral_sun.azimuth(corner, when)
            # A corner in daylight while the centre is night (or the
            # reverse) would be a slot the table gets categorically wrong.
            assert corner_elevation > 0, (month, hour, corner)
            worst_elevation = max(worst_elevation, abs(corner_elevation - elevation))
            spread = abs(corner_azimuth - azimuth) % 360
            worst_azimuth = max(worst_azimuth, min(spread, 360 - spread))
    assert worst_elevation < 0.5
    assert worst_azimuth < 1.5


def test_anchors_are_plain_clock_hours():
    for month in range(1, 13):
        for hour in range(24):
            when = sun.anchor_datetime(month, hour)
            # Round-tripping through UTC lands on the same wall clock only
            # when the hour exists exactly once that day.
            assert when.astimezone(ZoneInfo("UTC")).astimezone(TZ) == when
            assert when.hour == hour and when.fold == 0
            assert when.utcoffset() in (timedelta(hours=-4), timedelta(hours=-5))


def test_table_shape_night_and_daylight_count():
    table = sun.sun_table()
    assert len(table) == 12
    assert all(len(row) == 24 for row in table)
    for row in table:
        assert row[0] is None          # midnight is night in every month
        assert row[12] is not None     # noon is daylight in every month
    for value in (v for row in table for v in row if v is not None):
        azimuth, elevation = value
        assert 0 <= azimuth < 360
        assert 0 < elevation <= 90
    assert len(sun.daylight_slots(table)) == 146


def test_table_is_plain_json():
    table = sun.sun_table()
    assert json.loads(json.dumps(table)) == table
    for value in (v for row in table for v in row if v is not None):
        assert all(type(x) is float for x in value)


def test_naive_datetime_is_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        sun.sun_position(datetime(2026, 7, 15, 12))


def test_anchor_arguments_are_validated():
    with pytest.raises(ValueError):
        sun.anchor_datetime(0, 12)
    with pytest.raises(ValueError):
        sun.anchor_datetime(13, 12)
    with pytest.raises(ValueError):
        sun.anchor_datetime(7, 24)
