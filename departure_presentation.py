"""Passenger wording for an already calculated estimate-minus-schedule difference."""

from math import floor


def passenger_delay_description(delay_seconds: float | None) -> str:
    """Round magnitudes >= 60 seconds to the nearest minute, with halves up.

    This formats a display value only; callers retain the original seconds/status.
    Sub-minute late estimates receive neutral wording, never an on-time guarantee.
    """
    if delay_seconds is None:
        return "Timing comparison unavailable: a scheduled time or real-time estimate is missing."
    if delay_seconds == 0:
        return "The estimate matches the schedule; this is not a guarantee of on-time operation."
    magnitude = abs(delay_seconds)
    if magnitude < 60:
        return ("Slightly early (estimated)." if delay_seconds < 0
                else "Estimate is close to the schedule; timing may change.")
    minutes = floor(magnitude / 60 + 0.5)
    unit = "minute" if minutes == 1 else "minutes"
    status = "early" if delay_seconds < 0 else "late"
    return f"About {minutes} {unit} {status} (estimated)."
