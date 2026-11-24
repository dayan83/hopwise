from copy import deepcopy
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import Mock, call

import httpx
import httpx2
from openai import APIError, OpenAI
import pytest

from agent import AgentError, INSTRUCTIONS, MAX_TOOL_ROUNDS, answer_question
from agent_trace import AgentTrace
import departure_service
from evals.fixture_transport import FixtureTransport
from models import Departure, DepartureBoard, DepartureWithDisruptions
import ptv


def tool_response(name="search_stations", arguments='{"query":"Richmond"}', call_id="call-search", extra=()):
    return SimpleNamespace(status="completed", output_text="", output=[*extra, SimpleNamespace(
        type="function_call", name=name, arguments=arguments, call_id=call_id, id="different-item-id")])


def final_response(text="Here are the retrieved trains.", status="completed"):
    return SimpleNamespace(status=status, output_text=text, output=[SimpleNamespace(type="message")])


class ScriptedClient:
    """Return scripted model outputs and capture the conversation for inspection."""

    def __init__(self, responses):
        self.responses = self
        self.script = iter(responses)
        self.requests = []
        self.tool_calls = []

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        response = next(self.script)
        if isinstance(response, Exception):
            raise response
        self.tool_calls.extend(
            {"name": item.name, "arguments": item.arguments, "call_id": item.call_id}
            for item in response.output if item.type == "function_call")
        return response


@pytest.fixture
def search(monkeypatch):
    search = Mock(return_value=[{"stop_id": 1, "stop_name": "Richmond", "route_type": 0}])
    monkeypatch.setattr(ptv, "search_stations", search)
    return search


def tool_outputs(request):
    return [item for item in request["input"]
            if isinstance(item, dict) and item.get("type") == "function_call_output"]


def test_complete_loop_preserves_output_items_and_call_ids(search, monkeypatch):
    # Synthetic IDs make the dependency on the actual search result explicit.
    search.return_value = [
        {"stop_id": 99, "stop_name": "East Richmond", "stop_suburb": "Cremorne", "route_type": 0},
        {"stop_id": 4321, "stop_name": "Richmond", "stop_suburb": "Richmond", "route_type": 0},
    ]
    fetch = Mock(return_value=DepartureBoard(stop_id=4321, departures=[DepartureWithDisruptions(
        departure=Departure(stop_id=4321, route_id=9, run_ref="test-run", disruption_ids=[]))]))
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", fetch)
    services = Mock()
    services.attach_mock(search, "search")
    services.attach_mock(fetch, "departures")
    reasoning = SimpleNamespace(type="reasoning", id="reasoning-item")
    client = ScriptedClient([
        tool_response(extra=[reasoning]),
        tool_response("get_departures_with_disruptions", '{"stop_id":4321,"limit":5}', "call-departures"),
        final_response("A departure was returned for Richmond; no estimate is available."),
    ])
    result = answer_question("Next Richmond trains?", client=client, model="test-model")
    assert result == "A departure was returned for Richmond; no estimate is available."
    assert len(client.requests) == 3
    assert any(getattr(item, "id", None) == "reasoning-item" for item in client.requests[1]["input"])
    first_output = tool_outputs(client.requests[1])[0]
    assert first_output["call_id"] == "call-search"
    assert json.loads(first_output["output"]) == {"ok": True, "stations": search.return_value}
    last_output = tool_outputs(client.requests[2])[-1]
    assert last_output["call_id"] == "call-departures"
    returned = json.loads(last_output["output"])
    assert returned["ok"] is True
    assert returned["board"]["stop_id"] == 4321
    assert returned["board"]["departures"][0]["departure"]["run_ref"] == "test-run"
    for request in client.requests:
        assert request["parallel_tool_calls"] is False
        assert request["store"] is False
        assert len(request["tools"]) == 2
        assert "only from tool results" in request["instructions"]
    assert services.mock_calls == [
        call.search("Richmond", None, transport=None),
        call.departures(4321, max_results=5, config=None, transport=None),
    ]
    assert [item["name"] for item in client.tool_calls] == [
        "search_stations", "get_departures_with_disruptions"]
    # pytest captures this by default; use -s on this test to inspect the sequence.
    print(json.dumps({
        "model_requests": len(client.requests),
        "requested_tools": [{**item, "arguments": json.loads(item["arguments"])}
                            for item in client.tool_calls],
        "returned_results": [{"call_id": item["call_id"], "ok": json.loads(item["output"])["ok"]}
                             for item in tool_outputs(client.requests[-1])],
    }, indent=2))


@pytest.mark.parametrize("case_id,time_description", [
    ("glen_waverley_no_estimate",
     "Scheduled departure: 2026-11-18 10:05:00 AEDT; real-time estimate unavailable."),
    ("richmond_30_seconds_early",
     "Estimated departure: 2026-11-18 09:57:30 AEDT; scheduled departure: 2026-11-18 09:58:00 AEDT."),
])
def test_timing_presentation_contract_reaches_model_and_trace_from_baseline_fixtures(
        monkeypatch, case_id, time_description):
    """Check data and instructions sent to a scripted LLM, not generated prose."""
    def forbidden(*args, **kwargs):
        pytest.fail("Timing regressions must use fixtures without network or real credentials")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(ptv.PTVConfig, "from_env", forbidden)
    evals = Path(__file__).resolve().parents[1] / "evals"
    cases = json.loads((evals / "dataset.json").read_text(encoding="utf-8"))["cases"]
    case = next(case for case in cases if case["case_id"] == case_id)
    fixture = FixtureTransport.from_file(evals / case["fixture"])
    client = ScriptedClient([
        *(tool_response(call["name"], json.dumps(call["arguments"]), f"call-{index}")
          for index, call in enumerate(case["expected_calls"])),
        final_response("Captured offline tool output."),
    ])
    trace = AgentTrace()
    with fixture.reference_time():
        answer_question(case["question"], client=client, model="test",
                        ptv_config=fixture.config, ptv_transport=fixture, trace=trace)
    sent = json.loads(tool_outputs(client.requests[-1])[-1]["output"])
    departure = sent["board"]["departures"][0]["departure"]
    assert departure["departure_time_description"] == time_description
    golden = case["expected_facts"]["departures"][0]
    for key in ("scheduled_departure_utc", "estimated_departure_utc", "scheduled_local",
                "estimated_local", "delay_seconds", "delay_status", "delay_description"):
        assert departure[key] == golden[key]
    assert trace.tool_calls[-1]["result"]["board"]["departures"][0]["departure"] == departure
    for request in client.requests:
        assert "Use departure_time_description" in request["instructions"]
        assert "keep the labelled schedule as reference" in request["instructions"]
        assert "real-time estimate is unavailable" in request["instructions"]
        assert "Never substitute a scheduled time" in request["instructions"]
    fixture.assert_all_used()
    assert fixture.miss_count == 0


def test_disruption_response_policy_separates_stated_hours_from_actual_impact():
    """An offline prompt contract; this does not score generated LLM prose."""
    policy = " ".join(INSTRUCTIONS.split())
    for requirement in (
        "For departure questions, lead with departure times, destinations, platforms and supplied delay information.",
        "Summarise directly associated notices briefly when their immediate relevance is unclear",
        "keep the association and any explicit safety information visible",
        "Use relevance annotations as limited date/stop evidence, not a verdict of impact.",
        "If notice text explicitly states operating hours, briefly relate those stated hours to the departure, identifying this as interpretation of the notice text.",
        "Different described hours do not prove non-impact.",
        "Ambiguous or incomplete notice details leave actual impact uncertain.",
        "Never invent operating hours, dates, affected stations or causal relationships not supported by retrieved data.",
        "association alone is not proof that a train is delayed or cancelled.",
        "within_window only means the departure falls within the supplied date bounds; it does not prove a disruption is operating at that time or affecting that train.",
        "Do not infer unstated recurring overnight operating windows from descriptions.",
    ):
        assert requirement in policy, requirement


@pytest.mark.parametrize("variant", ["overnight", "missing_description", "safety"])
def test_disruption_policy_reaches_model_without_removing_associated_notices(monkeypatch, variant):
    """Retain notices and evidence; only instructions govern the model's wording."""
    def forbidden(*args, **kwargs):
        pytest.fail("Disruption policy regressions must not use live APIs or real credentials")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(ptv.PTVConfig, "from_env", forbidden)
    evals = Path(__file__).resolve().parents[1] / "evals"
    cases = json.loads((evals / "dataset.json").read_text(encoding="utf-8"))["cases"]
    case = next(case for case in cases if case["case_id"] == "flinders_street_morning_late_night_notice")
    data = json.loads((evals / case["fixture"]).read_text(encoding="utf-8"))
    original_notice = data["responses"][1]["response"]["body"]["disruptions"]["5101"]
    # Variants exist only in memory; golden cases and fixture files stay untouched.
    if variant == "missing_description":
        original_notice["description"] = None
    elif variant == "safety":
        original_notice["description"] = "Safety notice: keep clear of the affected platform area and follow staff instructions."
    fixture = FixtureTransport(data)
    client = ScriptedClient([
        *(tool_response(call["name"], json.dumps(call["arguments"]), f"call-{index}")
          for index, call in enumerate(case["expected_calls"])),
        final_response("Captured offline disruption policy and evidence."),
    ])
    trace = AgentTrace()
    with fixture.reference_time():
        answer_question(case["question"], client=client, model="test",
                        ptv_config=fixture.config, ptv_transport=fixture, trace=trace)
    output = tool_outputs(client.requests[-1])[-1]
    assert output["call_id"] == "call-1"
    sent = json.loads(output["output"])
    assert sent["ok"] is True
    board = sent["board"]
    assert board["stop_id"] == 1102
    assert len(board["departures"]) == 1
    entry = board["departures"][0]
    departure = entry["departure"]
    assert departure["disruption_ids"] == [5101]
    assert departure["estimated_local"] == departure["scheduled_local"] == "2026-11-18 10:00:00 AEDT"
    assert departure["destination_name"] == "Glen Waverley"
    assert departure["platform_number"] == "3"
    assert departure["delay_seconds"] == 0
    assert departure["delay_status"] == "matches_schedule"
    assert [notice["disruption_id"] for notice in entry["associated_disruptions"]] == [5101]
    notice = entry["associated_disruptions"][0]
    assert notice["description"] == original_notice["description"]
    assert notice["disruption_status"] == "Planned"
    assert notice["relevance"] == case["expected_facts"]["notices"][0]["relevance"]
    assert notice["from_date"] == original_notice["from_date"]
    assert notice["to_date"] == original_notice["to_date"]
    assert entry["unresolved_disruption_ids"] == []
    assert board["contextual_notices"] == []
    assert board["route_notices"] == {}
    captured_notice = trace.tool_calls[-1]["result"]["board"]["departures"][0]["associated_disruptions"][0]
    assert captured_notice["description"] == original_notice["description"]
    assert captured_notice["disruption_id"] == 5101
    assert captured_notice["relevance"] == notice["relevance"]
    if variant == "overnight":
        assert "from 10 pm to 4 am" in notice["description"]
        assert notice["relevance"]["temporal_status"] == "within_window"
    assert len(client.requests) == 3
    assert [call["name"] for call in client.tool_calls] == [call["name"] for call in case["expected_calls"]]
    assert all(request["instructions"] == INSTRUCTIONS for request in client.requests)
    fixture.assert_all_used()
    assert fixture.match_counts == (1, 1)
    assert fixture.miss_count == 0


@pytest.mark.parametrize("response,code", [
    (tool_response(name="unknown"), "unknown_tool"),
    (tool_response(arguments="invalid JSON"), "invalid_arguments"),
    (tool_response("get_departures_with_disruptions", '{"stop_id":999,"limit":5}'), "unverified_stop_id"),
])
def test_invalid_calls_return_correlated_errors_without_ptv(search, monkeypatch, response, code):
    fetch = Mock()
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", fetch)
    client = ScriptedClient([response, final_response("The requested information is unavailable.")])
    assert answer_question("Richmond?", client=client, model="test") == "The requested information is unavailable."
    output = tool_outputs(client.requests[1])[0]
    assert output["call_id"] == response.output[0].call_id
    assert json.loads(output["output"])["error"]["code"] == code
    assert json.loads(output["output"])["ok"] is False
    assert len(client.requests) == 2
    search.assert_not_called()
    fetch.assert_not_called()


@pytest.mark.parametrize("stage", ["search", "departures"])
def test_tool_api_failure_is_returned_as_unavailable(search, monkeypatch, stage):
    fetch = Mock(side_effect=ptv.PTVRequestError("raw diagnostics must not reach model"))
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", fetch)
    responses = [tool_response()]
    if stage == "search":
        search.side_effect = ptv.PTVRequestError("raw diagnostics must not reach model")
    else:
        responses.append(tool_response("get_departures_with_disruptions",
                                       '{"stop_id":1,"limit":5}', "call-departures"))
    client = ScriptedClient([*responses, final_response("PTV data is unavailable.")])
    assert answer_question("Richmond?", client=client, model="test") == "PTV data is unavailable."
    output = tool_outputs(client.requests[-1])[-1]
    assert output["call_id"] == ("call-search" if stage == "search" else "call-departures")
    assert json.loads(output["output"])["error"]["code"] == "data_unavailable"
    assert "raw diagnostics" not in output["output"]
    if stage == "search":
        fetch.assert_not_called()
    else:
        fetch.assert_called_once_with(1, max_results=5, config=None, transport=None)


@pytest.mark.parametrize("invalid_limit", [0, 11, "5"])
def test_invalid_departure_arguments_can_be_corrected_without_calling_ptv_early(search, monkeypatch, invalid_limit):
    fetch = Mock(return_value=DepartureBoard(stop_id=1))
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", fetch)
    client = ScriptedClient([
        tool_response(),
        tool_response("get_departures_with_disruptions", json.dumps({"stop_id": 1, "limit": invalid_limit}), "call-invalid"),
        tool_response("get_departures_with_disruptions", '{"stop_id":1,"limit":5}', "call-corrected"),
        final_response("No departures were returned."),
    ])
    assert answer_question("Richmond?", client=client, model="test") == "No departures were returned."
    invalid_output = tool_outputs(client.requests[2])[-1]
    assert invalid_output["call_id"] == "call-invalid"
    assert json.loads(invalid_output["output"])["error"]["code"] == "invalid_arguments"
    corrected_output = tool_outputs(client.requests[3])[-1]
    assert corrected_output["call_id"] == "call-corrected"
    assert json.loads(corrected_output["output"])["ok"] is True
    assert len(client.requests) == 4
    search.assert_called_once()
    fetch.assert_called_once_with(1, max_results=5, config=None, transport=None)


def test_final_response_without_a_tool_does_not_execute_ptv(search, monkeypatch):
    fetch = Mock()
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", fetch)
    client = ScriptedClient([final_response("Which Melbourne train station would you like to use?")])
    assert answer_question("Help me choose a station", client=client, model="test") == (
        "Which Melbourne train station would you like to use?")
    assert len(client.requests) == 1
    assert client.tool_calls == []
    search.assert_not_called()
    fetch.assert_not_called()


def test_five_tool_rounds_allow_a_final_answer(search):
    client = ScriptedClient([*(tool_response(call_id=f"call-{i}") for i in range(5)), final_response()])
    answer_question("Richmond?", client=client, model="test")
    assert MAX_TOOL_ROUNDS == 5
    assert search.call_count == 5
    assert len(client.requests) == 6
    assert client.requests[-1]["tool_choice"] == "none"


def test_a_sixth_tool_round_never_executes(search):
    client = ScriptedClient([tool_response(call_id=f"call-{i}") for i in range(6)])
    with pytest.raises(AgentError, match="five tool-call rounds"):
        answer_question("Richmond?", client=client, model="test")
    assert search.call_count == 5
    assert len(client.requests) == 6
    assert client.requests[-1]["tool_choice"] == "none"
    assert len(tool_outputs(client.requests[-1])) == 5


def test_stop_provenance_does_not_persist_between_questions(search, monkeypatch):
    fetch = Mock()
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", fetch)
    client = ScriptedClient([tool_response(), final_response("Found Richmond."),
        tool_response("get_departures_with_disruptions", '{"stop_id":1,"limit":5}'), final_response()])
    answer_question("Find Richmond", client=client, model="test")
    answer_question("Get trains", client=client, model="test")
    output = tool_outputs(client.requests[-1])[0]
    assert json.loads(output["output"])["error"]["code"] == "unverified_stop_id"
    fetch.assert_not_called()


@pytest.mark.parametrize("response,message", [
    (final_response(""), "did not provide an answer"),
    (final_response(status="incomplete"), "did not complete"),
    (tool_response(call_id=""), "valid call_id"),
])
def test_incomplete_protocol_results_stop_safely(response, message):
    with pytest.raises(AgentError, match=message):
        answer_question("Richmond?", client=ScriptedClient([response]), model="test")


def test_openai_errors_do_not_expose_raw_diagnostics(caplog):
    error = APIError("private-test-secret", request=httpx2.Request("POST", "https://api.openai.com/v1/responses"), body=None)
    with caplog.at_level("DEBUG"), pytest.raises(AgentError) as caught:
        answer_question("Richmond?", client=ScriptedClient([error]), model="test")
    assert "private-test-secret" not in str(caught.value) + caplog.text


@pytest.mark.parametrize("question,model", [("", "test"), ("question", "")])
def test_empty_inputs_never_call_model(question, model):
    client = ScriptedClient([])
    with pytest.raises(ValueError):
        answer_question(question, client=client, model=model)
    assert client.requests == []


def test_installed_sdk_serializes_real_responses_protocol_without_network(search, caplog):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            output = [{"type": "function_call", "id": "fc-1", "call_id": "sdk-call-1",
                       "name": "search_stations", "arguments": '{"query":"Richmond"}', "status": "completed"}]
        else:
            assert body["input"][-1]["call_id"] == "sdk-call-1"
            output = [{"type": "message", "id": "msg-1", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "Found Richmond.", "annotations": []}]}]
        return httpx2.Response(200, json={"id": f"resp-{len(requests)}", "object": "response",
            "created_at": 0, "status": "completed", "model": "test-model", "output": output})

    with caplog.at_level("DEBUG"), OpenAI(api_key="private-sdk-test-key", max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler), trust_env=False)) as client:
        assert answer_question("Find Richmond", client=client, model="test-model") == "Found Richmond."
    assert len(requests) == 2
    assert "private-sdk-test-key" not in json.dumps(requests) + caplog.text
