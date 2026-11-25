"""Bounded train departure search using explicit run stopping-pattern evidence."""

from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from departure_presentation import passenger_delay_description
from departure_service import departure_delay, get_departures_with_disruptions, interpret_delay
from models import (Departure, DestinationDepartureBoard, DestinationDepartureCandidate,
                    DestinationSearchMetadata, DestinationVerification)
from ptv import PTVConfig, PTVRequestError, get_stopping_pattern
from stopping_pattern_service import verify_destination


MAX_RESULTS = 10
MAX_CANDIDATES = 50
MAX_PATTERN_REQUESTS = 20


def _bounded_integer(name, value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")


def _now(clock):
    value = clock() if clock is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def best_departure_time(departure: Departure) -> tuple[datetime | None, str]:
    """Prefer the validated estimate even if it is earlier than the schedule."""
    if departure.estimated_departure_utc is not None:
        return departure.estimated_departure_utc, "estimated"
    if departure.scheduled_departure_utc is not None:
        return departure.scheduled_departure_utc, "scheduled"
    return None, "unavailable"


def get_departures_to_destination(origin_stop_id: int, destination_stop_id: int, limit: int = 3, *,
                                  candidate_limit: int = 20, max_pattern_requests: int = 10,
                                  config: PTVConfig | None = None,
                                  transport: httpx.BaseTransport | None = None,
                                  clock: Callable[[], datetime] | None = None) -> DestinationDepartureBoard:
    """Return confirmed upcoming matches within one bounded origin departure board.

    All candidates/outcomes remain inspectable. Unknowns are never non-matches.
    Patterns (including failed lookups) are cached only within this call by run,
    mode and full scheduled UTC datetime, including its service date/occurrence.
    The final clock check removes departures that became past during retrieval.
    Source-board failures are distinct from a successful empty search.
    """
    for name, value in (("origin_stop_id", origin_stop_id), ("destination_stop_id", destination_stop_id)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    _bounded_integer("limit", limit, 1, MAX_RESULTS)
    _bounded_integer("candidate_limit", candidate_limit, 1, MAX_CANDIDATES)
    _bounded_integer("max_pattern_requests", max_pattern_requests, 0, MAX_PATTERN_REQUESTS)
    started = _now(clock)
    config = config or PTVConfig.from_env()
    metadata = DestinationSearchMetadata(candidate_limit=candidate_limit, max_pattern_requests=max_pattern_requests)

    def checked_time():
        checked = _now(clock)
        if checked < started:
            raise ValueError("clock must not move backwards during the search")
        return checked

    def unknown(departure, reason):
        return DestinationVerification(status="unknown", reason=reason, run_ref=departure.run_ref,
                                       boarding_stop_id=origin_stop_id, destination_stop_id=destination_stop_id)

    try:
        board = get_departures_with_disruptions(origin_stop_id, max_results=candidate_limit,
                                                config=config, transport=transport)
    except PTVRequestError:
        return DestinationDepartureBoard(origin_stop_id=origin_stop_id, destination_stop_id=destination_stop_id,
                                         requested_count=limit, started_at_utc=started, checked_at_utc=checked_time(),
                                         status="unavailable", reason="departure_board_unavailable", metadata=metadata)
    metadata.candidates_received = len(board.departures)
    metadata.candidate_limit_reached = len(board.departures) >= candidate_limit
    candidates = []
    for entry in board.departures[:candidate_limit]:
        departure = entry.departure
        best, source = best_departure_time(departure)
        delay = departure_delay(departure)
        seconds = delay.total_seconds() if delay is not None else None
        candidates.append(DestinationDepartureCandidate(
            **entry.model_dump(), best_departure_utc=best, time_source=source,
            upcoming_status="unknown" if best is None else "past" if best < started else "upcoming",
            verification=unknown(departure, "not_checked"), delay_seconds=seconds,
            **interpret_delay(seconds), passenger_delay_description=passenger_delay_description(seconds)))
    # Stable ties retain source order; missing times remain last and unverifiable.
    candidates.sort(key=lambda row: (row.best_departure_utc is None, row.best_departure_utc or started))
    cache = {}
    for row in candidates:
        departure = row.departure
        metadata.candidates_examined += 1
        if row.upcoming_status == "past":
            row.exclusion_reason = "past_departure"
            row.verification = unknown(departure, "not_checked_past_departure")
            continue
        if row.upcoming_status == "unknown":
            row.verification = unknown(departure, "missing_departure_time")
            continue
        if departure.stop_id != origin_stop_id:
            row.verification = unknown(departure, "boarding_stop_mismatch")
            continue
        if not departure.run_ref or not departure.run_ref.strip() or departure.scheduled_departure_utc is None:
            row.verification = unknown(departure, "missing_boarding_details")
            continue
        key = (departure.run_ref, 0, departure.scheduled_departure_utc)
        if key in cache:
            metadata.pattern_cache_hits += 1
        elif metadata.pattern_requests >= max_pattern_requests:
            metadata.pattern_budget_skips += 1
            row.verification = unknown(departure, "pattern_request_limit")
            continue
        else:
            # Failed attempts also consume the budget and are cached, not retried.
            metadata.pattern_requests += 1
            try:
                cache[key] = get_stopping_pattern(departure.run_ref, date_utc=departure.scheduled_departure_utc,
                                                  config=config, transport=transport)
                metadata.patterns_fetched += 1
            except (PTVRequestError, ValueError):
                cache[key] = None
        row.verification = (unknown(departure, "pattern_unavailable") if cache[key] is None else
                            verify_destination(cache[key], origin_stop_id, destination_stop_id, departure=departure))
    checked = checked_time()
    for row in candidates:
        if row.best_departure_utc is not None and row.best_departure_utc < checked:
            row.upcoming_status = "past"
            row.exclusion_reason = "past_departure"

    # A duplicated API record must not be presented as two independent trains.
    # Conflicting records for the same origin/run/instant remain unknown.
    occurrences = {}
    for row in candidates:
        departure = row.departure
        if departure.run_ref and departure.scheduled_departure_utc is not None:
            key = (departure.stop_id, departure.run_ref, departure.scheduled_departure_utc)
            occurrences.setdefault(key, []).append(row)
    for rows in occurrences.values():
        if any(row.departure != rows[0].departure for row in rows[1:]):
            for row in rows:
                row.verification = unknown(row.departure, "conflicting_duplicate_departures")
        else:
            for row in rows[1:]:
                if row.exclusion_reason is None:
                    row.exclusion_reason = "duplicate_departure"
    eligible = [row for row in candidates if row.exclusion_reason is None]
    matches = [row for row in eligible if row.verification.status == "served" and row.upcoming_status == "upcoming"]
    metadata.confirmed_matches = len(matches)
    metadata.confirmed_non_matches = sum(row.verification.status == "not_served" for row in eligible)
    metadata.unknown_verifications = sum(row.verification.status == "unknown" for row in eligible)
    metadata.past_excluded = sum(row.exclusion_reason == "past_departure" for row in candidates)
    metadata.duplicates_excluded = sum(row.exclusion_reason == "duplicate_departure" for row in candidates)
    metadata.matches_returned = min(len(matches), limit)
    metadata.pattern_request_limit_reached = (metadata.pattern_budget_skips > 0
                                             or metadata.pattern_requests > 0 and metadata.pattern_requests >= max_pattern_requests)
    metadata.search_limit_reached = metadata.candidate_limit_reached or metadata.pattern_budget_skips > 0
    if len(matches) >= limit:
        reason = "requested_matches_found"
    elif matches:
        reason = "fewer_confirmed_matches_within_search"
    elif metadata.unknown_verifications:
        reason = "no_confirmed_matches_unverifiable"
    elif not candidates:
        reason = "no_candidates_returned"
    elif not eligible:
        reason = "no_upcoming_candidates"
    else:
        reason = "no_confirmed_matches_within_search"
    return DestinationDepartureBoard(
        origin_stop_id=origin_stop_id, destination_stop_id=destination_stop_id, requested_count=limit,
        started_at_utc=started, checked_at_utc=checked,
        status="partial" if metadata.unknown_verifications or metadata.search_limit_reached else "ok",
        reason=reason, matches=matches[:limit], candidates=candidates, metadata=metadata,
        contextual_notices=board.contextual_notices, route_notices=board.route_notices)
