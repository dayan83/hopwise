import json
from unittest.mock import Mock

import httpx
import pytest

import departure_service
import ptv
from agent_tools import TOOL_DEFINITIONS, ToolContext, execute_tool
from models import Departure, DepartureBoard, DepartureWithDisruptions, Disruption


@pytest.fixture
def context():
    return ToolContext(config=ptv.PTVConfig("fake-id", "fake-ptv-key", "https://ptv.example"))


def test_only_two_strict_tool_schemas():
    assert [tool["name"] for tool in TOOL_DEFINITIONS] == ["search_stations", "get_departures_with_disruptions"]
    for tool in TOOL_DEFINITIONS:
        assert tool["type"] == "function"
        assert tool["strict"] is True
        schema = tool["parameters"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_adapters_reuse_services_and_serialize_board(context, monkeypatch):
    search = Mock(return_value=[{"stop_id": 1, "stop_name": "Richmond", "stop_suburb": "Richmond", "route_type": 0}])
    monkeypatch.setattr(ptv, "search_stations", search)
    notice = Disruption(disruption_id=101, disruption_status="Planned", to_date=None,
                        from_date="2026-11-18T00:00:00Z", source_groups=["general"])
    board = DepartureBoard(stop_id=1, departures=[DepartureWithDisruptions(
        departure=Departure(stop_id=1, route_id=9, run_ref="A", disruption_ids=[101, 999],
                            scheduled_departure_utc="2026-11-17T00:00:00Z"),
        associated_disruptions=[notice], unresolved_disruption_ids=[999])])
    fetch = Mock(return_value=board)
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", fetch)

    stations = execute_tool("search_stations", '{"query":"  Richmond  "}', context)
    assert stations["ok"] is True
    assert context.known_stop_ids == {1}
    search.assert_called_once_with("Richmond", context.config, transport=None)
    result = execute_tool("get_departures_with_disruptions", '{"stop_id":1,"limit":5}', context)
    fetch.assert_called_once_with(1, max_results=5, config=context.config, transport=None)
    entry = result["board"]["departures"][0]
    assert entry["departure"]["scheduled_local"] == "2026-11-17 11:00:00 AEDT"
    assert entry["departure"]["estimated_local"] is None
    assert entry["departure"]["delay_seconds"] is None
    assert entry["unresolved_disruption_ids"] == [999]
    assert entry["associated_disruptions"][0]["disruption_status"] == "Planned"
    assert entry["associated_disruptions"][0]["to_date_local"] is None
    assert entry["associated_disruptions"][0]["from_date_local"] == "2026-11-18 11:00:00 AEDT"
    assert board.departures[0].departure.scheduled_departure_utc.utcoffset().total_seconds() == 0
    serialized = json.dumps(result)
    assert context.config.api_key not in serialized
    assert context.config.dev_id not in serialized


@pytest.mark.parametrize("scheduled,estimated,seconds,status,description", [
    ("2026-11-17T22:58:00Z", "2026-11-17T22:58:30Z", 30, "late",
     "Estimated 30 seconds late relative to the schedule."),
    ("2026-11-17T22:58:00Z", "2026-11-17T22:57:30Z", -30, "early",
     "Estimated 30 seconds early relative to the schedule."),
    ("2026-11-17T22:58:00Z", "2026-11-17T22:58:00Z", 0, "matches_schedule",
     "The estimate matches the schedule; this is not a guarantee of on-time operation."),
    ("2026-11-17T22:58:00Z", None, None, "unknown",
     "Timing comparison unavailable: a scheduled time or real-time estimate is missing."),
    (None, "2026-11-17T22:58:30Z", None, "unknown",
     "Timing comparison unavailable: a scheduled time or real-time estimate is missing."),
])
def test_departure_tool_supplies_timing_interpretation_without_changing_timestamps(
        context, monkeypatch, scheduled, estimated, seconds, status, description):
    context.known_stop_ids.add(1)
    board = DepartureBoard(stop_id=1, departures=[DepartureWithDisruptions(departure=Departure(
        stop_id=1, scheduled_departure_utc=scheduled, estimated_departure_utc=estimated))])
    original = board.model_dump(mode="json")
    fetch = Mock(return_value=board)
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", fetch)
    result = execute_tool("get_departures_with_disruptions", '{"stop_id":1,"limit":5}', context)
    departure = result["board"]["departures"][0]["departure"]
    assert result["ok"] is True
    assert departure["scheduled_departure_utc"] == scheduled
    assert departure["estimated_departure_utc"] == estimated
    assert departure["delay_seconds"] == seconds
    assert departure["delay_status"] == status
    assert departure["delay_description"] == description
    assert board.model_dump(mode="json") == original
    fetch.assert_called_once_with(1, max_results=5, config=context.config, transport=None)


@pytest.mark.parametrize("estimated,description", [
    ("2026-11-17T22:57:30Z",
     "Estimated departure: 2026-11-18 09:57:30 AEDT; scheduled departure: unavailable."),
    (None, "Scheduled departure: unavailable; real-time estimate unavailable."),
])
def test_time_description_handles_missing_schedule_without_inventing_times(
        context, monkeypatch, estimated, description):
    context.known_stop_ids.add(1)
    board = DepartureBoard(stop_id=1, departures=[DepartureWithDisruptions(departure=Departure(
        stop_id=1, scheduled_departure_utc=None, estimated_departure_utc=estimated))])
    original = board.model_dump(mode="json")
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", Mock(return_value=board))
    result = execute_tool("get_departures_with_disruptions", '{"stop_id":1,"limit":5}', context)
    assert result["ok"] is True
    departure = result["board"]["departures"][0]["departure"]
    assert departure["departure_time_description"] == description
    assert departure["scheduled_departure_utc"] is None
    assert departure["estimated_departure_utc"] == estimated
    assert departure["delay_seconds"] is None
    assert departure["delay_status"] == "unknown"
    assert board.model_dump(mode="json") == original


@pytest.mark.parametrize("arguments", [
    "not JSON", "[]", '{"query":"Ri"}', '{"query":"1234"}', '{"query":1}',
    '{"query":"Richmond","extra":"ignored?"}',
])
def test_invalid_search_arguments_never_call_ptv(context, monkeypatch, arguments):
    search = Mock()
    monkeypatch.setattr(ptv, "search_stations", search)
    result = execute_tool("search_stations", arguments, context)
    assert result["error"]["code"] == "invalid_arguments"
    search.assert_not_called()


@pytest.mark.parametrize("arguments", [
    {"stop_id": 0, "limit": 5}, {"stop_id": True, "limit": 5},
    {"stop_id": "1", "limit": 5}, {"stop_id": 1, "limit": 0},
    {"stop_id": 1, "limit": 11}, {"stop_id": 1, "limit": True},
    {"stop_id": 1, "limit": 1.5}, {"stop_id": 1},
    {"stop_id": 1, "limit": 5, "extra": 1},
])
def test_invalid_departure_arguments_never_call_ptv(context, monkeypatch, arguments):
    context.known_stop_ids.add(1)
    fetch = Mock()
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", fetch)
    result = execute_tool("get_departures_with_disruptions", json.dumps(arguments), context)
    assert result["error"]["code"] == "invalid_arguments"
    fetch.assert_not_called()


def test_unknown_stop_and_unknown_tool_never_execute(context, monkeypatch):
    fetch = Mock()
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", fetch)
    result = execute_tool("get_departures_with_disruptions", '{"stop_id":1,"limit":5}', context)
    assert result["error"]["code"] == "unverified_stop_id"
    assert execute_tool("os.system", '{}', context)["error"]["code"] == "unknown_tool"
    fetch.assert_not_called()


def test_raw_backend_errors_are_not_forwarded(context, monkeypatch, caplog):
    secret = "private-test-value"
    monkeypatch.setattr(ptv, "search_stations", Mock(side_effect=ptv.PTVRequestError(secret)))
    with caplog.at_level("DEBUG"):
        result = execute_tool("search_stations", '{"query":"Richmond"}', context)
    assert result["error"]["code"] == "data_unavailable"
    assert secret not in json.dumps(result) + caplog.text


def test_tools_use_real_ptv_code_with_mocked_http(context):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/v3/search/Richmond":
            return httpx.Response(200, json={"stops": [
                {"stop_id": 1, "stop_name": "Richmond", "route_type": 0}]})
        assert request.url.path == "/v3/departures/route_type/0/stop/1"
        assert request.url.params["max_results"] == "3"
        return httpx.Response(200, json={"departures": [
            {"stop_id": 1, "route_id": 9, "run_ref": "A", "disruption_ids": [101],
             "scheduled_departure_utc": "2026-11-17T00:00:00Z",
             "estimated_departure_utc": "2026-11-17T00:01:00Z"}],
            "disruptions": {"101": {"disruption_id": 101, "title": "Works", "disruption_status": "Current"}}})

    context.transport = httpx.MockTransport(handler)
    execute_tool("search_stations", '{"query":"Richmond"}', context)
    result = execute_tool("get_departures_with_disruptions", '{"stop_id":1,"limit":3}', context)
    assert result["ok"] is True
    entry = result["board"]["departures"][0]
    assert entry["departure"]["delay_seconds"] == 60
    assert entry["associated_disruptions"][0]["disruption_id"] == 101
    assert paths == ["/v3/search/Richmond", "/v3/departures/route_type/0/stop/1"]
