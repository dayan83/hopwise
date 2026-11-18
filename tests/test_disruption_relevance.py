import httpx
import pytest

from agent_tools import ToolContext, execute_tool
from departure_service import assess_disruption_relevance
from models import Departure, Disruption
from ptv import PTVConfig


def bounded_notice(**overrides):
    return Disruption.model_validate({
        "disruption_id": 101, "from_date": "2026-11-18T00:00:00Z",
        "to_date": "2026-11-18T02:00:00Z", **overrides,
    })


@pytest.mark.parametrize("timestamp,status", [
    ("2026-11-17T23:59:59Z", "before_window"),
    ("2026-11-18T00:00:00Z", "within_window"),
    ("2026-11-18T01:00:00Z", "within_window"),
    ("2026-11-18T02:00:00Z", "within_window"),
    ("2026-11-18T02:00:01Z", "after_window"),
])
def test_departure_is_compared_with_explicit_inclusive_date_bounds(timestamp, status):
    departure = Departure(scheduled_departure_utc=timestamp)
    result = assess_disruption_relevance(bounded_notice(), 10, departure)
    assert result["temporal_status"] == status
    assert result["temporal_unknown_reason"] is None
    assert result["reference_time_source"] == "scheduled"
    assert result["reference_departure_utc"] == departure.scheduled_departure_utc.isoformat()


@pytest.mark.parametrize("start,end,reason", [
    (None, None, "missing_date_bounds"),
    ("2026-11-18T00:00:00Z", None, "missing_date_bounds"),
    (None, "2026-11-18T02:00:00Z", "missing_date_bounds"),
    ("2026-11-18T02:00:00Z", "2026-11-18T00:00:00Z", "invalid_date_bounds"),
])
def test_incomplete_or_reversed_bounds_remain_unknown(start, end, reason):
    notice = bounded_notice(from_date=start, to_date=end)
    result = assess_disruption_relevance(notice, 10, Departure(
        scheduled_departure_utc="2026-11-18T01:00:00Z"))
    assert result["temporal_status"] == "unknown"
    assert result["temporal_unknown_reason"] == reason


@pytest.mark.parametrize("departure", [None, Departure()])
def test_missing_departure_reference_is_unknown(departure):
    result = assess_disruption_relevance(bounded_notice(), 10, departure)
    assert result["temporal_status"] == "unknown"
    assert result["temporal_unknown_reason"] == "no_departure_time"
    assert result["reference_departure_utc"] is None
    assert result["reference_time_source"] is None


@pytest.mark.parametrize("estimated,scheduled,source", [
    ("2026-11-18T12:00:00+11:00", "2026-11-17T23:00:00Z", "estimated"),
    (None, "2026-11-18T01:00:00Z", "scheduled"),
])
def test_estimate_has_precedence_and_aware_times_compare_in_utc(estimated, scheduled, source):
    result = assess_disruption_relevance(bounded_notice(), 10, Departure(
        scheduled_departure_utc=scheduled, estimated_departure_utc=estimated))
    assert result["reference_time_source"] == source
    assert result["reference_departure_utc"] == "2026-11-18T01:00:00+00:00"
    assert result["temporal_status"] == "within_window"


@pytest.mark.parametrize("stops,status,ids", [
    (None, "unknown", []),
    ([], "unknown", []),
    ([{}], "unknown", []),
    ([{"stop_name": "Flinders Street"}], "unknown", []),
    ([{"stop_id": "10"}], "unknown", []),
    ([{"stop_id": True}, {"stop_id": 0}], "unknown", []),
    ([{"stop_id": 10}], "boarding_station_listed", [10]),
    ([{"stop_id": 99}], "other_stops_listed", [99]),
    ([{"stop_id": 99}, {"stop_id": 10}, {"stop_id": 10}], "boarding_station_listed", [10, 99]),
    ([{"stop_id": 99}, {}], "other_stops_listed", [99]),
])
def test_location_uses_only_explicit_valid_stop_ids(stops, status, ids):
    result = assess_disruption_relevance(bounded_notice(stops=stops), 10)
    assert result["location_status"] == status
    assert result["affected_stop_ids"] == ids
    assert result["boarding_stop_id"] == 10


def test_descriptions_and_status_do_not_establish_recurring_windows_or_impact():
    notice = bounded_notice(
        from_date="2026-11-17T00:00:00Z", to_date="2026-11-19T00:00:00Z",
        description="Replacement buses each night from 10 pm to 4 am.",
        disruption_status="Planned")
    departure = Departure(scheduled_departure_utc="2026-11-17T23:00:00Z")  # 10 am Melbourne
    original = notice.model_dump()
    assessment = assess_disruption_relevance(notice, 10, departure)
    assert assessment["temporal_status"] == "within_window"
    assert assessment["location_status"] == "unknown"
    assert notice.model_dump() == original
    # Changing free text/status does not turn the assessment into an impact classification.
    other = notice.model_copy(update={"description": "Station car park closed.", "disruption_status": "Current"})
    assert assess_disruption_relevance(other, 10, departure) == assessment


def test_tool_annotations_preserve_shared_associations_categories_and_request_deduplication():
    paths = []
    shared = {
        "disruption_id": 101, "title": "Works at another station", "disruption_status": "Planned",
        "from_date": "2026-11-18T12:00:00Z", "to_date": "2026-11-19T12:00:00Z",
        "stops": [{"stop_id": 99}],
    }
    context_notice = {
        "disruption_id": 102, "title": "Boarding station access", "stops": [{"stop_id": 10}],
        "from_date": "2026-11-17T00:00:00Z", "to_date": "2026-11-19T00:00:00Z",
    }

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.startswith("/v3/departures/"):
            return httpx.Response(200, json={"departures": [
                {"stop_id": 10, "route_id": 9, "run_ref": "A", "disruption_ids": [101, 999],
                 "scheduled_departure_utc": "2026-11-17T23:00:00Z",
                 "estimated_departure_utc": "2026-11-17T23:00:30Z"},
                {"stop_id": 10, "route_id": 9, "run_ref": "B", "disruption_ids": [101],
                 "scheduled_departure_utc": "2026-11-18T23:00:00Z"},
            ], "disruptions": {"101": shared, "102": context_notice}})
        assert request.url.path == "/v3/disruptions/route/9"
        return httpx.Response(200, json={"disruptions": {"general": [{
            "disruption_id": 103, "title": "Other route notice", "stops": [{"stop_id": 77}],
            "from_date": "2026-11-17T00:00:00Z", "to_date": "2026-11-19T00:00:00Z",
        }]}})

    context = ToolContext(config=PTVConfig("fake-id", "fake-key", "https://ptv.example"),
                          transport=httpx.MockTransport(handler), known_stop_ids={10})
    result = execute_tool("get_departures_with_disruptions", '{"stop_id":10,"limit":2}', context)
    assert result["ok"] is True
    assert paths == ["/v3/departures/route_type/0/stop/10", "/v3/disruptions/route/9"]
    board = result["board"]
    first, second = board["departures"]
    assert first["departure"]["disruption_ids"] == [101, 999]
    assert first["unresolved_disruption_ids"] == [999]
    assert first["departure"]["scheduled_departure_utc"] == "2026-11-17T23:00:00Z"
    assert [item["disruption_id"] for item in first["associated_disruptions"]] == [101]
    assert [item["disruption_id"] for item in second["associated_disruptions"]] == [101]
    first_notice = first["associated_disruptions"][0]
    second_notice = second["associated_disruptions"][0]
    assert first_notice["disruption_status"] == second_notice["disruption_status"] == "Planned"
    assert first_notice["relevance"]["temporal_status"] == "before_window"
    assert first_notice["relevance"]["reference_time_source"] == "estimated"
    assert second_notice["relevance"]["temporal_status"] == "within_window"
    assert second_notice["relevance"]["reference_time_source"] == "scheduled"
    assert first_notice["relevance"]["location_status"] == "other_stops_listed"
    assert [item["disruption_id"] for item in board["contextual_notices"]] == [102]
    assert [item["disruption_id"] for item in board["route_notices"]["9"]] == [103]
    for notice in [board["contextual_notices"][0], board["route_notices"]["9"][0]]:
        assert notice["relevance"]["temporal_status"] == "unknown"
        assert notice["relevance"]["temporal_unknown_reason"] == "no_departure_time"
    assert board["contextual_notices"][0]["relevance"]["location_status"] == "boarding_station_listed"
    assert board["route_notices"]["9"][0]["relevance"]["location_status"] == "other_stops_listed"
