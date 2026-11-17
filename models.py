"""Structured data for departure/disruption integration."""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field


Identifier = Annotated[int, Field(strict=True, ge=0)]


def _timestamp_input(value):
    if value is None or value == "":
        return None
    if not isinstance(value, (str, datetime)):
        raise ValueError("Expected an ISO 8601 timestamp or aware datetime")
    return value


def _utc(value: datetime | None) -> datetime | None:
    return value.astimezone(timezone.utc) if value is not None else None


OptionalTimestamp = Annotated[
    AwareDatetime | None, BeforeValidator(_timestamp_input), AfterValidator(_utc)
]


class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class Departure(APIModel):
    stop_id: Identifier | None = None
    route_id: Identifier | None = None
    route_name: str | None = None
    run_ref: str | None = None
    destination_name: str | None = None
    direction_id: Identifier | None = None
    scheduled_departure_utc: OptionalTimestamp = None
    estimated_departure_utc: OptionalTimestamp = None
    platform_number: str | None = None
    # None means association information was omitted; [] means no IDs were listed.
    disruption_ids: list[Identifier] | None = None


class Disruption(APIModel):
    disruption_id: Identifier | None = None
    title: str | None = None
    description: str | None = None
    disruption_status: str | None = None
    disruption_type: str | None = None
    from_date: OptionalTimestamp = None
    to_date: OptionalTimestamp = None
    last_updated: OptionalTimestamp = None
    routes: list[dict] | None = None
    stops: list[dict] | None = None
    url: str | None = None
    source_groups: list[str] = Field(default_factory=list)


class DeparturesResponse(APIModel):
    departures: list[Departure] = Field(default_factory=list)
    disruptions: list[Disruption] = Field(default_factory=list)


class DepartureWithDisruptions(APIModel):
    departure: Departure
    associated_disruptions: list[Disruption] = Field(default_factory=list)
    unresolved_disruption_ids: list[Identifier] = Field(default_factory=list)


class DepartureBoard(APIModel):
    stop_id: Identifier
    departures: list[DepartureWithDisruptions] = Field(default_factory=list)
    # Unlinked records from expansion have stop/route context, not a trip association.
    contextual_notices: list[Disruption] = Field(default_factory=list)
    route_notices: dict[Identifier, list[Disruption]] = Field(default_factory=dict)
