from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import socket

import httpx
import httpx2
import pytest

import agent
from conversation import ConversationState
from evals import multiturn
from evals.multiturn_checks import check_isolation, score_scenario
from ptv import PTVConfig


DATASET = multiturn.load_dataset()
SCENARIOS = DATASET["scenarios"]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Multi-turn evaluation must not create live clients, load credentials or access the network")

    monkeypatch.setattr(agent, "OpenAI", forbidden)
    monkeypatch.setattr(agent, "load_dotenv", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(PTVConfig, "from_env", forbidden)


def load_results(summary, directory):
    root = directory / summary["run_id"]
    assert json.loads((root / "summary.json").read_text(encoding="utf-8")) == summary
    return [json.loads((root / item["result_file"]).read_text(encoding="utf-8")) for item in summary["scenarios"]]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario["scenario_id"])
def test_each_scenario_passes_with_independent_scripts_and_fixed_ptv(scenario, tmp_path):
    summary = multiturn.run_suite(scenario_id=scenario["scenario_id"], results_dir=tmp_path)
    assert summary["mode"] == "scripted-offline"
    assert summary["scenarios_attempted"] == summary["scenarios_passed"] == 1
    assert summary["turns_attempted"] == summary["turns_completed"] == len(scenario["turns"])
    assert summary["turns_with_errors"] == summary["fixture_misses"] == 0
    assert summary["passed"] is True
    result = load_results(summary, tmp_path)[0]
    assert result["checks"]["passed"] is True
    for turn, spec in zip(result["turns"], scenario["turns"], strict=True):
        assert turn["fixture_coverage"]["match_counts"] == spec["expected_http_counts"]
        assert turn["state_before"] == turn["preferences_sent"] == spec["expected_state_before"]
        assert turn["state_after"] == spec["expected_state_after"]
        assert turn["error"] is None
        assert turn["execution_trace"]["status"] == "completed"


def test_all_five_create_distinct_states_and_reset_within_only_its_scenario(tmp_path, monkeypatch):
    states = []
    transports = []
    from_file = multiturn.FixtureTransport.from_file

    def new_state():
        state = ConversationState()
        states.append(state)
        return state

    monkeypatch.setattr(multiturn, "ConversationState", new_state)

    def new_transport(path):
        transport = from_file(path)
        transports.append(transport)
        return transport

    monkeypatch.setattr(multiturn.FixtureTransport, "from_file", new_transport)
    summary = multiturn.run_suite(results_dir=tmp_path)
    assert summary["scenarios_passed"] == summary["scenarios_attempted"] == 5
    assert summary["turns_attempted"] == summary["turns_completed"] == 8
    assert summary["passed"] is True
    assert len(states) == len({id(state) for state in states}) == 5
    assert len(transports) == len({id(transport) for transport in transports}) == 8
    results = load_results(summary, tmp_path)
    assert len({item["session_id"] for item in results}) == 5
    assert all(item["initial_state"] == {"station_name": None, "departure_limit": None} for item in results)
    assert states[0].preferences() == {"station_name": "Richmond", "departure_limit": 2}
    assert states[1].preferences() == {"station_name": "Flinders Street", "departure_limit": 5}
    assert states[4].preferences() == {"station_name": None, "departure_limit": None}
    reset = results[4]["turns"][1]
    assert reset["reset_applied"] is True
    assert reset["state_before_reset"] == {"station_name": "Richmond", "departure_limit": 5}
    assert reset["state_before"] == reset["state_after"] == {"station_name": None, "departure_limit": None}
    assert reset["requested_tool_calls"] == reset["execution_trace"]["tool_calls"] == []


@pytest.fixture
def retained_result(tmp_path):
    summary = multiturn.run_suite(scenario_id="retain_station_context", results_dir=tmp_path)
    return load_results(summary, tmp_path)[0]


@pytest.mark.parametrize("mutation", ["lost_state", "state_not_forwarded", "wrong_question", "wrong_station", "wrong_limit",
                                      "no_fresh_http", "cached_board", "reused_transport", "stale_timestamp"])
def test_checks_reject_context_argument_and_freshness_regressions(retained_result, mutation):
    record = deepcopy(retained_result)
    first, second = record["turns"]
    if mutation == "lost_state":
        second["state_before"]["station_name"] = None
    elif mutation == "state_not_forwarded":
        second["preferences_sent"] = None
    elif mutation == "wrong_question":
        second["question_sent"] = first["question_sent"]
    elif mutation in {"wrong_station", "wrong_limit"}:
        key, value = ("stop_id", 1102) if mutation == "wrong_station" else ("limit", 5)
        second["requested_tool_calls"][1]["arguments"][key] = value
    elif mutation == "no_fresh_http":
        second["fixture_coverage"]["match_counts"][1] = 0
    elif mutation == "cached_board":
        # Even if the HTTP counts and retrieval timestamp look fresh, old records must fail.
        second["execution_trace"]["tool_calls"][1]["result"]["board"] = deepcopy(first["execution_trace"]["tool_calls"][1]["result"]["board"])
    elif mutation == "reused_transport":
        second["fixture_instance_id"] = first["fixture_instance_id"]
    else:
        second["execution_trace"]["tool_calls"][1]["result"]["retrieved_at_utc"] = first["execution_trace"]["tool_calls"][1]["result"]["retrieved_at_utc"]
    assert score_scenario(SCENARIOS[0], record)["passed"] is False


def test_reset_and_session_isolation_checks_reject_leaked_preferences(tmp_path):
    summary = multiturn.run_suite(results_dir=tmp_path)
    results = load_results(summary, tmp_path)
    reset = results[4]["turns"][1]
    reset["reset_applied"] = False
    reset["state_before"] = deepcopy(reset["state_before_reset"])
    assert score_scenario(SCENARIOS[4], results[4])["passed"] is False
    results[1]["session_id"] = results[0]["session_id"]
    results[1]["initial_state"] = {"station_name": "Richmond", "departure_limit": 2}
    assert all(item["passed"] is False for item in check_isolation(results))


def test_zero_call_turn_rejects_an_unexpected_fixture_lookup(tmp_path):
    summary = multiturn.run_suite(scenario_id="follow_up_without_context", results_dir=tmp_path)
    result = load_results(summary, tmp_path)[0]
    result["turns"][0]["fixture_coverage"]["match_counts"][0] = 1
    assert score_scenario(SCENARIOS[2], result)["passed"] is False


def test_fixture_miss_is_recorded_without_live_fallback_and_other_scenarios_continue(tmp_path):
    scripts = json.loads(multiturn.SCRIPTS_PATH.read_text(encoding="utf-8"))["scenarios"]

    def factory(scenario, turn):
        replies = deepcopy(scripts[scenario][turn])
        if scenario == "retain_station_context" and turn == "2":
            replies[1]["arguments"]["limit"] = 5
        return multiturn.ScriptedClient(replies)

    summary = multiturn.run_suite(client_factory=factory, results_dir=tmp_path)
    assert summary["scenarios_attempted"] == 5
    assert summary["scenarios_passed"] == 4
    assert summary["turns_with_errors"] == summary["fixture_misses"] == 1
    results = load_results(summary, tmp_path)
    failure = results[0]["turns"][1]
    assert failure["error"]["code"] == "fixture_miss"
    assert failure["final_answer"] is None
    assert failure["fixture_coverage"]["match_counts"] == [1, 0]
    assert all(item["checks"]["passed"] for item in results[1:])


def test_client_failure_is_sanitized_and_saved_without_sdk_diagnostics(tmp_path):
    class BrokenClient:
        api_key = "private-model-client-key"

        def __init__(self):
            self.responses = self

        def create(self, **kwargs):
            raise RuntimeError("private-sdk-diagnostics https://private.invalid/?signature=hidden")

    summary = multiturn.run_suite(client_factory=lambda scenario, turn: BrokenClient(),
                                  scenario_id="follow_up_without_context", results_dir=tmp_path)
    result = load_results(summary, tmp_path)[0]
    assert result["turns"][0]["error"]["code"] == "execution_error"
    assert result["checks"]["passed"] is False
    for path in (tmp_path / summary["run_id"]).rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "private-sdk-diagnostics" not in text
        assert "private-model-client-key" not in text
        assert "signature=hidden" not in text


def test_fixture_time_and_golden_delay_values_are_independently_consistent():
    for scenario in SCENARIOS:
        for turn in scenario["turns"]:
            fixture = json.loads((multiturn.EVALS / turn["fixture"]).read_text(encoding="utf-8"))
            assert fixture["synthetic"] is True
            assert fixture["as_of_utc"] == turn["as_of_utc"]
            reference = datetime.fromisoformat(turn["as_of_utc"])
            assert reference.utcoffset().total_seconds() == 0
            if not turn["expected_facts"].get("departures"):
                continue
            body = fixture["responses"][1]["response"]["body"]
            records = {row["run_ref"]: row for row in body["departures"]}
            for expected in turn["expected_facts"]["departures"]:
                record = records[expected["run_ref"]]
                assert record["stop_id"] == expected["stop_id"]
                scheduled = datetime.fromisoformat(record["scheduled_departure_utc"])
                estimated = datetime.fromisoformat(record["estimated_departure_utc"])
                assert scheduled.utcoffset().total_seconds() == estimated.utcoffset().total_seconds() == 0
                assert record["scheduled_departure_utc"] == expected["scheduled_departure_utc"]
                assert record["estimated_departure_utc"] == expected["estimated_departure_utc"]
                assert scheduled > reference and estimated > reference
                assert (estimated - scheduled).total_seconds() == expected["delay_seconds"]
                assert record["route_id"] == expected["route_id"]
                assert body["runs"][record["run_ref"]]["destination_name"] == expected["destination_name"]
                assert record["platform_number"] == expected["platform_number"]


def test_dataset_rejects_path_escape_without_reading_external_files(tmp_path):
    dataset = deepcopy(DATASET)
    dataset["scenarios"][0]["turns"][0]["fixture"] = "../outside.json"
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid multi-turn"):
        multiturn.load_dataset(path)


def test_offline_cli_runs_all_five_and_saves_reports(tmp_path, capsys):
    assert multiturn.main(["--all", "--results-dir", str(tmp_path)]) == 0
    assert "Scenarios passed: 5/5" in capsys.readouterr().out
    assert len(list(tmp_path.glob("*/summary.json"))) == 1
    assert len(list(tmp_path.glob("*/scenarios/*/result.json"))) == 5
