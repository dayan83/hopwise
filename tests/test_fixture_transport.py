from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import traceback
from zoneinfo import ZoneInfo

import httpx
import pytest

import agent_tools
from agent_tools import ToolContext, execute_tool
from evals.fixture_transport import FixtureMiss, FixtureTransport
from ptv import PTVConfig, PTVRequestError, get_departures_response, search_stations


EVALS = Path(__file__).resolve().parents[1] / "evals"
CASES = json.loads((EVALS / "dataset.json").read_text(encoding="utf-8"))["cases"]


@pytest.fixture(autouse=True)
def no_network_or_real_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Fixture tests must not use a network transport or environment credentials")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(PTVConfig, "from_env", forbidden)


@pytest.fixture
def fixture_data():
    return json.loads((EVALS / "fixtures/richmond_departures.json").read_text(encoding="utf-8"))


def request_for(entry, *, query=None, method=None, path=None):
    spec = entry["request"]
    return httpx.Request(method or spec["method"], "https://ptv-fixture.invalid" + (path or spec["path"]),
                         params=spec["query"] if query is None else query)


def test_matches_fixture_and_returns_exact_fresh_body(fixture_data):
    fixture = FixtureTransport(fixture_data)
    assert isinstance(fixture, httpx.MockTransport)
    entry = fixture_data["responses"][0]
    response = fixture.handle_request(request_for(entry))
    assert response.status_code == entry["response"]["status_code"]
    assert response.json() == entry["response"]["body"]
    response.json()["stops"].clear()
    assert fixture.handle_request(request_for(entry)).json() == entry["response"]["body"]
    assert fixture.match_counts == (2, 0)
    assert fixture.miss_count == 0


def test_query_and_expansion_order_do_not_matter(fixture_data):
    fixture = FixtureTransport(fixture_data)
    entry = fixture_data["responses"][1]
    query = [("expand", "3"), ("max_results", "5"), ("expand", "5"), ("expand", "2")]
    assert fixture.handle_request(request_for(entry, query=query)).json() == entry["response"]["body"]
    assert fixture.match_counts == (0, 1)


def test_auth_parameters_do_not_affect_matching_or_unused_descriptions(fixture_data):
    entry = fixture_data["responses"][0]
    entry["request"]["query"].update(devid="fixture-placeholder", signature="fixture-placeholder")
    fixture = FixtureTransport(fixture_data)
    assert "devid" not in fixture.unused_requests[0]["query"]
    assert "signature" not in fixture.unused_requests[0]["query"]
    query = {**entry["request"]["query"], "devid": "different-developer", "signature": "different-signature"}
    assert fixture.handle_request(request_for(entry, query=query)).json() == entry["response"]["body"]


@pytest.mark.parametrize("query", [
    {"max_results": "1", "expand": ["5", "2", "3"]},
    {"max_results": "5", "expand": ["5", "2"]},
    {"max_results": "5"},
    {"max_results": "5", "expand": ["5", "2", "3"], "include_cancelled": "true"},
    {"max_results": ["5", "5"], "expand": ["5", "2", "3"]},
])
def test_semantic_parameter_mismatches_fail_closed(fixture_data, query):
    fixture = FixtureTransport(fixture_data)
    with pytest.raises(FixtureMiss, match="fixture_miss") as caught:
        fixture.handle_request(request_for(fixture_data["responses"][1], query=query))
    assert caught.value.code == "fixture_miss"
    assert fixture.match_counts == (0, 0)
    assert fixture.miss_count == 1


@pytest.mark.parametrize("method,path", [("POST", "/v3/search/Richmond"), ("GET", "/v3/route_types")])
def test_unexpected_method_or_path_never_uses_network(fixture_data, method, path):
    fixture = FixtureTransport(fixture_data)
    with httpx.Client(transport=fixture) as client, pytest.raises(FixtureMiss, match="fixture_miss"):
        client.request(method, "https://ptv-fixture.invalid" + path,
                       params=fixture_data["responses"][0]["request"]["query"])
    assert fixture.miss_count == 1


def test_same_path_with_different_queries_selects_different_responses(fixture_data):
    one = deepcopy(fixture_data["responses"][1])
    one["request"]["query"]["max_results"] = "1"
    one["response"]["body"]["departures"] = one["response"]["body"]["departures"][:1]
    fixture_data["responses"].append(one)
    fixture = FixtureTransport(fixture_data)
    for limit, count in [(1, 1), (5, 3)]:
        departures = get_departures_response(1101, max_results=limit, config=fixture.config, transport=fixture)
        assert len(departures.departures) == count
    assert fixture.match_counts == (0, 1, 1)


def test_unused_requests_are_explicit_and_repeated_requests_are_reusable(fixture_data):
    fixture = FixtureTransport(fixture_data)
    search_stations("Richmond", fixture.config, transport=fixture)
    assert fixture.unused_requests == [{"response_index": 2, **fixture_data["responses"][1]["request"]}]
    with pytest.raises(AssertionError, match="fixture_unused"):
        fixture.assert_all_used()
    get_departures_response(1101, config=fixture.config, transport=fixture)
    fixture.assert_all_used()
    assert fixture.unused_requests == []
    assert fixture.match_counts == (1, 1)


def test_misses_remain_fixture_errors_through_existing_tool_adapter(fixture_data):
    fixture = FixtureTransport(fixture_data)
    context = ToolContext(config=fixture.config, transport=fixture, known_stop_ids={1101})
    with pytest.raises(FixtureMiss, match="fixture_miss"):
        execute_tool("get_departures_with_disruptions", '{"stop_id":1101,"limit":1}', context)
    assert fixture.miss_count == 1


def test_fixture_errors_and_logs_do_not_expose_credentials(fixture_data, caplog):
    fixture = FixtureTransport(fixture_data)
    config = PTVConfig("private-developer", "private-api-key", "https://ptv-fixture.invalid")
    query = "private-unexpected-station"
    with caplog.at_level("DEBUG"):
        search_stations("Richmond", config, transport=fixture)
        with pytest.raises(FixtureMiss) as caught:
            search_stations(query, config, transport=fixture)
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    for value in (config.dev_id, config.api_key, query, "signature=", "https://"):
        assert value not in rendered
    assert not hasattr(caught.value, "request")


def test_fixture_status_code_uses_existing_ptv_http_error_handling(fixture_data, caplog):
    fixture_data["responses"][0]["response"] = {"status_code": 503, "body": {"error": "private-diagnostics"}}
    fixture = FixtureTransport(fixture_data)
    with caplog.at_level("DEBUG"), pytest.raises(PTVRequestError, match="HTTP 503") as caught:
        search_stations("Richmond", fixture.config, transport=fixture)
    assert "private-diagnostics" not in str(caught.value) + caplog.text
    assert fixture.match_counts == (1, 0)


def test_duplicate_semantic_request_definitions_are_rejected(fixture_data):
    duplicate = deepcopy(fixture_data["responses"][1])
    duplicate["request"]["query"]["expand"] = ["3", "2", "5"]
    fixture_data["responses"].append(duplicate)
    with pytest.raises(ValueError, match="invalid_fixture"):
        FixtureTransport(fixture_data)


@pytest.mark.parametrize("change", ["naive_time", "invalid_query", "invalid_path"])
def test_invalid_fixture_values_raise_safe_errors(fixture_data, change):
    if change == "naive_time":
        fixture_data["as_of_utc"] = "2026-11-17T22:55:00"
    elif change == "invalid_query":
        fixture_data["responses"][0]["request"]["query"] = {"private-key": 123}
    else:
        fixture_data["responses"][0]["request"]["path"] = "https://private.example/?signature=private-value"
    with pytest.raises(ValueError, match="invalid_fixture") as caught:
        FixtureTransport(fixture_data)
    assert "private" not in "".join(traceback.format_exception(caught.value))


def test_invalid_json_load_does_not_expose_file_contents(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"secret": "private-value",', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid_fixture") as caught:
        FixtureTransport.from_file(path)
    assert "private-value" not in "".join(traceback.format_exception(caught.value))


def test_reference_time_is_nested_context_local_and_restored_after_failure(fixture_data):
    outer = FixtureTransport(fixture_data)
    other_data = deepcopy(fixture_data)
    other_data["as_of_utc"] = "2026-11-17T23:00:00Z"
    inner = FixtureTransport(other_data)
    original = agent_tools.datetime
    with pytest.raises(RuntimeError, match="test exit"):
        with outer.reference_time():
            assert agent_tools.datetime.now(timezone.utc) == outer.as_of_utc
            with inner.reference_time():
                assert agent_tools.datetime.now(timezone.utc) == inner.as_of_utc
            assert agent_tools.datetime.now(timezone.utc) == outer.as_of_utc
            before = datetime.now(timezone.utc)
            with ThreadPoolExecutor(max_workers=1) as pool:
                unrelated = pool.submit(agent_tools.datetime.now, timezone.utc).result(timeout=5)
            assert before <= unrelated <= datetime.now(timezone.utc)
            assert unrelated != outer.as_of_utc
            raise RuntimeError("test exit")
    assert agent_tools.datetime is original


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
def test_all_existing_scenarios_through_real_ptv_and_service_interfaces(case):
    fixture = FixtureTransport.from_file(EVALS / case["fixture"])
    assert fixture.scenario_id == case["case_id"]
    context = ToolContext(config=fixture.config, transport=fixture)
    original_clock = agent_tools.datetime
    with fixture.reference_time():
        results = [execute_tool(call["name"], json.dumps(call["arguments"]), context)
                   for call in case["expected_calls"]]
    assert agent_tools.datetime is original_clock
    assert all(result["ok"] for result in results)
    assert len(results[0]["stations"]) == case["expected_facts"]["station_match_count"]
    if len(results) == 2:
        assert results[1]["retrieved_at_utc"] == fixture.as_of_utc.isoformat()
        board = results[1]["board"]
        expected = case["expected_facts"]
        assert board["stop_id"] == expected["selected_stop_id"]
        assert len(board["departures"]) == expected["departure_count"]
        for actual, facts in zip(board["departures"], expected["departures"], strict=True):
            assert all(actual["departure"][key] == value for key, value in facts.items())
        notices = {notice["disruption_id"]: notice for entry in board["departures"]
                   for notice in entry["associated_disruptions"]}
        assert sorted(notices) == expected["associated_notice_ids"]
        for facts in expected.get("notices", []):
            assert notices[facts["disruption_id"]]["relevance"] == facts["relevance"]
    else:
        assert context.known_stop_ids == {station["stop_id"] for station in results[0]["stations"]}
        assert case["expected_facts"]["departure_data_retrieved"] is False
    fixture.assert_all_used()
    assert fixture.match_counts == (1,) * len(fixture.match_counts)
    assert fixture.miss_count == 0


def test_ambiguous_search_has_no_unique_match_or_departure_fixture():
    case = next(case for case in CASES if case["case_id"] == "ambiguous_rich_station")
    fixture = FixtureTransport.from_file(EVALS / case["fixture"])
    context = ToolContext(config=fixture.config, transport=fixture)
    result = execute_tool("search_stations", '{"query":"Rich"}', context)
    assert {(station["stop_id"], station["stop_name"]) for station in result["stations"]} == {
        (1101, "Richmond"), (1105, "East Richmond"), (1104, "West Richmond")}
    assert not any(station["stop_name"].casefold() == "rich" for station in result["stations"])
    assert case["expected_calls"] == [{"name": "search_stations", "arguments": {"query": "Rich"}}]
    assert case["expected_facts"]["outcome"] == "clarification_required"
    assert case["prohibited_calls"] == ["get_departures_with_disruptions"]
    fixture.assert_all_used()
    # A future model that arbitrarily chooses even a known stop must fail closed.
    with pytest.raises(FixtureMiss, match="fixture_miss"):
        execute_tool("get_departures_with_disruptions", '{"stop_id":1101,"limit":5}', context)
    assert fixture.match_counts == (1,)
    assert fixture.miss_count == 1


def test_explicit_three_fixture_rejects_default_five_limit():
    fixture = FixtureTransport.from_file(EVALS / "fixtures/richmond_next_three.json")
    with pytest.raises(FixtureMiss, match="fixture_miss"):
        get_departures_response(1101, max_results=5, config=fixture.config, transport=fixture)
    assert fixture.match_counts == (0, 0)
    assert fixture.miss_count == 1
    response = get_departures_response(1101, max_results=3, config=fixture.config, transport=fixture)
    assert [departure.run_ref for departure in response.departures] == ["310007", "310008", "310009"]
    assert fixture.match_counts == (0, 1)


def test_dataset_ids_and_golden_times_match_literal_ptv_records():
    """Check authored labels with stdlib arithmetic, without production delay helpers."""
    stations_by_id, routes_by_id, runs_by_ref = {}, {}, {}
    assert len({case["case_id"] for case in CASES}) == len(CASES)
    for case in CASES:
        data = json.loads((EVALS / case["fixture"]).read_text(encoding="utf-8"))
        facts = case["expected_facts"]
        assert data["synthetic"] is True
        assert data["scenario_id"] == case["case_id"]
        assert data["as_of_utc"] == case["as_of_utc"]
        reference = datetime.fromisoformat(data["as_of_utc"])
        assert reference.utcoffset().total_seconds() == 0
        stations = data["responses"][0]["response"]["body"]["stops"]
        assert len(stations) == len({station["stop_id"] for station in stations}) == facts["station_match_count"]
        for station in stations:
            assert station["route_type"] == 0
            assert stations_by_id.setdefault(station["stop_id"], station) == station
        if len(case["expected_calls"]) == 1:
            assert len(data["responses"]) == 1
            continue
        request = data["responses"][1]["request"]
        body = data["responses"][1]["response"]["body"]
        arguments = case["expected_calls"][1]["arguments"]
        stop_id = arguments["stop_id"]
        assert stop_id in {station["stop_id"] for station in stations}
        assert request["path"] == f"/v3/departures/route_type/0/stop/{stop_id}"
        assert request["query"]["max_results"] == str(arguments["limit"])
        assert len(body["departures"]) == facts["departure_count"] <= arguments["limit"]
        for record, golden in zip(body["departures"], facts["departures"], strict=True):
            assert record["stop_id"] == stop_id == golden["stop_id"]
            route = body["routes"][str(record["route_id"])]
            run = body["runs"][record["run_ref"]]
            assert route["route_id"] == run["route_id"] == record["route_id"] == golden["route_id"]
            assert record["direction_id"] == run["direction_id"]
            assert run["run_ref"] == record["run_ref"] == golden["run_ref"]
            assert route["route_type"] == run["route_type"] == 0
            assert route["route_name"] == golden["route_name"]
            assert run["destination_name"] == golden["destination_name"]
            assert record["platform_number"] == golden["platform_number"]
            assert routes_by_id.setdefault(route["route_id"], route) == route
            assert record["run_ref"] not in runs_by_ref
            runs_by_ref[record["run_ref"]] = run
            assert record["disruption_ids"] == golden["disruption_ids"]
            assert all(body["disruptions"][str(key)]["disruption_id"] == key for key in record["disruption_ids"])
            for key, local_key in [("scheduled_departure_utc", "scheduled_local"),
                                   ("estimated_departure_utc", "estimated_local")]:
                assert record[key] == golden[key]
                instant = datetime.fromisoformat(record[key]) if record[key] else None
                if instant is None:
                    assert golden[local_key] is None
                else:
                    assert instant.utcoffset().total_seconds() == 0
                    assert instant > reference
                    assert instant.astimezone(ZoneInfo("Australia/Melbourne")).strftime("%Y-%m-%d %H:%M:%S %Z") == golden[local_key]
            scheduled = datetime.fromisoformat(record["scheduled_departure_utc"])
            estimated = datetime.fromisoformat(record["estimated_departure_utc"]) if record["estimated_departure_utc"] else None
            assert golden["delay_seconds"] == ((estimated - scheduled).total_seconds() if estimated else None)
