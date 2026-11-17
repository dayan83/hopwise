"""Two validated adapters exposing existing Hopwise functions to the LLM."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

import departure_service
import ptv
from models import DepartureBoard, Disruption


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


def _notice_output(notice: Disruption, melbourne: ZoneInfo) -> dict:
    result = notice.model_dump(mode="json")
    for name in ("from_date", "to_date", "last_updated"):
        result[name + "_local"] = _local(getattr(notice, name), melbourne)
    return result


def _board_output(board: DepartureBoard) -> dict:
    melbourne = ZoneInfo("Australia/Melbourne")
    result = board.model_dump(mode="json")
    for item, entry in zip(result["departures"], board.departures, strict=True):
        departure = entry.departure
        item["departure"]["scheduled_local"] = _local(departure.scheduled_departure_utc, melbourne)
        item["departure"]["estimated_local"] = _local(departure.estimated_departure_utc, melbourne)
        delay = departure_service.departure_delay(departure)
        item["departure"]["delay_seconds"] = delay.total_seconds() if delay is not None else None
        item["associated_disruptions"] = [_notice_output(notice, melbourne)
                                           for notice in entry.associated_disruptions]
    result["contextual_notices"] = [_notice_output(notice, melbourne) for notice in board.contextual_notices]
    result["route_notices"] = {str(route_id): [_notice_output(notice, melbourne) for notice in notices]
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


def execute_tool(name: str, arguments_json: str, context: ToolContext) -> dict:
    """Validate and execute an allowlisted tool, returning safe JSON-compatible data."""
    if not isinstance(name, str) or name not in _TOOLS:
        return _error("unknown_tool", "That tool is not available. Use one of the two declared tools.")
    arguments_model, handler, _ = _TOOLS[name]
    try:
        arguments = arguments_model.model_validate_json(arguments_json)
    except ValidationError:
        return _error("invalid_arguments", "Arguments must follow the tool schema: a station name, or a positive stop_id and integer limit from 1 to 10.")
    try:
        return handler(arguments, context)
    except (ptv.PTVRequestError, ValueError, ZoneInfoNotFoundError):
        # Never forward exception text, which may contain credentials or signed URLs.
        return _error("data_unavailable", "PTV data could not be retrieved. Check server configuration or try again later.")
