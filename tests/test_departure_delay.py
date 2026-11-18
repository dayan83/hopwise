from datetime import timedelta

import pytest

from departure_service import departure_delay, interpret_delay
from models import Departure


@pytest.mark.parametrize("scheduled,estimated,seconds", [
    ("2026-11-17T00:00:00Z", None, None),
    (None, "2026-11-17T00:00:00Z", None),
    (None, None, None),
    ("2026-11-17T00:00:00Z", "2026-11-17T00:00:00Z", 0),
    ("2026-11-17T00:00:00Z", "2026-11-17T00:02:00Z", 120),
    ("2026-11-17T00:00:00Z", "2026-11-16T23:59:00Z", -60),
    ("2026-11-17T00:00:00Z", "2026-11-17T00:00:15Z", 15),
    # Melbourne clocks move backward between these instants; UTC comparison stays correct.
    ("2026-04-04T15:59:00Z", "2026-04-04T16:01:00Z", 120),
])
def test_estimate_based_delay(scheduled, estimated, seconds):
    departure = Departure(scheduled_departure_utc=scheduled, estimated_departure_utc=estimated)
    expected = timedelta(seconds=seconds) if seconds is not None else None
    assert departure_delay(departure) == expected


@pytest.mark.parametrize("seconds,status,description", [
    (30, "late", "Estimated 30 seconds late relative to the schedule."),
    (-30, "early", "Estimated 30 seconds early relative to the schedule."),
    (0, "matches_schedule", "The estimate matches the schedule; this is not a guarantee of on-time operation."),
    (None, "unknown", "Timing comparison unavailable: a scheduled time or real-time estimate is missing."),
    (1, "late", "Estimated 1 second late relative to the schedule."),
    (-0.5, "early", "Estimated 0.5 seconds early relative to the schedule."),
])
def test_delay_interpretation(seconds, status, description):
    assert interpret_delay(seconds) == {"delay_status": status, "delay_description": description}
