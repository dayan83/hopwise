from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
from openai import APIError
import pytest

import agent
import agent_tools
from agent import AgentError, answer_question
from agent_trace import AgentTrace
import departure_service
from models import Departure, DepartureBoard, DepartureWithDisruptions, Disruption
import ptv


def tool(name="search_stations", arguments='{"query":"  Richmond  "}', call_id="call-search"):
    return SimpleNamespace(status="completed", output_text="", output=[SimpleNamespace(
        type="function_call", name=name, arguments=arguments, call_id=call_id)])


def final():
    return SimpleNamespace(status="completed", output_text="Retrieved train information.", output=[])


class FakeClient:
    api_key = "private-openai-client-key"

    def __init__(self, responses):
        self.responses = self
        self.script = iter(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        response = next(self.script)
        if isinstance(response, Exception):
            raise response
        return response


def successful_client():
    return FakeClient([
        tool(),
        tool("get_departures_with_disruptions", '{"stop_id":4321,"limit":5}', "call-departures"),
        final(),
    ])


@pytest.fixture(autouse=True)
def services(monkeypatch):
    # All calls are mocked, and fixed capture time makes traced/untraced requests comparable.
    monkeypatch.setattr(agent_tools, "datetime", SimpleNamespace(
        now=lambda tz: datetime(2026, 11, 17, 22, 57, tzinfo=timezone.utc)))
    search = Mock(return_value=[{
        "stop_id": 4321, "stop_name": "Richmond", "stop_suburb": "Richmond", "route_type": 0,
    }])
    notice = Disruption(disruption_id=101, title="Track works", disruption_status="Planned",
                        from_date="2026-11-18T12:00:00Z", to_date=None,
                        routes=[{"route_id": 9}], stops=[{"stop_id": 4321}])
    board = DepartureBoard(stop_id=4321, departures=[
        DepartureWithDisruptions(departure=Departure(
            stop_id=4321, route_id=9, run_ref="run-A", destination_name="Hurstbridge",
            scheduled_departure_utc="2026-11-17T22:58:00Z",
            estimated_departure_utc="2026-11-17T22:58:30Z", disruption_ids=[101, 999]),
            associated_disruptions=[notice], unresolved_disruption_ids=[999]),
        DepartureWithDisruptions(departure=Departure(
            stop_id=4321, route_id=9, run_ref="run-B", disruption_ids=[101]),
            associated_disruptions=[notice]),
    ], contextual_notices=[Disruption(disruption_id=102, title="Station access")],
        route_notices={9: [Disruption(disruption_id=103, title="Other route notice")]})
    departures = Mock(return_value=board)
    monkeypatch.setattr(ptv, "search_stations", search)
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", departures)
    return search, departures


def test_trace_records_search_and_departures(services):
    trace = AgentTrace()
    client = successful_client()
    answer = answer_question("Next Richmond trains?", client=client, model="test", trace=trace)
    assert isinstance(answer, str)
    assert answer == "Retrieved train information."
    assert trace.status == "completed"
    assert trace.error_code is None
    search, departures = trace.tool_calls
    assert search == {
        "sequence": 1, "tool_name": "search_stations", "call_id": "call-search",
        "arguments": {"query": "Richmond"}, "ok": True,
        "result": {"ok": True, "stations": services[0].return_value},
    }
    assert departures["sequence"] == 2
    assert departures["tool_name"] == "get_departures_with_disruptions"
    assert departures["call_id"] == "call-departures"
    assert departures["arguments"] == {"stop_id": 4321, "limit": 5}
    assert departures["ok"] is True
    board = departures["result"]["board"]
    first = board["departures"][0]
    assert first["departure"]["scheduled_departure_utc"] == "2026-11-17T22:58:00Z"
    assert first["departure"]["estimated_departure_utc"] == "2026-11-17T22:58:30Z"
    assert first["departure"]["scheduled_local"] == "2026-11-18 09:58:00 AEDT"
    assert first["departure"]["estimated_local"] == "2026-11-18 09:58:30 AEDT"
    assert first["departure"]["delay_seconds"] == 30
    assert first["departure"]["delay_status"] == "late"
    assert first["departure"]["delay_description"] == "Estimated 30 seconds late relative to the schedule."
    sent = json.loads(client.requests[-1]["input"][-1]["output"])["board"]["departures"][0]["departure"]
    assert sent["delay_status"] == first["departure"]["delay_status"]
    assert sent["delay_description"] == first["departure"]["delay_description"]
    assert first["departure"]["run_ref"] == "run-A"
    assert first["departure"]["disruption_ids"] == [101, 999]
    assert first["unresolved_disruption_ids"] == [999]
    assert first["associated_disruptions"][0]["disruption_id"] == 101
    assert first["associated_disruptions"][0]["disruption_status"] == "Planned"
    assert first["associated_disruptions"][0]["to_date"] is None
    assert first["associated_disruptions"][0]["stops"] == [{"stop_id": 4321}]
    relevance = first["associated_disruptions"][0]["relevance"]
    assert relevance == {
        "temporal_status": "unknown", "temporal_unknown_reason": "missing_date_bounds",
        "reference_departure_utc": "2026-11-17T22:58:30+00:00", "reference_time_source": "estimated",
        "location_status": "boarding_station_listed", "boarding_stop_id": 4321,
        "affected_stop_ids": [4321],
    }
    sent_notice = json.loads(client.requests[-1]["input"][-1]["output"])["board"]["departures"][0]["associated_disruptions"][0]
    assert sent_notice["relevance"] == relevance
    assert board["departures"][1]["departure"]["estimated_departure_utc"] is None
    assert board["departures"][1]["departure"]["delay_seconds"] is None
    assert board["departures"][1]["departure"]["delay_status"] == "unknown"
    assert board["departures"][1]["associated_disruptions"][0]["disruption_id"] == 101
    assert board["contextual_notices"][0]["disruption_id"] == 102
    assert board["route_notices"]["9"][0]["disruption_id"] == 103
    # Run this test with -s to inspect a complete synthetic execution, without APIs.
    print(json.dumps(trace.as_dict(), indent=2))


def test_tracing_is_disabled_by_default(monkeypatch, capsys):
    capture = Mock()
    monkeypatch.setattr(AgentTrace, "record_tool", capture)
    dispatch = Mock(wraps=agent.execute_tool)
    monkeypatch.setattr(agent, "execute_tool", dispatch)
    unused = AgentTrace()
    answer = answer_question("Next Richmond trains?", client=successful_client(), model="test")
    assert isinstance(answer, str)
    capture.assert_not_called()
    assert unused.as_dict() == {"status": "pending", "error_code": None, "tool_calls": []}
    assert all(not call.kwargs for call in dispatch.call_args_list)
    assert capsys.readouterr() == ("", "")


def test_tracing_does_not_change_requests_results_or_service_calls(services):
    plain_client, traced_client = successful_client(), successful_client()
    plain_answer = answer_question("Next Richmond trains?", client=plain_client, model="test")
    trace = AgentTrace()
    traced_answer = answer_question("Next Richmond trains?", client=traced_client, model="test", trace=trace)
    assert plain_answer == traced_answer
    assert plain_client.requests == traced_client.requests
    assert all(service.call_count == 2 for service in services)
    # A snapshot cannot mutate the data used by the LLM or the stored collector.
    snapshot = trace.as_dict()
    snapshot["tool_calls"][0]["result"]["stations"].clear()
    assert trace.tool_calls[0]["result"]["stations"]
    assert services[0].return_value


@pytest.mark.parametrize("response,code,arguments", [
    (tool(name="unknown", arguments='{"api_key":"raw-secret"}'), "unknown_tool", None),
    (tool(name=["not-a-name"]), "unknown_tool", None),
    (tool(arguments='{"query":"Ri","api_key":"raw-secret"}'), "invalid_arguments", None),
    (tool(arguments="raw-secret, invalid JSON"), "invalid_arguments", None),
    (tool("get_departures_with_disruptions", '{"stop_id":999,"limit":5}'),
     "unverified_stop_id", {"stop_id": 999, "limit": 5}),
])
def test_rejected_calls_record_only_validated_arguments(response, code, arguments, services):
    trace = AgentTrace()
    answer_question("Richmond?", client=FakeClient([response, final()]), model="test", trace=trace)
    entry = trace.tool_calls[0]
    assert entry["ok"] is False
    assert entry["arguments"] == arguments
    assert entry["result"] == {"ok": False, "error": {"code": code}}
    assert "raw-secret" not in json.dumps(trace.as_dict())
    assert trace.status == "completed"  # The model still provided a final answer.
    for service in services:
        service.assert_not_called()


def test_ptv_failure_is_captured_without_raw_diagnostics(services):
    services[0].side_effect = ptv.PTVRequestError("secret https://ptv.example/?signature=private")
    trace = AgentTrace()
    answer_question("Richmond?", client=FakeClient([tool(), final()]), model="test", trace=trace)
    entry = trace.tool_calls[0]
    assert entry["arguments"] == {"query": "Richmond"}
    assert entry["result"] == {"ok": False, "error": {"code": "data_unavailable"}}
    assert entry["ok"] is False
    assert "secret" not in json.dumps(trace.as_dict())
    assert "signature" not in json.dumps(trace.as_dict())


def test_llm_failure_keeps_partial_trace_without_sdk_diagnostics(caplog):
    error = APIError("private-sdk-diagnostics", request=httpx2.Request(
        "POST", "https://api.example/?api_key=private-url-key"), body={"secret": "private-body"})
    trace = AgentTrace()
    with pytest.raises(AgentError):
        answer_question("Richmond?", client=FakeClient([tool(), error]), model="test", trace=trace)
    assert trace.status == "failed"
    assert trace.error_code == "llm_api_error"
    assert len(trace.tool_calls) == 1
    output = json.dumps(trace.as_dict()) + caplog.text
    for secret in ("private-sdk-diagnostics", "private-url-key", "private-body"):
        assert secret not in output


def test_snapshot_excludes_credentials_urls_and_unknown_fields(monkeypatch, services):
    config = ptv.PTVConfig("private-developer-id", "private-ptv-client-key", "https://private-origin.example")
    monkeypatch.setenv("OPENAI_API_KEY", "private-openai-env-key")
    monkeypatch.setenv("PTV_API_KEY", "private-ptv-env-key")
    monkeypatch.setenv("PTV_DEV_ID", config.dev_id)
    client = successful_client()
    secrets = (config.api_key, config.dev_id, client.api_key, "private-openai-env-key", "private-ptv-env-key")
    notice = services[1].return_value.departures[0].associated_disruptions[0]
    notice.description = (" ".join(secrets) + " https://ptv.example/v3/search/Richmond?devid=123&signature=hidden-signature"
                          " /v3/search/Richmond?devid=123&signature=hidden-relative-signature"
                          " api_key=unknown-key signature=unknown-signature Bearer private-bearer")
    notice.url = "https://ptv.example/?signature=private-source-signature"
    notice.stops[0]["api_key"] = "private-nested-key"
    notice.stops[0]["url"] = "https://ptv.example/?signature=private-nested-signature"
    notice.stops[0]["sdk_diagnostics"] = "private-diagnostics"
    trace = AgentTrace()
    answer_question("Richmond?", client=client, model="test", ptv_config=config, trace=trace)
    output = json.dumps(trace.as_dict())
    for secret in (*secrets, "hidden-signature", "hidden-relative-signature", "unknown-key",
                   "unknown-signature", "private-bearer", "private-source-signature",
                   "private-nested-key", "private-nested-signature", "private-diagnostics"):
        assert secret not in output
    assert "https://" not in output
    assert "[REDACTED]" in output
    assert "url" not in trace.tool_calls[1]["result"]["board"]["departures"][0]["associated_disruptions"][0]
    assert not any(secret in repr(trace) for secret in secrets)
    # Sanitizing a trace must not change the actual tool output sent to the model.
    actual_result = json.loads(client.requests[-1]["input"][-1]["output"])
    assert actual_result["board"]["departures"][0]["associated_disruptions"][0]["description"] == notice.description


def test_collector_is_reset_between_executions():
    trace = AgentTrace()
    answer_question("Richmond?", client=FakeClient([tool(), final()]), model="test", trace=trace)
    answer_question("Help?", client=FakeClient([final()]), model="test", trace=trace)
    assert trace.as_dict() == {"status": "completed", "error_code": None, "tool_calls": []}


def test_tool_round_limit_preserves_five_observed_attempts():
    trace = AgentTrace()
    client = FakeClient([tool(call_id=f"call-{i}") for i in range(6)])
    with pytest.raises(AgentError, match="five tool-call rounds"):
        answer_question("Richmond?", client=client, model="test", trace=trace)
    assert len(trace.tool_calls) == 5
    assert trace.error_code == "tool_round_limit"
