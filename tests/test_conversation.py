from copy import deepcopy
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import httpx
import httpx2
import pytest

from agent import AgentError, INSTRUCTIONS, answer_question
from agent_trace import AgentTrace
from conversation import CONVERSATION_INSTRUCTIONS, ConversationState
from evals.fixture_transport import FixtureTransport
from ptv import PTVConfig


EVALS = Path(__file__).resolve().parents[1] / "evals"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Conversation tests must not use live APIs or environment credentials")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(PTVConfig, "from_env", forbidden)


def tool(name, arguments, call_id):
    return SimpleNamespace(status="completed", output_text="", output=[SimpleNamespace(
        type="function_call", name=name, arguments=json.dumps(arguments), call_id=call_id)])


def final(text="Fresh departure results."):
    return SimpleNamespace(status="completed", output_text=text, output=[])


class ScriptedClient:
    def __init__(self, responses):
        self.responses = self
        self.script = iter(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return next(self.script)


def fixture_data(case_id="richmond_departures", limit=5):
    data = json.loads((EVALS / "fixtures" / f"{case_id}.json").read_text(encoding="utf-8"))
    if len(data["responses"]) > 1:
        entry = data["responses"][1]
        entry["request"]["query"]["max_results"] = str(limit)
        if "departures" in entry["response"]["body"]:
            entry["response"]["body"]["departures"] = entry["response"]["body"]["departures"][:limit]
    return data


def run_turn(state, fixture, question, *, query="Richmond", stop_id=1101, limit=5, answer="Fresh departure results."):
    client = ScriptedClient([
        tool("search_stations", {"query": query}, "search"),
        tool("get_departures_with_disruptions", {"stop_id": stop_id, "limit": limit}, "departures"),
        final(answer),
    ])
    trace = AgentTrace()
    with fixture.reference_time():
        response = answer_question(question, client=client, model="test",
                                   ptv_config=fixture.config, ptv_transport=fixture,
                                   conversation=state, trace=trace)
    fixture.assert_all_used()
    return response, client, trace


def preferences(client):
    content = client.requests[0]["input"][0]["content"]
    return json.loads(content.split(": ", 1)[1])


def result(client):
    outputs = [item for item in client.requests[-1]["input"]
               if isinstance(item, dict) and item.get("type") == "function_call_output"]
    return json.loads(outputs[-1]["output"])


def test_follow_up_revalidates_station_fetches_two_fresh_departures_and_carries_no_old_times():
    state = ConversationState()
    first = FixtureTransport(fixture_data())
    _, first_client, _ = run_turn(state, first, "What are the next trains from Richmond?")
    assert state.preferences() == {"station_name": "Richmond", "departure_limit": 5}
    assert first.match_counts == (1, 1)

    data = fixture_data(limit=2)
    data["as_of_utc"] = "2026-11-17T22:56:00Z"
    departure = data["responses"][1]["response"]["body"]["departures"][0]
    departure.update(estimated_departure_utc="2026-11-17T22:59:00Z", platform_number="8")
    second = FixtureTransport(data)
    _, client, _ = run_turn(state, second, "What about just the next two?", limit=2)
    assert preferences(client) == {"station_name": "Richmond", "departure_limit": 5}
    assert state.preferences() == {"station_name": "Richmond", "departure_limit": 2}
    payload = result(client)
    assert len(payload["board"]["departures"]) == 2
    assert payload["board"]["departures"][0]["departure"]["platform_number"] == "8"
    assert payload["board"]["departures"][0]["departure"]["delay_seconds"] == 60
    assert payload["retrieved_at_utc"] != result(first_client)["retrieved_at_utc"]
    assert second.match_counts == (1, 1)
    initial_input = json.dumps(client.requests[0]["input"])
    for old_fact in ("scheduled_departure_utc", "310001", "09:58", "Fresh departure results.", "stop_id"):
        assert old_fact not in initial_input
    assert all(request["instructions"] == INSTRUCTIONS + CONVERSATION_INSTRUCTIONS for request in client.requests)


def test_explicit_station_change_updates_context_and_preserves_count_preference():
    state = ConversationState("Richmond", 2)
    fixture = FixtureTransport(fixture_data("flinders_street_morning_late_night_notice", limit=2))
    _, client, _ = run_turn(state, fixture, "And from Flinders Street instead?",
                            query="Flinders Street", stop_id=1102, limit=2)
    assert preferences(client) == {"station_name": "Richmond", "departure_limit": 2}
    assert state.preferences() == {"station_name": "Flinders Street", "departure_limit": 2}
    assert result(client)["board"]["stop_id"] == 1102


def test_follow_up_without_station_context_requests_clarification_without_ptv():
    state = ConversationState()
    fixture = FixtureTransport(fixture_data())
    client = ScriptedClient([final("Unsupported train at 09:58 from Richmond.")])
    answer = answer_question("What about the next two?", client=client, model="test",
                             conversation=state, ptv_config=fixture.config, ptv_transport=fixture)
    assert answer == "Which Melbourne train station would you like departures from?"
    assert "09:58" not in answer
    assert fixture.match_counts == (0, 0)
    assert state.station_name is None


def test_ambiguous_search_stops_for_clarification_without_using_old_station():
    state = ConversationState("Glen Waverley", 2)
    fixture = FixtureTransport(fixture_data("ambiguous_rich_station"))
    client = ScriptedClient([
        tool("search_stations", {"query": "Rich"}, "search"),
        tool("get_departures_with_disruptions", {"stop_id": 1101, "limit": 2}, "must-not-run"),
    ])
    trace = AgentTrace()
    answer = answer_question("And from a station called Rich?", client=client, model="test",
                             conversation=state, trace=trace, ptv_config=fixture.config, ptv_transport=fixture)
    assert "Which station" in answer
    assert all(name in answer for name in ("Richmond", "East Richmond", "West Richmond"))
    assert state.station_name is None
    assert len(client.requests) == len(trace.tool_calls) == 1
    assert trace.status == "completed"
    fixture.assert_all_used()


def test_unique_exact_match_does_not_authorize_other_stops_from_the_same_search():
    data = fixture_data()
    ambiguous = fixture_data("ambiguous_rich_station")
    data["responses"][0]["response"]["body"]["stops"] = ambiguous["responses"][0]["response"]["body"]["stops"]
    client = ScriptedClient([
        tool("search_stations", {"query": "Richmond Station"}, "search"),
        tool("get_departures_with_disruptions", {"stop_id": 1104, "limit": 5}, "wrong"),
        tool("get_departures_with_disruptions", {"stop_id": 1101, "limit": 5}, "right"),
        final(),
    ])
    # The fixture path follows the exact query; returned station names remain canonical.
    data["responses"][0]["request"]["path"] = "/v3/search/Richmond Station"
    fixture = FixtureTransport(data)
    state = ConversationState()
    answer_question("Next Richmond Station trains?", client=client, model="test",
                    conversation=state, ptv_config=fixture.config, ptv_transport=fixture)
    outputs = [item for item in client.requests[2]["input"] if isinstance(item, dict)
               and item.get("type") == "function_call_output"]
    assert json.loads(outputs[-1]["output"])["error"]["code"] == "unverified_stop_id"
    assert state.station_name == "Richmond"
    assert fixture.match_counts == (1, 1)


def test_missing_estimate_on_follow_up_is_not_replaced_with_previous_estimate():
    state = ConversationState()
    run_turn(state, FixtureTransport(fixture_data()), "Next trains from Richmond?")
    data = fixture_data(limit=2)
    for departure in data["responses"][1]["response"]["body"]["departures"]:
        departure["estimated_departure_utc"] = None
    _, client, _ = run_turn(state, FixtureTransport(data), "What about the next two?", limit=2)
    for entry in result(client)["board"]["departures"]:
        departure = entry["departure"]
        assert departure["estimated_departure_utc"] is None
        assert departure["estimated_local"] is None
        assert departure["delay_seconds"] is None
        assert departure["delay_status"] == "unknown"
        assert "real-time estimate unavailable" in departure["departure_time_description"]


def test_failed_follow_up_retrieval_preserves_preferences_but_returns_no_old_board():
    state = ConversationState()
    run_turn(state, FixtureTransport(fixture_data()), "Next trains from Richmond?")
    answer, client, _ = run_turn(state, FixtureTransport(fixture_data("ptv_api_failure", limit=2)),
                                 "What about the next two?", limit=2, answer="Departure data could not be retrieved.")
    assert answer == "Departure data could not be retrieved."
    payload = result(client)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "data_unavailable"
    assert "board" not in payload
    assert state.preferences() == {"station_name": "Richmond", "departure_limit": 2}
    _, retry, _ = run_turn(state, FixtureTransport(fixture_data(limit=2)), "Try again", limit=2)
    assert result(retry)["ok"] is True


def test_reset_removes_station_and_count_before_next_question():
    state = ConversationState("Richmond", 2)
    state.reset()
    client = ScriptedClient([final("Which station?")])
    assert "Which Melbourne train station" in answer_question("Next two?", client=client, model="test", conversation=state)
    assert state.preferences() == {"station_name": None, "departure_limit": None}
    assert preferences(client) == state.preferences()


def test_separate_conversation_objects_do_not_share_station_or_count():
    first, second = ConversationState(), ConversationState()
    run_turn(first, FixtureTransport(fixture_data()), "Next trains from Richmond?")
    run_turn(second, FixtureTransport(fixture_data("glen_waverley_no_estimate")),
             "Next trains from Glen Waverley?", query="Glen Waverley", stop_id=1103)
    run_turn(first, FixtureTransport(fixture_data(limit=2)), "Next two?", limit=2)
    assert first.preferences() == {"station_name": "Richmond", "departure_limit": 2}
    assert second.preferences() == {"station_name": "Glen Waverley", "departure_limit": 5}


def test_remembered_station_does_not_make_any_stop_id_trusted_without_new_search():
    state = ConversationState("Richmond", 5)
    fixture = FixtureTransport(fixture_data())
    trace = AgentTrace()
    client = ScriptedClient([
        tool("get_departures_with_disruptions", {"stop_id": 1101, "limit": 2}, "unverified"),
        final("Old trains at 09:58."),
    ])
    with pytest.raises(AgentError, match="Fresh departure information was not retrieved"):
        answer_question("Next two?", client=client, model="test", conversation=state, trace=trace,
                        ptv_config=fixture.config, ptv_transport=fixture)
    assert trace.tool_calls[0]["result"]["error"]["code"] == "unverified_stop_id"
    assert trace.error_code == "fresh_data_required"
    assert fixture.match_counts == (0, 0)
    assert state.departure_limit == 5


def test_model_cannot_finish_with_departure_claims_after_only_a_station_search():
    state = ConversationState()
    fixture = FixtureTransport(fixture_data())
    client = ScriptedClient([tool("search_stations", {"query": "Richmond"}, "search"),
                             final("The next train leaves at 09:58.")])
    with pytest.raises(AgentError, match="Fresh departure information"):
        answer_question("Next trains from Richmond?", client=client, model="test", conversation=state,
                        ptv_config=fixture.config, ptv_transport=fixture)
    assert fixture.match_counts == (1, 0)


def test_unknown_station_clears_previous_station_instead_of_silently_falling_back():
    state = ConversationState("Richmond", 2)
    fixture = FixtureTransport(fixture_data("unknown_station_xyzabc"))
    client = ScriptedClient([tool("search_stations", {"query": "XYZABC"}, "search")])
    answer = answer_question("And from XYZABC?", client=client, model="test", conversation=state,
                             ptv_config=fixture.config, ptv_transport=fixture)
    assert "No matching stations" in answer
    assert state.station_name is None
    fixture.assert_all_used()
