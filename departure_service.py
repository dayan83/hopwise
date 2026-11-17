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
