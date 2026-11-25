from hashlib import sha256
import json
from pathlib import Path
import socket

import httpx
import httpx2
import pytest

import agent
from evals import multiturn, multiturn_checks
from evals.multiturn_answer_checks import check_answer
from ptv import PTVConfig


CORPUS = json.loads((Path(__file__).parent / "data" / "multiturn_saved_answers.json").read_text(encoding="utf-8"))
SCENARIOS = json.loads(multiturn.DATASET_PATH.read_text(encoding="utf-8"))["scenarios"]
SPECS = {(scenario["scenario_id"], turn["turn_id"]): turn for scenario in SCENARIOS for turn in scenario["turns"]}
ANSWERS = {(sample["scenario_id"], sample["turn_id"]): sample["answer"] for sample in CORPUS["answers"]}
FOLLOWUP = ANSWERS["retain_station_context", "2"]
FOLLOWUP_SPEC = SPECS["retain_station_context", "2"]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Answer recognition and rescoring must remain fully offline")

    monkeypatch.setattr(multiturn, "OpenAI", forbidden)
    monkeypatch.setattr(multiturn, "load_dotenv", forbidden)
    monkeypatch.setattr(agent, "OpenAI", forbidden)
    monkeypatch.setattr(agent, "load_dotenv", forbidden)
    monkeypatch.setattr(PTVConfig, "from_env", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("sample", CORPUS["answers"], ids=lambda row: row["scenario_id"] + "." + row["turn_id"])
def test_actual_saved_formats_verify_against_unchanged_golden_facts(sample):
    spec = SPECS[sample["scenario_id"], sample["turn_id"]]
    result = check_answer(spec, sample["answer"])
    assert result["status"] == "verified" and result["passed"] is True
    assert all(check["status"] == "verified" for check in result["checks"])
    if spec["expected_facts"].get("departures"):
        count = next(check for check in result["checks"] if check["id"] == "recognized_service_count")
        assert count["actual"] == count["expected"] == len(spec["expected_facts"]["departures"])
        assert any("_date." in check["id"] for check in result["checks"])
        assert any("_timezone." in check["id"] for check in result["checks"])


@pytest.mark.parametrize("mutation, expected_check", [
    ("wrong_time", "answer.310101.estimated_local"),
    ("wrong_platform", "answer.310101.platform"),
    ("missing_service", "recognized_service_count"),
    ("invented_service", "recognized_service_count"),
    ("duplicate_service", "answer.310101.unique_service_row"),
    ("stale_board", "answer.310101.estimated_local"),
    ("swapped_destinations", "answer.310101.estimated_local"),
    ("wrong_date", "answer.310101.scheduled_date.0"),
    ("wrong_timezone", "answer.310101.estimated_timezone.0"),
])
def test_recognized_incorrect_answers_are_factual_mismatches(mutation, expected_check):
    answer = FOLLOWUP
    if mutation == "wrong_time":
        answer = answer.replace("10:00:00 AEDT", "09:58:00 AEDT")
    elif mutation == "wrong_platform":
        answer = answer.replace("Platform 8", "Platform 9")
    elif mutation == "missing_service":
        answer = answer.split("\n2.")[0]
    elif mutation in {"invented_service", "duplicate_service"}:
        destination = "Hurstbridge" if mutation == "invented_service" else "Glen Waverley"
        answer += f"\n3. {destination} - Platform 11\n   Estimated departure: 2026-11-18 10:07:00 AEDT; scheduled departure: 2026-11-18 10:07:00 AEDT."
    elif mutation == "stale_board":
        answer = ANSWERS["retain_station_context", "1"]
    elif mutation == "swapped_destinations":
        answer = answer.replace("Glen Waverley", "SWAP").replace("Belgrave", "Glen Waverley").replace("SWAP", "Belgrave")
    elif mutation == "wrong_date":
        answer = answer.replace("scheduled departure: 2026-11-18 09:59", "scheduled departure: 2026-11-19 09:59")
    elif mutation == "wrong_timezone":
        answer = answer.replace("10:00:00 AEDT", "10:00:00 AEST")
    checked = check_answer(FOLLOWUP_SPEC, answer, known_destinations={"Lilydale"})
    assert checked["status"] == "factual_mismatch" and checked["passed"] is False
    assert any(check["id"] == expected_check and check["status"] == "factual_mismatch" for check in checked["checks"])


@pytest.mark.parametrize("answer", [
    "Richmond has a Glen Waverley service at ten and then a Belgrave service.",
    FOLLOWUP.replace("scheduled departure:", "timetable value:"),
    FOLLOWUP.replace("AEDT", "XYZ"),
])
def test_unrecognized_wording_needs_review_and_does_not_become_a_verified_answer(answer):
    checked = check_answer(FOLLOWUP_SPEC, answer)
    assert checked["status"] == "unrecognized_format" and checked["passed"] is False
    assert all(check["status"] != "factual_mismatch" for check in checked["checks"])


@pytest.mark.parametrize("marker", ["numbered_parentheses", "star_bullets", "bold_labels"])
def test_equivalent_markdown_presentation_preserves_service_associations(marker):
    answer = FOLLOWUP
    if marker == "numbered_parentheses":
        answer = answer.replace("\n1.", "\n1)").replace("\n2.", "\n2)")
    elif marker == "star_bullets":
        answer = answer.replace("\n1.", "\n*").replace("\n2.", "\n*")
    else:
        answer = answer.replace("Estimated departure:", "**Estimated departure:**").replace("scheduled departure:", "**scheduled departure:**")
    assert check_answer(FOLLOWUP_SPEC, answer)["status"] == "verified"


def test_markdown_table_dates_are_checked_not_discarded():
    answer = """From Richmond:
| Destination | Estimated | Scheduled | Platform |
| --- | --- | --- | --- |
| Glen Waverley | 2026-11-18 10:00:00 AEDT | 2026-11-18 09:59:00 AEDT | 8 |
| Belgrave | 2026-11-18 10:03:00 AEDT | 2026-11-18 10:03:00 AEDT | 10 |
"""
    assert check_answer(FOLLOWUP_SPEC, answer)["status"] == "verified"
    assert check_answer(FOLLOWUP_SPEC, answer.replace("2026-11-18", "2026-11-19"))["status"] == "factual_mismatch"


def test_notice_explanation_is_not_recognized_as_a_second_departure():
    answer = ANSWERS["change_station", "2"]
    checked = check_answer(SPECS["change_station", "2"], answer)
    count = next(check for check in checked["checks"] if check["id"] == "recognized_service_count")
    assert count["actual"] == 1 and checked["status"] == "verified"


def test_extra_service_with_unsupported_labels_cannot_silently_disappear():
    answer = FOLLOWUP + "\n3. Hurstbridge leaves at 10:07."
    checked = check_answer(FOLLOWUP_SPEC, answer)
    assert checked["passed"] is False and checked["status"] == "unrecognized_format"
    assert any(check["id"] == "recognized_service_count" and check["actual"] == 3 for check in checked["checks"])


@pytest.fixture
def saved_run(tmp_path):
    # Build real traces through fixed PTV responses, then insert verbatim saved prose.
    manifest = multiturn.run_suite(results_dir=tmp_path)
    root = tmp_path / manifest["run_id"]
    for entry in manifest["scenarios"]:
        path = root / entry["result_file"]
        result = json.loads(path.read_text(encoding="utf-8"))
        for turn in result["turns"]:
            turn["final_answer"] = ANSWERS[result["scenario_id"], turn["turn_id"]]
        path.write_text(json.dumps(result), encoding="utf-8")
    manifest.update(answer_checks_enabled=True, checks_version=2, scenarios_passed=2, turns_answer_checks_passed=3)
    (root / "summary.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "review_summary.json").write_text('{"reviewer_notes":"Keep existing manual labels"}', encoding="utf-8")
    return root


def test_offline_rescore_writes_new_reports_and_preserves_original_outputs_and_reviews(saved_run, monkeypatch, capsys):
    originals = {path: sha256(path.read_bytes()).hexdigest() for path in saved_run.rglob("*") if path.is_file()}
    monkeypatch.setattr(multiturn, "answer_question", lambda *args, **kwargs: pytest.fail("Rescoring must not execute the agent"))
    for _ in range(2):
        assert multiturn_checks.main(["--run-dir", str(saved_run)]) == 0
    assert "Scenarios passed: 5/5" in capsys.readouterr().out
    reports = list(saved_run.glob("rescores/*/summary.json"))
    assert len(reports) == 2
    for report in reports:
        summary = json.loads(report.read_text(encoding="utf-8"))
        assert summary["original_checks_version"] == 2 and summary["checks_version"] == 3
        assert summary["original_metrics"]["turns_answer_checks_passed"] == 3
        assert summary["scenarios_orchestration_passed"] == summary["scenarios_passed"] == 5
        assert summary["answer_status_counts"] == {"verified": 8, "factual_mismatch": 0, "unrecognized_format": 0}
        assert all((report.parent / item["checks_file"]).is_file() for item in summary["scenarios"])
    assert all(sha256(path.read_bytes()).hexdigest() == value for path, value in originals.items())


def test_rescoring_bad_answer_keeps_tool_success_separate(saved_run):
    path = saved_run / "scenarios" / "retain_station_context" / "result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    result["turns"][1]["final_answer"] = FOLLOWUP.replace("Platform 8", "Platform 9")
    path.write_text(json.dumps(result), encoding="utf-8")
    _, summary = multiturn_checks.rescore_run(saved_run)
    assert summary["scenarios_orchestration_passed"] == 5
    assert summary["scenarios_passed"] == 4
    assert summary["answer_status_counts"] == {"verified": 7, "factual_mismatch": 1, "unrecognized_format": 0}


@pytest.mark.parametrize("invalid", ["dataset_hash", "fixture_hash", "path_escape"])
def test_rescore_rejects_mismatched_sources_before_writing_a_report(saved_run, invalid):
    manifest_path = saved_run / "summary.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if invalid == "dataset_hash":
        manifest["dataset_sha256"] = "0" * 64
    elif invalid == "fixture_hash":
        path = saved_run / manifest["scenarios"][0]["result_file"]
        result = json.loads(path.read_text(encoding="utf-8"))
        result["turns"][0]["fixture_sha256"] = "0" * 64
        path.write_text(json.dumps(result), encoding="utf-8")
    else:
        manifest["scenarios"][0]["result_file"] = "../outside.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        multiturn_checks.rescore_run(saved_run)
    assert not (saved_run / "rescores").exists()
