"""Conversation/fact checks and offline rescoring; never calls an agent or API."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from evals.checks import score_case
from evals.multiturn_answer_checks import check_answer


EMPTY_STATE = {"station_name": None, "departure_limit": None}
CHECKS_VERSION = 3
DATASET_PATH = Path(__file__).resolve().parent / "multiturn_dataset.json"


def check(name, expected, actual):
    return {"id": name, "expected": expected, "actual": actual,
            "passed": json.dumps(expected, sort_keys=True) == json.dumps(actual, sort_keys=True)}


def score_scenario(scenario, result, *, check_answers=False):
    records = result.get("turns", [])
    checks = [check("scenario_identity", scenario["scenario_id"], result.get("scenario_id")),
              check("initial_state_empty", EMPTY_STATE, result.get("initial_state")),
              check("turn_order", [turn["turn_id"] for turn in scenario["turns"]],
                    [turn.get("turn_id") for turn in records])]
    scored_turns = []
    previous_state = result.get("initial_state")
    transports = set()
    destinations = {row["destination_name"] for spec in scenario["turns"]
                    for row in spec["expected_facts"].get("departures", [])}
    for spec, record in zip(scenario["turns"], records):
        turn_checks = [
            check("state_continuity", previous_state, record.get("state_before_reset")),
            check("state_before_reset", spec["expected_state_before_reset"], record.get("state_before_reset")),
            check("reset_applied", spec["reset_before"], record.get("reset_applied")),
            check("state_before", spec["expected_state_before"], record.get("state_before")),
            check("state_after", spec["expected_state_after"], record.get("state_after")),
            check("preferences_forwarded", spec["expected_state_before"], record.get("preferences_sent")),
            check("current_turn_input_only", ["developer", "user"], record.get("initial_input_roles")),
            check("current_question_forwarded", spec["question"], record.get("question_sent")),
            check("http_request_counts", spec["expected_http_counts"], record.get("fixture_coverage", {}).get("match_counts")),
        ]
        instance = record.get("fixture_instance_id")
        turn_checks.append(check("fresh_transport_instance", True,
                                 isinstance(instance, str) and bool(instance) and instance not in transports))
        transports.add(instance)
        case = {"case_id": f"{scenario['scenario_id']}.{spec['turn_id']}",
                "question": spec["question"], "expected_calls": spec["expected_calls"],
                "expected_facts": spec["expected_facts"],
                "required_response_notes": spec["required_response_notes"],
                "prohibited_claims": spec["prohibited_claims"]}
        required = [index + 1 for index, count in enumerate(spec["expected_http_counts"]) if count]
        mechanical = score_case(case, record, required_fixture_indices=required, run_id=result["run_id"])
        calls = record.get("execution_trace", {}).get("tool_calls", [])
        boards = [call["result"] for call in calls
                  if call.get("tool_name") == "get_departures_with_disruptions"
                  and call.get("ok") is True and isinstance(call.get("result", {}).get("board"), dict)]
        departure_calls = [call for call in spec["expected_calls"] if call["name"] == "get_departures_with_disruptions"]
        turn_checks.append(check("departure_board_count", len(departure_calls), len(boards)))
        if departure_calls and len(boards) == 1:
            entries = boards[0]["board"].get("departures", [])
            turn_checks.append(check("departure_limit_respected", True,
                                     len(entries) <= departure_calls[0]["arguments"]["limit"]))
            turn_checks.append(check("current_run_refs", [row["run_ref"] for row in spec["expected_facts"]["departures"]],
                                     [row.get("departure", {}).get("run_ref") for row in entries]))
            reference = datetime.fromisoformat(spec["as_of_utc"]).isoformat()
            turn_checks.append(check("current_retrieval_time", reference, boards[0].get("retrieved_at_utc")))
        if "expected_answer" in spec:
            turn_checks.append(check("clarification_contract", spec["expected_answer"], record.get("final_answer")))
        orchestration_passed = mechanical["automated_checks_passed"] and all(item["passed"] for item in turn_checks)
        answer_checks = check_answer(spec, record.get("final_answer"), known_destinations=destinations) if check_answers else None
        scored_turns.append({"turn_id": spec["turn_id"], "checks": turn_checks,
                             "mechanical": mechanical, "orchestration_passed": orchestration_passed,
                             "answer_checks": answer_checks,
                             "passed": orchestration_passed and (answer_checks is None or answer_checks["passed"])})
        previous_state = record.get("state_after")
    return {"scenario_id": scenario["scenario_id"], "checks": checks, "turns": scored_turns,
            "orchestration_passed": all(item["passed"] for item in checks) and all(turn["orchestration_passed"] for turn in scored_turns),
            "answer_checks_passed": all(turn["answer_checks"]["passed"] for turn in scored_turns) if check_answers else None,
            "passed": all(item["passed"] for item in checks) and all(turn["passed"] for turn in scored_turns)}


def check_isolation(results):
    sessions = [result.get("session_id") for result in results]
    return [check("distinct_scenario_sessions", True,
                  all(isinstance(session, str) and session for session in sessions)
                  and len(sessions) == len(set(sessions))),
            check("all_scenarios_start_empty", True,
                  all(result.get("initial_state") == EMPTY_STATE for result in results))]


def rescore_run(run_dir, *, dataset_path=DATASET_PATH):
    """Read saved records; write a new, versioned report without rewriting sources."""
    root, dataset_path = Path(run_dir).resolve(), Path(dataset_path).resolve()
    manifest_path = root / "summary.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    dataset_hash = sha256(dataset_path.read_bytes()).hexdigest()
    if manifest["dataset_sha256"] != dataset_hash:
        raise ValueError("Saved run does not match the unchanged golden dataset")
    scenarios = {scenario["scenario_id"]: scenario for scenario in dataset["scenarios"]}
    results, rescored = [], []
    seen = set()
    for entry in manifest["scenarios"]:
        name = entry["scenario_id"]
        path = (root / entry["result_file"]).resolve()
        if name not in scenarios or name in seen or not path.is_relative_to(root) or path.suffix != ".json":
            raise ValueError("Invalid saved scenario reference")
        seen.add(name)
        result = json.loads(path.read_text(encoding="utf-8"))
        if result["run_id"] != manifest["run_id"]:
            raise ValueError("Saved scenario belongs to another run")
        specs = {turn["turn_id"]: turn for turn in scenarios[name]["turns"]}
        for turn in result["turns"]:
            fixture = (dataset_path.parent / specs[turn["turn_id"]]["fixture"]).resolve()
            if (not fixture.is_relative_to(dataset_path.parent) or fixture.suffix != ".json"
                    or sha256(fixture.read_bytes()).hexdigest() != turn["fixture_sha256"]):
                raise ValueError("Saved turn does not match its unchanged fixture")
        checks = score_scenario(scenarios[name], result, check_answers=manifest.get("answer_checks_enabled", False))
        results.append(result)
        rescored.append({"scenario_id": name, "source_result_file": entry["result_file"],
                         "source_result_sha256": sha256(path.read_bytes()).hexdigest(), "checks": checks})
    if not results:
        raise ValueError("No saved scenarios")
    isolation = check_isolation(results)
    statuses = [turn["answer_checks"]["status"] for item in rescored for turn in item["checks"]["turns"]
                if turn["answer_checks"] is not None]
    rescore_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:12]
    output = root / "rescores" / rescore_id
    output.mkdir(parents=True, exist_ok=False)
    for item in rescored:
        # Output names come from the trusted dataset, not untrusted result paths.
        index = next(i for i, scenario in enumerate(dataset["scenarios"]) if scenario["scenario_id"] == item["scenario_id"])
        name = f"scenario_{index + 1}.json"
        (output / name).write_text(json.dumps(item, indent=2) + "\n", encoding="utf-8")
        item["checks_file"] = name
    summary = {
        "schema_version": 1, "checks_version": CHECKS_VERSION, "rescore_id": rescore_id,
        "run_id": manifest["run_id"], "source_summary_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
        "dataset_sha256": dataset_hash, "original_checks_version": manifest["checks_version"],
        "original_metrics": {key: manifest.get(key) for key in
                             ("scenarios_passed", "scenarios_orchestration_passed", "turns_answer_checks_passed")},
        "scenarios_attempted": len(rescored), "scenarios_passed": sum(item["checks"]["passed"] for item in rescored),
        "scenarios_orchestration_passed": sum(item["checks"]["orchestration_passed"] for item in rescored),
        "turns_answer_checks_passed": statuses.count("verified"),
        "answer_status_counts": {status: statuses.count(status) for status in
                                 ("verified", "factual_mismatch", "unrecognized_format")},
        "isolation_checks": isolation,
        "passed": all(item["checks"]["passed"] for item in rescored) and all(check["passed"] for check in isolation),
        "scenarios": [{key: item[key] for key in ("scenario_id", "source_result_file", "source_result_sha256", "checks_file")}
                      for item in rescored],
        "scope": "Offline rescoring of saved evidence. Original outputs and manual labels are unchanged; verified means checked facts only, not full answer quality.",
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return output, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Rescore saved multi-turn evaluations offline; preserve original outputs and labels.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    args = parser.parse_args(argv)
    try:
        output, summary = rescore_run(args.run_dir, dataset_path=args.dataset)
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("Unable to rescore. Check saved results, dataset and fixture hashes.") from None
    print(f"Scenarios passed: {summary['scenarios_passed']}/{summary['scenarios_attempted']}; answer statuses: {summary['answer_status_counts']}")
    print("Saved rescore: " + str(output / "summary.json"))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
