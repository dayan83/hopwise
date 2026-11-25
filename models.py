"""Validated PTV departures, disruptions and stopping-pattern evidence."""

from datetime import datetime, timezone
from typing import Annotated, Literal

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


StopIdentifier = Annotated[int, Field(strict=True, gt=0)]


class PatternStop(APIModel):
    stop_id: StopIdentifier | None = None
    stop_name: str | None = None
    stop_suburb: str | None = None
    route_type: Identifier | None = None
    # Physical route order is distinct from a run's departure_sequence.
    stop_sequence: Identifier | None = None


class PatternDeparture(Departure):
    stop_id: StopIdentifier | None = None
    departure_sequence: Identifier | None = None
    skipped_stops: list[PatternStop] | None = None
    flags: str | None = None


class PatternRun(APIModel):
    run_ref: str | None = None
    route_id: Identifier | None = None
    route_type: Identifier | None = None
    direction_id: Identifier | None = None
    final_stop_id: StopIdentifier | None = None


class StoppingPattern(APIModel):
    # Requested scope; missing fields on returned records are never filled from it.
    run_ref: str
    route_type: Identifier
    date_utc: OptionalTimestamp = None
    departures: list[PatternDeparture] | None = None
    stops: dict[StopIdentifier, PatternStop] | None = None
    run: PatternRun | None = None
    health: Identifier | None = None


class DestinationVerification(APIModel):
    status: Literal["served", "not_served", "unknown"]
    reason: str
    run_ref: str | None = None
    boarding_stop_id: Identifier | None = None
    destination_stop_id: StopIdentifier
    boarding_sequence: Identifier | None = None
    destination_sequence: Identifier | None = None
    skipped_after_sequence: Identifier | None = None


class DestinationDepartureCandidate(DepartureWithDisruptions):
    verification: DestinationVerification
    best_departure_utc: OptionalTimestamp = None
    time_source: Literal["estimated", "scheduled", "unavailable"]
    upcoming_status: Literal["upcoming", "past", "unknown"]
    exclusion_reason: Literal["past_departure", "duplicate_departure"] | None = None
    delay_seconds: float | None = None
    delay_status: str
    delay_description: str
    passenger_delay_description: str


class DestinationSearchMetadata(APIModel):
    candidate_limit: Identifier
    max_pattern_requests: Identifier
    # None means the source board was unavailable, not a successful empty board.
    candidates_received: Identifier | None = None
    candidates_examined: Identifier = 0
    pattern_requests: Identifier = 0
    patterns_fetched: Identifier = 0
    pattern_cache_hits: Identifier = 0
    pattern_budget_skips: Identifier = 0
    confirmed_matches: Identifier = 0
    confirmed_non_matches: Identifier = 0
    unknown_verifications: Identifier = 0
    past_excluded: Identifier = 0
    duplicates_excluded: Identifier = 0
    matches_returned: Identifier = 0
    candidate_limit_reached: bool = False
    pattern_request_limit_reached: bool = False
    search_limit_reached: bool = False


class DestinationDepartureBoard(APIModel):
    origin_stop_id: StopIdentifier
    destination_stop_id: StopIdentifier
    requested_count: Identifier
    started_at_utc: Annotated[AwareDatetime, AfterValidator(_utc)]
    checked_at_utc: Annotated[AwareDatetime, AfterValidator(_utc)]
    status: Literal["ok", "partial", "unavailable"]
    reason: str
    matches: list[DestinationDepartureCandidate] = Field(default_factory=list)
    candidates: list[DestinationDepartureCandidate] = Field(default_factory=list)
    metadata: DestinationSearchMetadata
    contextual_notices: list[Disruption] = Field(default_factory=list)
    route_notices: dict[Identifier, list[Disruption]] = Field(default_factory=dict)
