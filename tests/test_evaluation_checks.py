from copy import deepcopy
import csv
import json
from pathlib import Path
import socket

import httpx
import httpx2
import pytest

from evals import checks


EVALS = Path(__file__).resolve().parents[1] / "evals"
DATASET = json.loads((EVALS / "dataset.json").read_text(encoding="utf-8"))
CASE_COUNT = len(DATASET["cases"])
RUN_ID = "test-run"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Scoring must not make LLM or PTV requests")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", forbidden)


def saved_result(case):
    """Hand-shaped saved-record format, using literal golden values, not services."""
    facts = case["expected_facts"]
    fixture = json.loads((EVALS / case["fixture"]).read_text(encoding="utf-8"))
    stations = fixture["responses"][0]["response"]["body"]["stops"]
    requested, executed = [], []
    for index, call in enumerate(case["expected_calls"]):
        call_id = f"call-{index}"
        requested.append({"tool_name": call["name"], "arguments": deepcopy(call["arguments"]),
                          "call_id": call_id, "arguments_parse_status": "parsed", "omitted_argument_fields": False})
        if call["name"] == "search_stations":
            result = {"ok": True, "stations": stations}
        elif "expected_error_code" in call:
            result = {"ok": False, "error": {"code": call["expected_error_code"]}}
        else:
            notices = {notice["disruption_id"]: {key: deepcopy(value) for key, value in notice.items()
                                                if key not in {"associated_run_refs", "described_scope", "immediate_passenger_impact"}}
                       for notice in facts.get("notices", [])}
            result = {"ok": True, "board": {
                "stop_id": facts["selected_stop_id"],
                "departures": [{"departure": deepcopy(departure),
                                "associated_disruptions": [deepcopy(notices[notice_id]) for notice_id in departure["disruption_ids"] if notice_id in notices],
                                "unresolved_disruption_ids": [notice_id for notice_id in departure["disruption_ids"] if notice_id in facts["unresolved_disruption_ids"]]} for departure in facts["departures"]],
                "contextual_notices": [{"disruption_id": notice_id} for notice_id in facts["contextual_notice_ids"]],
                "route_notices": {},
            }}
        executed.append({"tool_name": call["name"], "call_id": call_id, "arguments": deepcopy(call["arguments"]),
                         "ok": result["ok"], "result": result})
    descriptions = [record["delay_description"] for record in facts.get("departures", []) if "delay_description" in record]
    return {"run_id": RUN_ID, "case_id": case["case_id"], "question": case["question"], "status": "completed",
            "requested_tool_calls": requested, "execution_trace": {"status": "completed", "tool_calls": executed},
            "final_answer": " ".join(descriptions) or "Retrieved information.", "error": None,
            "fixture_coverage": {"match_counts": [1] * len(fixture["responses"]), "unused_requests": [], "miss_count": 0}}


def score(case, result):
    count = len(json.loads((EVALS / case["fixture"]).read_text(encoding="utf-8"))["responses"])
    return checks.score_case(case, result, required_fixture_indices=list(range(1, count + 1)), run_id=RUN_ID)


@pytest.fixture
def late_case():
    return deepcopy(DATASET["cases"][2])


@pytest.mark.parametrize("case", DATASET["cases"], ids=lambda case: case["case_id"])
def test_golden_shaped_records_pass_only_mechanical_checks(case):
    result = saved_result(case)
    original = deepcopy(result)
    scored = score(case, result)
    assert scored["automated_checks_passed"] is True
    assert all(scored["manual_review"][name] is None for name in checks.MANUAL_FIELDS)
    assert result == original


def test_explicit_alternative_sequences_are_supported(late_case):
    result = saved_result(late_case)
    repeated = deepcopy(result["requested_tool_calls"][0])
    repeated["call_id"] = "call-repeat"
    result["requested_tool_calls"].insert(1, repeated)
    repeated_execution = deepcopy(result["execution_trace"]["tool_calls"][0])
    repeated_execution["call_id"] = "call-repeat"
    result["execution_trace"]["tool_calls"].insert(1, repeated_execution)
    assert score(late_case, result)["metrics"]["tool_selection"]["status"] == "fail"
    late_case["allowed_call_sequences"] = [[late_case["expected_calls"][0], *late_case["expected_calls"]]]
    scored = score(late_case, result)
    assert scored["metrics"]["tool_selection"]["status"] == "pass"
    assert scored["metrics"]["arguments"]["total"] == 3
    assert scored["metrics"]["execution"]["status"] == "pass"


def test_query_variants_require_explicit_declaration(late_case):
    result = saved_result(late_case)
    result["requested_tool_calls"][0]["arguments"]["query"] = "Richmond Station"
    assert score(late_case, result)["metrics"]["arguments"]["status"] == "fail"
    late_case["expected_calls"][0]["query_variants"] = ["Richmond Station"]
    assert score(late_case, result)["metrics"]["arguments"]["status"] == "pass"
    result["requested_tool_calls"][0]["arguments"]["query"] = "  Richmond  "
    assert score(late_case, result)["metrics"]["arguments"]["status"] == "pass"


def test_expected_tool_failure_requires_explicit_golden_annotation():
    case = deepcopy(next(case for case in DATASET["cases"] if case["case_id"] == "ptv_api_failure"))
    result = saved_result(case)
    scored = score(case, result)
    assert scored["automated_checks_passed"] is True
    assert scored["execution_completed"] is True
    assert scored["manual_review"]["grounding"] is None
    assert scored["metrics"]["execution"]["checks"][-1]["id"] == "expected_tool_error.2"
    case["expected_calls"][1].pop("expected_error_code")
    scored = score(case, result)
    assert scored["metrics"]["execution"]["status"] == "fail"
    assert scored["metrics"]["execution"]["checks"][-1]["id"] == "successful_tool.2"


@pytest.mark.parametrize("mutation", ["wrong_code", "missing_error", "empty_success", "error_with_board", "wrong_call_id", "wrong_executed_arguments"])
def test_expected_failure_check_rejects_wrong_or_uncorrelated_outcomes(mutation):
    case = next(case for case in DATASET["cases"] if case["case_id"] == "ptv_api_failure")
    result = saved_result(case)
    call = result["execution_trace"]["tool_calls"][1]
    if mutation == "wrong_code":
        call["result"]["error"]["code"] = "unverified_stop_id"
    elif mutation == "missing_error":
        call["result"].pop("error")
    elif mutation == "empty_success":
        call["ok"] = True
        call["result"] = {"ok": True, "board": {"stop_id": 1101, "departures": []}}
    elif mutation == "error_with_board":
        call["result"]["board"] = {"stop_id": 1101, "departures": []}
    elif mutation == "wrong_call_id":
        call["call_id"] = "uncorrelated-error"
    else:
        call["arguments"]["stop_id"] = 1102
    assert score(case, result)["metrics"]["execution"]["status"] == "fail"


@pytest.mark.parametrize("key,value", [("stop_id", "1101"), ("stop_id", 1102), ("limit", 3), ("limit", 5.0), ("limit", True)])
def test_ids_and_limits_are_exact_and_type_checked(late_case, key, value):
    result = saved_result(late_case)
    result["requested_tool_calls"][1]["arguments"][key] = value
    scored = score(late_case, result)
    assert scored["execution_completed"] is True
    assert scored["metrics"]["tool_selection"]["status"] == "pass"
    assert scored["metrics"]["arguments"]["status"] == "fail"
    assert scored["automated_checks_passed"] is False


def test_explicit_requested_limit_is_compared_with_golden_not_a_default(late_case):
    late_case["expected_calls"][1]["arguments"]["limit"] = 3
    result = saved_result(late_case)
    assert score(late_case, result)["metrics"]["arguments"]["status"] == "pass"
    result["requested_tool_calls"][1]["arguments"]["limit"] = 5
    assert score(late_case, result)["metrics"]["arguments"]["status"] == "fail"


@pytest.mark.parametrize("kind", ["fixture_miss", "unused", "tool_failure", "unknown_tool", "missing_call", "duplicate_id"])
def test_execution_completeness_detects_independent_failures(late_case, kind):
    result = saved_result(late_case)
    if kind == "fixture_miss":
        result["fixture_coverage"]["miss_count"] = 1
    elif kind == "unused":
        result["fixture_coverage"]["match_counts"][1] = 0
    elif kind == "tool_failure":
        result["execution_trace"]["tool_calls"][1]["ok"] = False
    elif kind == "unknown_tool":
        result["requested_tool_calls"].append({"tool_name": "unknown", "arguments": {}, "call_id": "call-extra"})
    elif kind == "missing_call":
        result["requested_tool_calls"].pop()
    else:
        result["requested_tool_calls"][1]["call_id"] = result["requested_tool_calls"][0]["call_id"]
    scored = score(late_case, result)
    assert scored["metrics"]["execution"]["status"] == "fail"
    assert scored["execution_completed"] is True
    assert scored["automated_checks_passed"] is False


def test_source_facts_compare_independent_golden_values_and_include_evidence(late_case):
    result = saved_result(late_case)
    result["execution_trace"]["tool_calls"][1]["result"]["board"]["departures"][0]["departure"]["delay_seconds"] = -30
    scored = score(late_case, result)
    failures = [check for check in scored["metrics"]["source_facts"]["checks"] if not check["passed"]]
    assert len(failures) == 1
    assert failures[0]["expected"] == 30 and failures[0]["actual"] == -30
    assert failures[0]["id"] == "departure.310004.delay_seconds"
    assert "tool_calls[1].result.board.departures[0].departure.delay_seconds" in failures[0]["evidence"]


def test_wrong_early_wording_fails_only_the_defined_contract(late_case):
    result = saved_result(late_case)
    result["final_answer"] = "Estimated 30 seconds early relative to the schedule."
    scored = score(late_case, result)
    assert scored["metrics"]["source_facts"]["status"] == "pass"
    assert scored["metrics"]["timing_wording"]["status"] == "fail"
    assert scored["manual_review"]["factual_accuracy"] is None


@pytest.mark.parametrize("case_id,passenger_answer", [
    ("richmond_30_seconds_late", "Estimate is close to the schedule; timing may change."),
    ("richmond_30_seconds_early", "Slightly early (estimated)."),
    ("mixed_departure_delays", "Slightly early (estimated). About 2 minutes late (estimated). "
     "Timing comparison unavailable: a scheduled time or real-time estimate is missing."),
])
def test_new_passenger_wording_does_not_silently_pass_frozen_exact_seconds_contract(case_id, passenger_answer):
    case = next(case for case in DATASET["cases"] if case["case_id"] == case_id)
    result = saved_result(case)
    result["final_answer"] = passenger_answer
    scored = score(case, result)
    assert scored["metrics"]["source_facts"]["status"] == "pass"
    assert scored["metrics"]["timing_wording"]["status"] == "fail"
    assert scored["automated_checks_passed"] is False
    assert scored["manual_review"]["factual_accuracy"] is None


def test_sentence_presence_is_not_a_grounding_or_accuracy_score(late_case):
    result = saved_result(late_case)
    result["final_answer"] += " Actually, the train is early and guaranteed to arrive on time."
    scored = score(late_case, result)
    assert scored["metrics"]["timing_wording"]["status"] == "pass"
    assert scored["manual_review"]["factual_accuracy"] is None
    assert scored["manual_review"]["grounding"] is None
    aggregate = checks.aggregate_scores([scored])
    assert aggregate["manual_review"]["fully_reviewed_cases"] == 0
    assert "factual_accuracy" not in aggregate["metrics"]


@pytest.fixture
def saved_run(tmp_path):
    root = tmp_path / RUN_ID
    root.mkdir()
    entries = []
    for case in DATASET["cases"]:
        relative = f"cases/{case['case_id']}/result.json"
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(saved_result(case)), encoding="utf-8")
        entries.append({"case_id": case["case_id"], "result_file": relative})
    (root / "summary.json").write_text(json.dumps({"run_id": RUN_ID, "cases": entries}), encoding="utf-8")
    return root


def test_csv_json_aggregates_and_manual_nulls(saved_run):
    before = {path: path.read_bytes() for path in saved_run.rglob("*.json")}
    aggregate = checks.score_run(saved_run)
    assert aggregate["cases"] == aggregate["execution_completed"] == CASE_COUNT
    assert aggregate["metrics"]["tool_selection"]["denominator"] == CASE_COUNT
    assert aggregate["metrics"]["arguments"]["checks_total"] == sum(len(case["expected_calls"]) for case in DATASET["cases"])
    timing_cases = sum(any("delay_description" in row for row in case["expected_facts"].get("departures", [])) for case in DATASET["cases"])
    assert aggregate["metrics"]["timing_wording"]["denominator"] == timing_cases
    assert aggregate["metrics"]["timing_wording"]["not_applicable"] == CASE_COUNT - timing_cases
    assert aggregate["manual_review"]["fully_reviewed_cases"] == 0
    rows = list(csv.DictReader((saved_run / "scores/cases.csv").open(encoding="utf-8", newline="")))
    assert len(rows) == CASE_COUNT
    assert all(row["manual_factual_accuracy"] == row["manual_grounding"] == row["manual_response_relevance"] == "" for row in rows)
    assert all(row["review_status"] == "manual_review_pending" for row in rows)
    details = json.loads((saved_run / "scores/checks.json").read_text(encoding="utf-8"))
    assert details["cases"][0]["manual_review"]["factual_accuracy"] is None
    assert all(path.read_bytes() == content for path, content in before.items())


def test_missing_result_is_a_failure_without_shrinking_denominators(saved_run):
    (saved_run / "cases/richmond_30_seconds_late/result.json").unlink()
    aggregate = checks.score_run(saved_run)
    assert aggregate["cases"] == CASE_COUNT
    assert aggregate["metrics"]["tool_selection"]["denominator"] == CASE_COUNT
    assert aggregate["metrics"]["tool_selection"]["passed"] == CASE_COUNT - 1
    assert aggregate["metrics"]["execution"]["failed"] == 1


def test_manual_zero_scores_and_notes_survive_rescoring(saved_run):
    checks.score_run(saved_run)
    path = saved_run / "scores/cases.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        rows, columns = list(reader), reader.fieldnames
    rows[0].update(manual_factual_accuracy="0", manual_grounding="1", manual_response_relevance="2", reviewer_notes="Needs work.\nCheck the estimate.")
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    aggregate = checks.score_run(saved_run)
    assert aggregate["manual_review"]["fully_reviewed_cases"] == 1
    assert aggregate["manual_review"]["factual_accuracy"] == {
        "denominator": CASE_COUNT, "reviewed": 1, "coverage": 1 / CASE_COUNT, "mean": 0.0}
    details = json.loads((saved_run / "scores/checks.json").read_text(encoding="utf-8"))
    assert details["cases"][0]["manual_review"]["reviewer_notes"] == rows[0]["reviewer_notes"]
    assert details["cases"][0]["review_status"] == "review_complete"


def test_invalid_manual_score_is_rejected_without_overwriting_labels(saved_run):
    checks.score_run(saved_run)
    path = saved_run / "scores/cases.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        rows, columns = list(reader), reader.fieldnames
    rows[0]["manual_grounding"] = "3"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    invalid = path.read_bytes()
    with pytest.raises(ValueError, match="Invalid manual review"):
        checks.score_run(saved_run)
    assert path.read_bytes() == invalid


def test_result_paths_cannot_escape_saved_run(saved_run):
    path = saved_run / "summary.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["cases"][0]["result_file"] = "../../outside.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="outside the run directory"):
        checks.score_run(saved_run)


def test_scoring_cli_is_offline(saved_run, capsys):
    assert checks.main(["--run-dir", str(saved_run)]) == 0
    assert f"tool_selection: {CASE_COUNT}/{CASE_COUNT}" in capsys.readouterr().out
