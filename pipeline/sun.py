"""Where the sun is, for the building-shade layer.

One table for the whole city: 12 months x 24 hours of (azimuth,
elevation), each computed at the ANCHOR for that slot -- the 15th of the
month, on the hour, New York clock time, in config.SUN_ANCHOR_YEAR. Night
slots (sun at or below the horizon) are None. The table ships in the
export's meta so the file is self-describing; the server blends between
neighbouring slots by the minute and by the day, it never recomputes the
sun.

Azimuth is degrees clockwise from north (90 = the sun is due east);
elevation is degrees above the horizon, with atmospheric refraction, so
"the sun is up" matches what a walker sees. A shadow points directly away
from the azimuth and is `height / tan(elevation)` long.

Why one observer point serves the whole city, with the measured spread,
is on config.SUN_OBSERVER_LAT. The library is astral (pure Python, NOAA's
algorithm); its one trap is that a NAIVE datetime is silently treated as
UTC, so sun_position refuses anything without a tzinfo.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from astral import Observer
from astral import sun as astral_sun

from pipeline import config

MONTHS = 12
HOURS = 24
SLOT_COUNT = MONTHS * HOURS

_OBSERVER = Observer(latitude=config.SUN_OBSERVER_LAT,
                     longitude=config.SUN_OBSERVER_LON)
_TZ = ZoneInfo(config.SUN_TIMEZONE)

# Stored to 3 decimals: 0.001 deg is far below the observer-point spread
# above, and it keeps the exported table readable.
_DECIMALS = 3


def anchor_datetime(month: int, hour: int) -> datetime:
    """The tz-aware moment slot (month, hour) is computed at."""
    if not 1 <= month <= MONTHS:
        raise ValueError(f"month must be 1-{MONTHS}, got {month}")
    if not 0 <= hour < HOURS:
        raise ValueError(f"hour must be 0-{HOURS - 1}, got {hour}")
    return datetime(config.SUN_ANCHOR_YEAR, month, config.SUN_ANCHOR_DAY,
                    hour, tzinfo=_TZ)


def sun_position(when: datetime) -> tuple[float, float]:
    """(azimuth, elevation) in degrees at the city observer point.

    `when` MUST be timezone-aware: astral reads a naive datetime as UTC,
    which would shift every slot by four or five hours without any error.
    """
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("sun_position needs a timezone-aware datetime "
                         "(astral treats a naive one as UTC)")
    azimuth = astral_sun.azimuth(_OBSERVER, when)
    elevation = astral_sun.elevation(_OBSERVER, when, with_refraction=True)
    return round(azimuth, _DECIMALS), round(elevation, _DECIMALS)


def sun_table() -> list[list[list[float] | None]]:
    """table[month - 1][hour] = [azimuth, elevation], or None at night.

    Plain lists and floats so it serialises to JSON as-is.
    """
    table = []
    for month in range(1, MONTHS + 1):
        row = []
        for hour in range(HOURS):
            azimuth, elevation = sun_position(anchor_datetime(month, hour))
            if elevation <= 0:
                row.append(None)
            else:
                row.append([azimuth, elevation])
        table.append(row)
    return table


def daylight_slots(table) -> list[tuple[int, int]]:
    """Every (month, hour) whose slot is not night, month 1-based."""
    slots = []
    for month_index, row in enumerate(table):
        for hour, value in enumerate(row):
            if value is not None:
                slots.append((month_index + 1, hour))
    return slots
