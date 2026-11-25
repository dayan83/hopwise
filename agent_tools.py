"""Two validated adapters exposing existing Hopwise functions to the LLM."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

import departure_service
from departure_presentation import passenger_delay_description
import ptv
from models import Departure, DepartureBoard, Disruption


class ToolArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class SearchStationsArguments(ToolArguments):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=100)]

    @field_validator("query")
    @classmethod
    def reject_numeric_names(cls, value):
        if value.isnumeric():
            raise ValueError("Use a station name, not a number")
        return value


class DeparturesArguments(ToolArguments):
    stop_id: int = Field(ge=1)
    limit: int = Field(ge=1, le=10)


@dataclass
class ToolContext:
    """Per-question provenance and server-side dependencies, never sent to the LLM."""

    known_stop_ids: set[int] = field(default_factory=set)
    config: ptv.PTVConfig | None = field(default=None, repr=False)
    transport: httpx.BaseTransport | None = field(default=None, repr=False)


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def _search_stations(arguments: SearchStationsArguments, context: ToolContext) -> dict:
    stations = ptv.search_stations(arguments.query, context.config, transport=context.transport)
    if any(type(station["stop_id"]) is not int or station["stop_id"] < 1 for station in stations):
        raise ValueError("Invalid station identifier in search response")
    context.known_stop_ids.update(station["stop_id"] for station in stations)
    return {"ok": True, "stations": stations}


def _local(value: datetime | None, melbourne: ZoneInfo) -> str | None:
    return value.astimezone(melbourne).strftime("%Y-%m-%d %H:%M:%S %Z") if value is not None else None


def _notice_output(notice: Disruption, melbourne: ZoneInfo, *, boarding_stop_id: int,
                   departure: Departure | None = None) -> dict:
    result = notice.model_dump(mode="json")
    for name in ("from_date", "to_date", "last_updated"):
        result[name + "_local"] = _local(getattr(notice, name), melbourne)
    result["relevance"] = departure_service.assess_disruption_relevance(
        notice, boarding_stop_id, departure)
    return result


def _board_output(board: DepartureBoard) -> dict:
    melbourne = ZoneInfo("Australia/Melbourne")
    result = board.model_dump(mode="json")
    for item, entry in zip(result["departures"], board.departures, strict=True):
        departure = entry.departure
        item["departure"]["scheduled_local"] = _local(departure.scheduled_departure_utc, melbourne)
        item["departure"]["estimated_local"] = _local(departure.estimated_departure_utc, melbourne)
        scheduled = item["departure"]["scheduled_local"] or "unavailable"
        estimated = item["departure"]["estimated_local"]
        item["departure"]["departure_time_description"] = (
            f"Estimated departure: {estimated}; scheduled departure: {scheduled}."
            if estimated is not None else
            f"Scheduled departure: {scheduled}; real-time estimate unavailable."
        )
        delay = departure_service.departure_delay(departure)
        item["departure"]["delay_seconds"] = delay.total_seconds() if delay is not None else None
        item["departure"].update(departure_service.interpret_delay(item["departure"]["delay_seconds"]))
        item["departure"]["passenger_delay_description"] = passenger_delay_description(
            item["departure"]["delay_seconds"])
        item["associated_disruptions"] = [
            _notice_output(notice, melbourne, boarding_stop_id=board.stop_id, departure=departure)
            for notice in entry.associated_disruptions]
    result["contextual_notices"] = [
        _notice_output(notice, melbourne, boarding_stop_id=board.stop_id)
        for notice in board.contextual_notices]
    result["route_notices"] = {
        str(route_id): [_notice_output(notice, melbourne, boarding_stop_id=board.stop_id)
                        for notice in notices]
        for route_id, notices in board.route_notices.items()}
    return result


def _get_departures(arguments: DeparturesArguments, context: ToolContext) -> dict:
    if arguments.stop_id not in context.known_stop_ids:
        return _error("unverified_stop_id", "Use a stop_id returned by a successful station search in this question.")
    board = departure_service.get_departures_with_disruptions(
        arguments.stop_id, max_results=arguments.limit, config=context.config, transport=context.transport)
    return {"ok": True, "timezone": "Australia/Melbourne", "board": _board_output(board),
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat()}


# The model can request only these two adapters; names are never evaluated as Python.
_TOOLS = MappingProxyType({
    "search_stations": (SearchStationsArguments, _search_stations,
        "Find Melbourne metropolitan train stations by name. Use the returned names, suburbs and stop IDs to select a station."),
    "get_departures_with_disruptions": (DeparturesArguments, _get_departures,
        "Get upcoming trains and ID-associated disruption notices for a stop returned by search_stations. Use limit=5 unless another count is requested."),
})

TOOL_DEFINITIONS = [
    {"type": "function", "name": name, "description": description, "strict": True,
     "parameters": arguments.model_json_schema()}
    for name, (arguments, _, description) in _TOOLS.items()
]


def execute_tool(name: str, arguments_json: str, context: ToolContext, *,
                 on_result: Callable[[dict | None, dict], None] | None = None) -> dict:
    """Validate and execute an allowlisted tool, returning safe JSON-compatible data.

    The optional observer receives validated arguments (None for rejected input)
    and the existing result. It never receives raw invalid arguments or errors.
    """
    def finish(result, arguments=None):
        if on_result is not None:
            on_result(arguments.model_dump() if arguments is not None else None, result)
        return result

    if not isinstance(name, str) or name not in _TOOLS:
        return finish(_error("unknown_tool", "That tool is not available. Use one of the two declared tools."))
    arguments_model, handler, _ = _TOOLS[name]
    try:
        arguments = arguments_model.model_validate_json(arguments_json)
    except ValidationError:
        return finish(_error("invalid_arguments", "Arguments must follow the tool schema: a station name, or a positive stop_id and integer limit from 1 to 10."))
    try:
        result = handler(arguments, context)
    except (ptv.PTVRequestError, ValueError, ZoneInfoNotFoundError):
        # Never forward exception text, which may contain credentials or signed URLs.
        result = _error("data_unavailable", "PTV data could not be retrieved. Check server configuration or try again later.")
    return finish(result, arguments)
