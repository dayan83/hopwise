"""Associate departures with notices using PTV's explicit disruption IDs."""

from datetime import timedelta

import httpx
from pydantic import ValidationError

from models import Departure, DepartureBoard, DepartureWithDisruptions, Disruption
from ptv import PTVConfig, PTVRequestError, get_departures_response, get_disruptions_for_route


def departure_delay(departure: Departure) -> timedelta | None:
    """Signed estimate minus schedule; None means no valid comparison exists.

    Zero requires an estimate equal to the schedule; a negative value is early.
    Models validate and normalize both timestamps to aware UTC datetimes.
    """
    if departure.estimated_departure_utc is None or departure.scheduled_departure_utc is None:
        return None
    return departure.estimated_departure_utc - departure.scheduled_departure_utc


def interpret_delay(delay_seconds: float | None) -> dict[str, str]:
    """Describe an estimate-versus-schedule comparison, not observed operation."""
    if delay_seconds is None:
        return {
            "delay_status": "unknown",
            "delay_description": "Timing comparison unavailable: a scheduled time or real-time estimate is missing.",
        }
    if delay_seconds == 0:
        return {
            "delay_status": "matches_schedule",
            "delay_description": "The estimate matches the schedule; this is not a guarantee of on-time operation.",
        }
    status = "late" if delay_seconds > 0 else "early"
    magnitude = abs(delay_seconds)
    unit = "second" if magnitude == 1 else "seconds"
    return {
        "delay_status": status,
        "delay_description": f"Estimated {magnitude:g} {unit} {status} relative to the schedule.",
    }


def assess_disruption_relevance(notice: Disruption, boarding_stop_id: int,
                                departure: Departure | None = None) -> dict:
    """Annotate explicit date/stop evidence without changing association or impact.

    Both date bounds are required; comparisons include the boundary instants.
    An estimate takes precedence over the schedule. A contextual notice without
    a departure reference has unknown temporal scope. Descriptions are not parsed.
    """
    reference = None
    source = None
    if departure is not None:
        if departure.estimated_departure_utc is not None:
            reference = departure.estimated_departure_utc
            source = "estimated"
        elif departure.scheduled_departure_utc is not None:
            reference = departure.scheduled_departure_utc
            source = "scheduled"

    temporal_status = "unknown"
    unknown_reason = None
    if reference is None:
        unknown_reason = "no_departure_time"
    elif notice.from_date is None or notice.to_date is None:
        unknown_reason = "missing_date_bounds"
    elif notice.from_date > notice.to_date:
        unknown_reason = "invalid_date_bounds"
    elif reference < notice.from_date:
        temporal_status = "before_window"
    elif reference > notice.to_date:
        temporal_status = "after_window"
    else:
        temporal_status = "within_window"

    stop_ids = sorted({
        stop["stop_id"] for stop in notice.stops or []
        if type(stop.get("stop_id")) is int and stop["stop_id"] > 0
    })
    location_status = ("boarding_station_listed" if boarding_stop_id in stop_ids
                       else "other_stops_listed" if stop_ids else "unknown")
    return {
        "temporal_status": temporal_status,
        "temporal_unknown_reason": unknown_reason,
        "reference_departure_utc": reference.isoformat() if reference is not None else None,
        "reference_time_source": source,
        "location_status": location_status,
        "boarding_stop_id": boarding_stop_id,
        "affected_stop_ids": stop_ids,
    }


def get_departures_with_disruptions(stop_id: int, max_results: int = 5, *,
                                   config: PTVConfig | None = None,
                                   transport: httpx.BaseTransport | None = None) -> DepartureBoard:
    """Build a train departure board without inferring delays or cancellations.

    Reuse expansion first, then fetch each necessary route at most once. A
    route match never creates a departure association; only disruption_ids do.
    API failures propagate rather than masquerading as empty disruption lists.
    """
    config = config or PTVConfig.from_env()
    response = get_departures_response(stop_id, max_results=max_results,
                                       config=config, transport=transport)
    known = {notice.disruption_id: notice for notice in response.disruptions
             if notice.disruption_id is not None}
    route_records = {}
    for departure in response.departures:
        ids = departure.disruption_ids or []
        if (all(disruption_id in known for disruption_id in ids)
                or departure.route_id is None or departure.route_id in route_records):
            continue
        records = get_disruptions_for_route(departure.route_id, config=config, transport=transport)
        try:
            notices = [Disruption.model_validate(record) for record in records]
        except ValidationError:
            raise PTVRequestError("PTV returned invalid structured disruption data") from None
        route_records[departure.route_id] = notices
        for notice in notices:
            if notice.disruption_id is not None:
                known.setdefault(notice.disruption_id, notice)

    linked_ids = {disruption_id for departure in response.departures
                  for disruption_id in departure.disruption_ids or []}
    entries = []
    for departure in response.departures:
        ids = list(dict.fromkeys(departure.disruption_ids or []))
        entries.append(DepartureWithDisruptions(
            departure=departure,
            associated_disruptions=[known[disruption_id] for disruption_id in ids if disruption_id in known],
            unresolved_disruption_ids=[disruption_id for disruption_id in ids if disruption_id not in known],
        ))
    return DepartureBoard(
        stop_id=stop_id, departures=entries,
        contextual_notices=[notice for notice in response.disruptions if notice.disruption_id not in linked_ids],
        route_notices={route_id: [notice for notice in notices if notice.disruption_id not in linked_ids]
                       for route_id, notices in route_records.items()},
    )
