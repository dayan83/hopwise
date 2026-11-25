"""Deterministic stop-order evidence; no line-name or terminus-name inference."""

import re

import httpx

from models import Departure, DestinationVerification, StoppingPattern
from ptv import PTVConfig, PTVRequestError, get_stopping_pattern


def _stop_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError("stop_id must be a positive integer")


def verify_destination(pattern: StoppingPattern, boarding_stop_id: int, destination_stop_id: int, *,
                       departure: Departure | None = None) -> DestinationVerification:
    """Verify a stop strictly after boarding in this run's returned pattern.

    Optional departure anchors the boarding occurrence by scheduled time and IDs.
    Repeated stops/sequences are ambiguous. Negative absence/before/skip findings
    require a validated terminal and fully accounted-for stop expansion. PTV has
    no explicit completeness flag: this is evidence in the returned pattern,
    not a guarantee of operation or of the upstream feed's completeness.
    """
    _stop_id(boarding_stop_id)
    _stop_id(destination_stop_id)
    evidence = {}

    def result(status, reason):
        return DestinationVerification(status=status, reason=reason, run_ref=pattern.run_ref,
                                       boarding_stop_id=boarding_stop_id, destination_stop_id=destination_stop_id,
                                       **evidence)

    if pattern.health not in (None, 1):
        return result("unknown", "api_unhealthy")
    rows = pattern.departures
    if not rows:
        return result("unknown", "missing_pattern")
    if any(row.stop_id is None or row.departure_sequence is None or row.run_ref is None
           or row.route_id is None for row in rows):
        return result("unknown", "incomplete_pattern")
    if any(row.run_ref != pattern.run_ref for row in rows) or len({row.route_id for row in rows}) != 1:
        return result("unknown", "run_identity_mismatch")
    if len({row.direction_id for row in rows if row.direction_id is not None}) > 1:
        return result("unknown", "run_identity_mismatch")
    if len({row.departure_sequence for row in rows}) != len(rows):
        return result("unknown", "ambiguous_sequence")
    served_ids = {row.stop_id for row in rows}
    if len(served_ids) != len(rows):
        return result("unknown", "ambiguous_stop_occurrences")
    ordered = sorted(rows, key=lambda row: row.departure_sequence)
    skipped = {}
    for row in ordered:
        for stop in row.skipped_stops or []:
            if stop.stop_id is None:
                return result("unknown", "incomplete_pattern")
            if stop.route_type not in (None, pattern.route_type):
                return result("unknown", "run_identity_mismatch")
            if stop.stop_id in skipped or stop.stop_id in served_ids:
                return result("unknown", "ambiguous_stop_occurrences")
            skipped[stop.stop_id] = row.departure_sequence
    if pattern.stops is not None and any(stop.stop_id not in (None, key)
            or stop.route_type not in (None, pattern.route_type) for key, stop in pattern.stops.items()):
        return result("unknown", "run_identity_mismatch")
    run = pattern.run
    if run is not None and (run.run_ref not in (None, pattern.run_ref)
            or run.route_type not in (None, pattern.route_type)
            or run.route_id not in (None, ordered[0].route_id)
            or run.direction_id is not None and any(row.direction_id not in (None, run.direction_id) for row in ordered)):
        return result("unknown", "run_identity_mismatch")
    boarding = next((row for row in ordered if row.stop_id == boarding_stop_id), None)
    if boarding is None:
        return result("unknown", "boarding_stop_missing")
    evidence["boarding_sequence"] = boarding.departure_sequence
    if departure is not None:
        if (departure.run_ref != pattern.run_ref or departure.stop_id != boarding_stop_id
                or departure.scheduled_departure_utc is None or boarding.scheduled_departure_utc is None
                or departure.scheduled_departure_utc != boarding.scheduled_departure_utc
                or departure.route_id is not None and departure.route_id != boarding.route_id
                or departure.direction_id is not None and departure.direction_id != boarding.direction_id):
            return result("unknown", "boarding_departure_mismatch")
    if "DOO" in re.split(r"[,;\s]+", (boarding.flags or "").upper()):
        return result("unknown", "boarding_drop_off_only")
    destination = next((row for row in ordered if row.stop_id == destination_stop_id), None)
    if destination is not None:
        evidence["destination_sequence"] = destination.departure_sequence
        if destination.departure_sequence > boarding.departure_sequence:
            if "PUO" in re.split(r"[,;\s]+", (destination.flags or "").upper()):
                return result("unknown", "destination_pick_up_only")
            return result("served", "destination_after_boarding")
    # Expanded metadata alone never proves a stop. It is used here only to
    # reject a negative conclusion when endpoints or records are unaccounted for.
    accounted_ids = served_ids | set(skipped)
    complete = (run is not None and run.run_ref == pattern.run_ref and run.route_type == pattern.route_type
                and run.route_id == ordered[0].route_id and run.final_stop_id == ordered[-1].stop_id
                and not ordered[-1].skipped_stops and pattern.stops is not None
                and set(pattern.stops) == accounted_ids
                and all(stop.stop_id == key and stop.route_type == pattern.route_type for key, stop in pattern.stops.items()))
    if not complete:
        return result("unknown", "incomplete_pattern")
    if destination is not None:
        return result("not_served", "destination_not_after_boarding")
    if destination_stop_id in skipped and skipped[destination_stop_id] >= boarding.departure_sequence:
        evidence["skipped_after_sequence"] = skipped[destination_stop_id]
        return result("not_served", "destination_skipped")
    return result("not_served", "destination_absent")


def get_destination_verification(departure: Departure, destination_stop_id: int, *, route_type: int = 0,
                                 config: PTVConfig | None = None,
                                 transport: httpx.BaseTransport | None = None) -> DestinationVerification:
    """Fetch one run for an existing departure; unavailable data remains unknown."""
    _stop_id(destination_stop_id)
    if type(route_type) is not int or route_type < 0:
        raise ValueError("route_type must be a nonnegative integer")

    def unknown(reason):
        return DestinationVerification(status="unknown", reason=reason, run_ref=departure.run_ref,
                                       boarding_stop_id=departure.stop_id, destination_stop_id=destination_stop_id)

    if not departure.run_ref or not departure.stop_id or departure.scheduled_departure_utc is None:
        return unknown("missing_boarding_details")
    try:
        pattern = get_stopping_pattern(departure.run_ref, route_type, date_utc=departure.scheduled_departure_utc,
                                       config=config, transport=transport)
    except (PTVRequestError, ValueError):
        return unknown("pattern_unavailable")
    return verify_destination(pattern, departure.stop_id, destination_stop_id, departure=departure)
