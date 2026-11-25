from copy import deepcopy
import csv
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import httpx
import httpx2
from openai import APIError
import pytest

import agent
from conversation import ConversationState
from evals import multiturn
from evals.multiturn_answer_checks import check_answer
from evals.multiturn_review import summarize_reviews
from ptv import PTVConfig


RICHMOND = """From Richmond:
- Glen Waverley: estimated 09:58 (scheduled 09:58), platform 9.
- Belgrave: estimated 10:02 (scheduled 10:02), platform 10.
- Lilydale: estimated 10:06 (scheduled 10:06), platform 10."""
REFRESHED = """From Richmond:
- Glen Waverley: estimated 10:00 (scheduled 09:59), platform 8, about 1 minute late.
- Belgrave: estimated 10:03 (scheduled 10:03), platform 10."""
FLINDERS = """From Flinders Street:
- Glen Waverley: estimated 10:00 (scheduled 10:00), platform 3.
An associated notice describes overnight works; it does not establish impact on this morning service."""


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("This test must not construct real clients, load credentials or use live network transports")

    monkeypatch.setattr(multiturn, "OpenAI", forbidden)
    monkeypatch.setattr(multiturn, "load_dotenv", forbidden)
    monkeypatch.setattr(agent, "OpenAI", forbidden)
    monkeypatch.setattr(agent, "load_dotenv", forbidden)
    monkeypatch.setattr(PTVConfig, "from_env", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class FakeOpenAI:
    api_key = "private-fake-openai-key"

    def __init__(self, replies):
        self.responses = self
        self.replies = iter(replies)
        self.requests = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        index = len(self.requests)
        output = [SimpleNamespace(type="function_call", name=reply["tool"],
                                  arguments=json.dumps(reply["arguments"]), call_id=f"fake-call-{index}")] if "tool" in reply else []
        return SimpleNamespace(id=f"fake-response-{index}", model="fake-model-snapshot-2026-11-01",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120),
                               status="completed", output=output, output_text=reply.get("answer", ""))


def replies(*, scenario_id=None, stale_answer=False, failed_followup=False, wrong_limit=False):
    scripts = json.loads(multiturn.SCRIPTS_PATH.read_text(encoding="utf-8"))["scenarios"]
    values = []
    for scenario, turns in scripts.items():
        if scenario_id is not None and scenario != scenario_id:
            continue
        for turn, messages in turns.items():
            if failed_followup and scenario == "retain_station_context" and turn == "2":
                values.append(APIError("private-sdk-body", request=httpx.Request("POST", "https://private.invalid"), body=None))
                continue
            for message in deepcopy(messages):
                if "answer" in message and any("get_departures" in item.get("tool", "") for item in messages):
                    message["answer"] = FLINDERS if scenario == "change_station" and turn == "2" else RICHMOND
                    if scenario == "retain_station_context" and turn == "2":
                        message["answer"] = RICHMOND if stale_answer else REFRESHED
                if wrong_limit and scenario == "retain_station_context" and turn == "2" and "limit" in message.get("arguments", {}):
                    message["arguments"]["limit"] = 5
                values.append(message)
    return values


def install_fake(monkeypatch, values):
    client = FakeOpenAI(values)
    creations = []
    loads = []

    def factory(**kwargs):
        creations.append(kwargs)
        return client

    monkeypatch.setattr(multiturn, "OpenAI", factory)
    monkeypatch.setattr(multiturn, "load_dotenv", lambda *args, **kwargs: loads.append((args, kwargs)))
    monkeypatch.setenv("OPENAI_API_KEY", client.api_key)
    monkeypatch.setenv("OPENAI_MODEL", "fake-model-alias")
    return client, creations, loads


def saved(directory):
    path = next(directory.glob("*/summary.json"))
    summary = json.loads(path.read_text(encoding="utf-8"))
    results = [json.loads((path.parent / item["result_file"]).read_text(encoding="utf-8")) for item in summary["scenarios"]]
    return path.parent, summary, results


@pytest.mark.parametrize("argv", [[], ["--all"]])
def test_default_cli_never_loads_credentials_or_constructs_openai_even_with_keys(argv, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key-present-but-no-permission-to-call")
    monkeypatch.setenv("OPENAI_MODEL", "model-present-but-no-permission-to-call")
    assert multiturn.main([*argv, "--results-dir", str(tmp_path)]) == 0
    _, summary, _ = saved(tmp_path)
    assert summary["mode"] == "scripted-offline"
    assert summary["answer_checks_enabled"] is False
    assert summary["scenarios_passed"] == 5
    assert summary["total_token_usage"] == {"input_tokens": None, "output_tokens": None, "total_tokens": None}


def test_programmatic_real_mode_requires_explicit_opt_in():
    with pytest.raises(ValueError, match="explicit"):
        multiturn.run_real_suite()


@pytest.mark.parametrize("argv", [["--model", "fake"], ["--real-llm", "--review-run", "unused"]])
def test_cli_rejects_implicit_or_incompatible_real_mode(argv):
    with pytest.raises(SystemExit) as caught:
        multiturn.main(argv)
    assert caught.value.code == 2


@pytest.mark.parametrize("missing", ["OPENAI_API_KEY", "OPENAI_MODEL"])
def test_real_mode_checks_configuration_before_client_creation(missing, monkeypatch, tmp_path):
    monkeypatch.setattr(multiturn, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("OPENAI_MODEL", "fake-model")
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match=missing):
        multiturn.run_real_suite(enabled=True, results_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_real_cli_runs_actual_agent_with_fake_sdk_and_only_fixture_ptv(tmp_path, monkeypatch):
    client, creations, loads = install_fake(monkeypatch, replies())
    states = []

    def new_state():
        state = ConversationState()
        states.append(state)
        return state

    monkeypatch.setattr(multiturn, "ConversationState", new_state)
    assert multiturn.main(["--real-llm", "--all", "--model", "explicit-model", "--results-dir", str(tmp_path)]) == 0
    assert creations == [{"timeout": 30.0, "max_retries": 0}]
    assert len(loads) == 1 and client.closed is True
    root, summary, results = saved(tmp_path)
    assert summary["mode"] == "real-llm" and summary["answer_checks_enabled"] is True
    assert summary["scenarios_passed"] == summary["scenarios_orchestration_passed"] == 5
    assert summary["turns_completed"] == summary["turns_answer_checks_passed"] == 8
    assert summary["fixture_misses"] == summary["turns_with_errors"] == 0
    assert summary["total_token_usage"] == {"input_tokens": 1800, "output_tokens": 360, "total_tokens": 2160}
    assert summary["token_usage_reporting"]["model_requests"] == len(client.requests) == 18
    assert summary["configuration"]["models_returned"] == ["fake-model-snapshot-2026-11-01"]
    assert summary["configuration"]["model_requested"] == "explicit-model"
    assert len(summary["configuration"]["instructions_sha256"]) == 64
    assert summary["configuration"]["openai_sdk_version"]
    assert len(states) == len({id(state) for state in states}) == 5
    assert all(check["passed"] for check in summary["isolation_checks"])
    assert len({turn["fixture_instance_id"] for item in results for turn in item["turns"]}) == 8
    second = results[0]["turns"][1]
    assert second["preferences_sent"] == {"station_name": "Richmond", "departure_limit": 5}
    assert second["question_sent"] == "What about the next two?"
    assert second["fixture_coverage"]["match_counts"] == [1, 1]
    assert second["requested_tool_calls"][1]["arguments"] == {"stop_id": 1101, "limit": 2}
    board = second["execution_trace"]["tool_calls"][1]["result"]["board"]
    assert [row["departure"]["run_ref"] for row in board["departures"]] == ["310101", "310102"]
    assert second["final_answer"] == REFRESHED
    assert second["fixture_id"] == "richmond_followup_two"
    assert len(second["fixture_sha256"]) == 64
    assert results[1]["turns"][1]["state_after"]["station_name"] == "Flinders Street"
    assert results[4]["turns"][1]["state_before"] == {"station_name": None, "departure_limit": None}
    for result in results:
        for scored in result["checks"]["turns"]:
            assert scored["mechanical"]["manual_review"] == {"factual_accuracy": None, "grounding": None,
                                                           "response_relevance": None, "reviewer_notes": ""}
    assert all(request["store"] is False and "previous_response_id" not in request for request in client.requests)
    assert client.api_key not in "".join(path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file())


def test_single_real_scenario_and_new_directories_preserve_previous_results(tmp_path, monkeypatch):
    old = tmp_path / "prior-run"
    old.mkdir()
    sentinel = old / "result.json"
    sentinel.write_text('{"keep":"prior results and manual labels"}', encoding="utf-8")
    before = sentinel.read_bytes()
    for _ in range(2):
        install_fake(monkeypatch, replies(scenario_id="retain_station_context"))
        assert multiturn.main(["--real-llm", "--scenario", "retain_station_context", "--results-dir", str(tmp_path)]) == 0
    assert sentinel.read_bytes() == before
    assert len(list(tmp_path.glob("*/summary.json"))) == 2


def test_successful_tools_do_not_make_a_stale_final_answer_pass(tmp_path, monkeypatch):
    install_fake(monkeypatch, replies(stale_answer=True))
    assert multiturn.main(["--real-llm", "--all", "--results-dir", str(tmp_path)]) == 1
    _, summary, results = saved(tmp_path)
    assert summary["turns_completed"] == 8 and summary["turns_with_errors"] == 0
    assert summary["scenarios_orchestration_passed"] == 5
    assert summary["scenarios_passed"] == 4 and summary["turns_answer_checks_passed"] == 7
    followup = results[0]["checks"]["turns"][1]
    assert followup["orchestration_passed"] is True and followup["answer_checks"]["passed"] is False
    failed_ids = {check["id"] for check in followup["answer_checks"]["checks"] if not check["passed"]}
    assert {"recognized_service_count", "answer.310101.estimated_local", "answer.310101.platform"} <= failed_ids


def test_fixture_miss_in_real_mode_is_controlled_and_cannot_reach_live_ptv(tmp_path, monkeypatch):
    install_fake(monkeypatch, replies(scenario_id="retain_station_context", wrong_limit=True))
    summary = multiturn.run_real_suite(enabled=True, scenario_id="retain_station_context", results_dir=tmp_path)
    assert summary["fixture_misses"] == summary["turns_with_errors"] == 1
    _, _, results = saved(tmp_path)
    assert results[0]["turns"][1]["error"]["code"] == "fixture_miss"


def test_real_model_failure_is_sanitized_and_other_scenarios_continue(tmp_path, monkeypatch):
    install_fake(monkeypatch, replies(failed_followup=True))
    summary = multiturn.run_real_suite(enabled=True, results_dir=tmp_path)
    root, _, results = saved(tmp_path)
    assert summary["turns_with_errors"] == 1
    assert summary["scenarios_passed"] == 4
    assert summary["token_usage_reporting"]["responses_missing_usage"] == 1
    assert results[0]["turns"][1]["error"]["code"] == "llm_api_error"
    assert results[1]["initial_state"] == {"station_name": None, "departure_limit": None}
    assert "private-sdk-body" not in "".join(path.read_text(encoding="utf-8") for path in root.rglob("*.json"))


def test_manual_scores_start_blank_and_offline_review_preserves_original_records(tmp_path, monkeypatch):
    install_fake(monkeypatch, replies())
    multiturn.run_real_suite(enabled=True, results_dir=tmp_path)
    root, _, _ = saved(tmp_path)
    originals = {path: path.read_bytes() for path in root.rglob("*.json")}
    with (root / "reviews.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 8
    assert all(row["manual_factual_accuracy"] == row["manual_grounding"] == row["manual_response_relevance"] == "" for row in rows)
    assert summarize_reviews(root)["manual_review"]["grounding"] == {"denominator": 8, "reviewed": 0, "coverage": 0.0, "mean": None}
    rows[1].update(manual_factual_accuracy="2", manual_grounding="2", manual_response_relevance="1",
                   reviewer_notes="Checked against the refreshed board, including platform 8.")
    with (root / "reviews.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    monkeypatch.setattr(multiturn, "OpenAI", lambda **kwargs: pytest.fail("Review is offline"))
    monkeypatch.setattr(multiturn, "load_dotenv", lambda *args, **kwargs: pytest.fail("Review needs no credentials"))
    assert multiturn.main(["--review-run", str(root)]) == 0
    review = json.loads((root / "review_summary.json").read_text(encoding="utf-8"))
    assert review["fully_reviewed_turns"] == 1
    assert review["manual_review"]["response_relevance"] == {"denominator": 8, "reviewed": 1, "coverage": 0.125, "mean": 1.0}
    assert all(path.read_bytes() == value for path, value in originals.items())
    rows[0]["manual_grounding"] = "3"
    with (root / "reviews.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="0, 1, 2"):
        summarize_reviews(root)


def refreshed_spec():
    return multiturn.load_dataset()["scenarios"][0]["turns"][1]


@pytest.mark.parametrize("answer", [REFRESHED, """From Richmond:
| Destination | Estimated | Scheduled | Platform |
| --- | --- | --- | --- |
| Glen Waverley | 10:00 am | 9:59 am | 8 |
| Belgrave | 10:03 am | 10:03 am | 10 |
"""])
def test_answer_checks_bind_times_and_platforms_to_each_destination(answer):
    assert check_answer(refreshed_spec(), answer)["passed"] is True


@pytest.mark.parametrize("answer", [RICHMOND,
                                   REFRESHED.replace("platform 8", "platform 9"),
                                   REFRESHED.replace("estimated 10:00", "estimated 09:58"),
                                   REFRESHED.replace("Glen Waverley", "SWAP").replace("Belgrave", "Glen Waverley").replace("SWAP", "Belgrave"),
                                   REFRESHED.replace("(scheduled 09:59)", ""),
                                   REFRESHED + "\n- Lilydale: estimated 10:06 (scheduled 10:06), platform 10.",
                                   "Richmond: two new services were retrieved."])
def test_answer_checks_reject_stale_swapped_extra_or_unsupported_service_facts(answer):
    assert check_answer(refreshed_spec(), answer, known_destinations={"Lilydale"})["passed"] is False
