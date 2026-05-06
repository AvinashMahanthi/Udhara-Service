from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_ist_timezone():
    """
    Return IST timezone.
    Falls back to fixed +05:30 if tzdata/IANA database is unavailable.
    """
    try:
        return ZoneInfo("Asia/Kolkata")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=5, minutes=30), name="IST")

