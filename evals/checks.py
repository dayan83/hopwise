"""Offline mechanical checks and unfilled manual review labels for saved runs.

Source-record comparisons and literal wording contracts do not establish complete
answer accuracy or grounding. No agent, service, SDK or production calculation is
imported or executed here.
"""

import argparse
from collections import Counter
import csv
from hashlib import sha256
import json
from pathlib import Path


DATASET_PATH = Path(__file__).resolve().parent / "dataset.json"
METRICS = ("tool_selection", "arguments", "execution", "source_facts", "timing_wording")
MANUAL_FIELDS = ("factual_accuracy", "grounding", "response_relevance")
_MISSING = object()


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Unable to read scoring input JSON.") from None


def _equal(expected, actual):
    if actual is _MISSING:
        return False
    if type(expected) in (int, float) and type(actual) in (int, float):
        return expected == actual
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(_equal(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(_equal(a, b) for a, b in zip(expected, actual))
    return expected == actual


def _check(name, expected, actual, evidence):
    return {"id": name, "passed": _equal(expected, actual), "expected": expected,
            "actual": "[missing]" if actual is _MISSING else actual, "evidence": evidence}


def _metric(checks):
    passed = sum(check["passed"] for check in checks)
    return {"status": "not_applicable" if not checks else "pass" if passed == len(checks) else "fail",
            "passed": passed, "total": len(checks), "checks": checks}


def _arguments_match(expected_call, actual_call):
    expected, actual = expected_call["arguments"], actual_call.get("arguments")
    if (not isinstance(actual, dict) or actual.keys() != expected.keys()
            or actual_call.get("omitted_argument_fields", False)
            or actual_call.get("arguments_parse_status", "parsed") != "parsed"):
        return False
    for key, value in expected.items():
        candidate = actual[key]
        if key == "query":
            variants = [value, *expected_call.get("query_variants", [])]
            if not isinstance(candidate, str) or candidate.strip() not in variants:
                return False
        elif not (type(candidate) is type(value) and candidate == value):
            # IDs and limits must not accept bools, floats or numeric strings.
            return False
    return True


def _sequence(case, requested):
    sequences = [case["expected_calls"], *case.get("allowed_call_sequences", [])]
    names = [call.get("tool_name") for call in requested]
    matches = [sequence for sequence in sequences if [call["name"] for call in sequence] == names]
    selected = max(matches or sequences[:1], key=lambda seq: sum(
        _arguments_match(expected, actual) for expected, actual in zip(seq, requested)))
    return selected, bool(matches)


def _leaves(prefix, expected, actual, evidence):
    if isinstance(expected, dict):
        for key, value in expected.items():
            yield from _leaves(f"{prefix}.{key}", value, actual.get(key, _MISSING) if isinstance(actual, dict) else _MISSING,
                               f"{evidence}.{key}")
    else:
        yield _check(prefix, expected, actual, evidence)


def _source_checks(case, calls):
    searches = [(i, call["result"]) for i, call in enumerate(calls)
                if call.get("tool_name") == "search_stations" and call.get("ok") is True
                and isinstance(call.get("result", {}).get("stations"), list)]
    boards = [(i, call["result"]["board"]) for i, call in enumerate(calls)
              if call.get("tool_name") == "get_departures_with_disruptions" and call.get("ok") is True
              and isinstance(call.get("result", {}).get("board"), dict)]
    search_index, search = searches[-1] if searches else (None, {})
    board_index, board = boards[-1] if boards else (None, {})
    entries = board.get("departures", [])
    search_path = f"execution_trace.tool_calls[{search_index}].result"
    board_path = f"execution_trace.tool_calls[{board_index}].result.board"
    notices = {}
    for i, entry in enumerate(entries):
        for j, notice in enumerate(entry.get("associated_disruptions", [])):
            notice_id = notice.get("disruption_id")
            if notice_id not in notices:
                notices[notice_id] = ({**notice, "associated_run_refs": []},
                                      f"{board_path}.departures[{i}].associated_disruptions[{j}]")
            notices[notice_id][0]["associated_run_refs"].append(entry["departure"].get("run_ref"))
    observed = {
        "station_match_count": len(search["stations"]) if searches else _MISSING,
        "selected_stop_id": board.get("stop_id", _MISSING) if boards else None if search.get("stations") == [] else _MISSING,
        "departure_count": len(entries) if boards else _MISSING,
        "departure_data_retrieved": bool(boards) if searches or boards else _MISSING,
        "associated_notice_ids": sorted(notices) if boards else _MISSING,
        "contextual_notice_ids": sorted({notice["disruption_id"] for notice in board.get("contextual_notices", [])}) if boards else _MISSING,
        "route_notice_ids": sorted({notice["disruption_id"] for values in board.get("route_notices", {}).values() for notice in values}) if boards else _MISSING,
        "unresolved_disruption_ids": sorted({item for entry in entries for item in entry.get("unresolved_disruption_ids", [])}) if boards else _MISSING,
    }
    checks = []
    for key, expected in case["expected_facts"].items():
        if key == "outcome":  # Natural-language outcome remains a review criterion.
            continue
        if key == "departures":
            for record in expected:
                matches = [(i, entry["departure"]) for i, entry in enumerate(entries)
                           if entry.get("departure", {}).get("run_ref") == record["run_ref"]]
                index, actual = matches[0] if len(matches) == 1 else (None, {})
                checks.extend(_leaves(f"departure.{record['run_ref']}", record, actual,
                                      f"{board_path}.departures[{index}].departure"))
        elif key == "notices":
            for record in expected:
                actual, path = notices.get(record["disruption_id"], ({}, board_path))
                fields = {name: value for name, value in record.items()
                          if name not in {"described_scope", "immediate_passenger_impact", "associated_run_refs"}}
                checks.extend(_leaves(f"notice.{record['disruption_id']}", fields, actual, path))
                if "associated_run_refs" in record:
                    checks.append(_check(f"notice.{record['disruption_id']}.associated_run_refs", record["associated_run_refs"],
                                         actual.get("associated_run_refs", _MISSING),
                                         f"{board_path}.departures[*].departure.run_ref and associated_disruptions"))
        else:
            if key not in observed:
                raise ValueError("Unsupported golden source-fact field.")
            checks.append(_check(key, expected, observed[key], search_path if key == "station_match_count" else board_path))
    return checks


def score_case(case, result, *, required_fixture_indices, run_id):
    """Check a saved case without assigning any manual answer-quality score."""
    result = result if isinstance(result, dict) else {}
    requested = result.get("requested_tool_calls", [])
    trace = result.get("execution_trace", {})
    calls = trace.get("tool_calls", [])
    selected, sequence_matches = _sequence(case, requested)
    selection = [_check("allowed_tool_sequence", True, sequence_matches, "requested_tool_calls")]
    selection[0].update(allowed_sequences=[[call["name"] for call in sequence] for sequence in
                                          [case["expected_calls"], *case.get("allowed_call_sequences", [])]],
                        actual_sequence=[call.get("tool_name") for call in requested])
    arguments = []
    for i, expected in enumerate(selected):
        actual = requested[i] if i < len(requested) else {}
        check = _check(f"arguments.{i + 1}", True,
                       actual.get("tool_name") == expected["name"] and _arguments_match(expected, actual),
                       f"requested_tool_calls[{i}].arguments")
        check.update(expected_arguments=expected["arguments"], actual_arguments=actual.get("arguments"),
                     query_variants=expected.get("query_variants", []), call_id=actual.get("call_id"))
        arguments.append(check)
    remaining = Counter(call["name"] for call in selected)
    unexpected = []
    for i, call in enumerate(requested):
        name = call.get("tool_name")
        if remaining[name] and name not in case.get("prohibited_calls", []):
            remaining[name] -= 1
        else:
            unexpected.append(i)
    request_ids = [call.get("call_id") for call in requested]
    extra_trace = [i for i, call in enumerate(calls) if call.get("call_id") not in request_ids]
    coverage = result.get("fixture_coverage") or {}
    counts = coverage.get("match_counts", [])
    unused = {entry["response_index"] for entry in coverage.get("unused_requests", [])}
    fixtures_used = bool(coverage) and all(
        index <= len(counts) and type(counts[index - 1]) is int and counts[index - 1] > 0 and index not in unused
        for index in required_fixture_indices)
    execution = [
        _check("case_identity", [case["case_id"], case["question"], run_id],
               [result.get("case_id"), result.get("question"), result.get("run_id")], "case_id,question,run_id"),
        _check("execution_completed", True,
               result.get("status") == trace.get("status") == "completed" and result.get("error") is None
               and isinstance(result.get("final_answer"), str) and bool(result["final_answer"].strip()), "status,error,execution_trace.status,final_answer"),
        _check("no_unexpected_tool_calls", [], unexpected + [f"trace:{i}" for i in extra_trace], "requested_tool_calls,execution_trace.tool_calls"),
        _check("no_fixture_misses", 0, coverage.get("miss_count", _MISSING), "fixture_coverage.miss_count"),
        _check("required_fixtures_used", True, fixtures_used, "fixture_coverage.match_counts,unused_requests"),
    ]
    execution[-1]["required_fixture_indices"] = required_fixture_indices
    for i, expected in enumerate(selected):
        request = requested[i] if i < len(requested) else {}
        call_id = request.get("call_id")
        matches = [call for call in calls if isinstance(call_id, str) and call_id and call.get("call_id") == call_id]
        success = (request.get("tool_name") == expected["name"] and request_ids.count(call_id) == 1
                   and len(matches) == 1 and matches[0].get("tool_name") == expected["name"]
                   and isinstance(matches[0].get("arguments"), dict) and matches[0].get("ok") is True
                   and matches[0].get("result", {}).get("ok") is True)
        check_id = f"successful_tool.{i + 1}"
        if "expected_error_code" in expected:
            # Opt-in failure scenarios; unannotated historical calls still require success.
            code = expected["expected_error_code"]
            success = (isinstance(code, str) and bool(code)
                       and request.get("tool_name") == expected["name"] and request_ids.count(call_id) == 1
                       and len(matches) == 1 and matches[0].get("tool_name") == expected["name"]
                       and _arguments_match(expected, matches[0]) and matches[0].get("ok") is False
                       and matches[0].get("result", {}).get("ok") is False
                       and matches[0].get("result", {}).get("error", {}).get("code") == code
                       and "board" not in matches[0].get("result", {}))
            check_id = f"expected_tool_error.{i + 1}"
        check = _check(check_id, True, success, "execution_trace.tool_calls")
        check["call_id"] = call_id
        execution.append(check)
    answer = result.get("final_answer") or ""
    normalized = " ".join(answer.translate(str.maketrans("", "", "*_`")).split())
    wording = [_check(f"verbatim_timing_description.{record['run_ref']}", True,
                      record["delay_description"] in normalized, "final_answer")
               for record in case["expected_facts"].get("departures", []) if "delay_description" in record]
    metrics = {"tool_selection": _metric(selection), "arguments": _metric(arguments),
               "execution": _metric(execution), "source_facts": _metric(_source_checks(case, calls)),
               "timing_wording": _metric(wording)}
    return {
        "case_id": case["case_id"], "question": case["question"], "metrics": metrics,
        "execution_completed": result.get("status") == "completed",
        "automated_checks_passed": all(metric["status"] != "fail" for metric in metrics.values()),
        "manual_review": {**{name: None for name in MANUAL_FIELDS}, "reviewer_notes": ""},
        "review_guidance": {"expected_calls": case["expected_calls"],
                            "required_response_notes": case.get("required_response_notes", []),
                            "prohibited_claims": case.get("prohibited_claims", []),
                            "expected_facts": case["expected_facts"]},
        "final_answer": result.get("final_answer"),
    }


def aggregate_scores(scores):
    aggregate = {"cases": len(scores), "execution_completed": sum(score["execution_completed"] for score in scores),
                 "automated_checks_passed": sum(score["automated_checks_passed"] for score in scores),
                 "metrics": {}, "manual_review": {}}
    for name in METRICS:
        metrics = [score["metrics"][name] for score in scores]
        eligible = [metric for metric in metrics if metric["status"] != "not_applicable"]
        aggregate["metrics"][name] = {
            "denominator": len(eligible), "passed": sum(metric["status"] == "pass" for metric in eligible),
            "failed": sum(metric["status"] == "fail" for metric in eligible), "not_applicable": len(metrics) - len(eligible),
            "checks_passed": sum(metric["passed"] for metric in metrics), "checks_total": sum(metric["total"] for metric in metrics),
            "failed_case_ids": [score["case_id"] for score in scores if score["metrics"][name]["status"] == "fail"],
        }
    for name in MANUAL_FIELDS:
        values = [score["manual_review"][name] for score in scores if score["manual_review"][name] is not None]
        aggregate["manual_review"][name] = {"denominator": len(scores), "reviewed": len(values),
                                            "coverage": len(values) / len(scores) if scores else None,
                                            "mean": sum(values) / len(values) if values else None}
    aggregate["manual_review"]["fully_reviewed_cases"] = sum(
        all(score["manual_review"][name] is not None for name in MANUAL_FIELDS) for score in scores)
    return aggregate


def _load_reviews(path, run_id, case_ids):
    reviews = {}
    if path.exists():
        try:
            with path.open(encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    if row["run_id"] != run_id or row["case_id"] not in case_ids or row["case_id"] in reviews:
                        raise ValueError
                    values = {name: None if row[f"manual_{name}"] == "" else int(row[f"manual_{name}"]) for name in MANUAL_FIELDS}
                    if any(value is not None and value not in (0, 1, 2) for value in values.values()):
                        raise ValueError
                    if not isinstance(row["reviewer_notes"], str):
                        raise ValueError
                    reviews[row["case_id"]] = {**values, "reviewer_notes": row["reviewer_notes"]}
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("Invalid manual review CSV; scores must be blank or 0, 1, 2, for this run's cases.") from None
    return reviews


def score_run(run_dir: Path, *, dataset_path: Path = DATASET_PATH, output_dir: Path | None = None):
    """Score saved batch results only; original results and production code are untouched."""
    run_dir, dataset_path = Path(run_dir).resolve(), Path(dataset_path).resolve()
    dataset = _load_json(dataset_path)
    manifest = _load_json(run_dir / "summary.json")
    cases = {case["case_id"]: case for case in dataset["cases"]}
    entries = manifest["cases"]
    ids = [entry["case_id"] for entry in entries]
    if not ids or len(set(ids)) != len(ids) or any(case_id not in cases for case_id in ids):
        raise ValueError("Invalid or unknown case IDs in the saved evaluation summary.")
    output_dir = Path(output_dir) if output_dir is not None else run_dir / "scores"
    reviews = _load_reviews(output_dir / "cases.csv", manifest["run_id"], ids)
    scores = []
    for entry in entries:
        case = cases[entry["case_id"]]
        sequences = [case["expected_calls"], *case.get("allowed_call_sequences", [])]
        if not all(isinstance(seq, list) and all(isinstance(call.get("arguments"), dict) for call in seq) for seq in sequences):
            raise ValueError("Invalid golden tool-call sequence.")
        fixture_path = (dataset_path.parent / case["fixture"]).resolve()
        if not fixture_path.is_relative_to(dataset_path.parent) or fixture_path.suffix != ".json":
            raise ValueError("Fixture path is outside the dataset directory.")
        fixture = _load_json(fixture_path)
        required = case.get("required_fixture_indices", list(range(1, len(fixture["responses"]) + 1)))
        if any(type(index) is not int or not 1 <= index <= len(fixture["responses"]) for index in required):
            raise ValueError("Invalid required fixture indices.")
        source_file = None
        result = None
        if entry.get("result_file") is not None:
            source_file = (run_dir / entry["result_file"]).resolve()
            if not source_file.is_relative_to(run_dir) or source_file.suffix != ".json":
                raise ValueError("Result path is outside the run directory or is not JSON.")
            try:
                result = _load_json(source_file)
            except ValueError:
                pass
        score = score_case(case, result, required_fixture_indices=required, run_id=manifest["run_id"])
        score["source_result_file"] = source_file.relative_to(run_dir).as_posix() if source_file else None
        score["source_fixture_file"] = case["fixture"]
        score["source_result_sha256"] = sha256(source_file.read_bytes()).hexdigest() if source_file and source_file.is_file() else None
        score["manual_review"].update(reviews.get(case["case_id"], {}))
        score["review_status"] = ("review_complete" if all(score["manual_review"][name] is not None for name in MANUAL_FIELDS)
                                  else "manual_review_pending")
        scores.append(score)
    aggregate = aggregate_scores(scores)
    aggregate.update(run_id=manifest["run_id"], checks_version=1, dataset_version=dataset.get("dataset_version"),
                     dataset_sha256=sha256(dataset_path.read_bytes()).hexdigest(),
                     scope="Mechanical checks only; answer accuracy, grounding and relevance require manual review.")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "checks.json").write_text(json.dumps({"run_id": manifest["run_id"], "cases": scores}, indent=2) + "\n", encoding="utf-8")
    (output_dir / "aggregate.json").write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")
    columns = ["run_id", "case_id", "question", "source_result_file", "execution_completed", "automated_checks_passed", "review_status"]
    columns += [f"{name}_{suffix}" for name in METRICS for suffix in ("status", "passed", "total")]
    columns += ["failed_check_ids", "final_answer", *(f"manual_{name}" for name in MANUAL_FIELDS), "reviewer_notes"]
    csv_path = output_dir / "cases.csv"
    csv_temp = output_dir / "cases.csv.tmp"
    with csv_temp.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for score in scores:
            row = {key: score[key] for key in ("case_id", "question", "source_result_file", "execution_completed", "automated_checks_passed", "final_answer", "review_status")}
            row.update(run_id=manifest["run_id"], reviewer_notes=score["manual_review"]["reviewer_notes"],
                       failed_check_ids=";".join(check["id"] for metric in score["metrics"].values() for check in metric["checks"] if not check["passed"]))
            row.update({f"{name}_{suffix}": score["metrics"][name][suffix] for name in METRICS for suffix in ("status", "passed", "total")})
            row.update({f"manual_{name}": score["manual_review"][name] for name in MANUAL_FIELDS})
            writer.writerow(row)
    csv_temp.replace(csv_path)
    return aggregate


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score saved Hopwise evaluations offline; manual labels remain unfilled.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    args = parser.parse_args(argv)
    try:
        aggregate = score_run(args.run_dir, dataset_path=args.dataset)
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("Unable to score this run. Check dataset, results and any existing manual review labels.") from None
    print("Saved offline checks: " + str(args.run_dir / "scores"))
    for name, metric in aggregate["metrics"].items():
        print(f"{name}: {metric['passed']}/{metric['denominator']} cases; {metric['failed']} failed; {metric['not_applicable']} not applicable")
    print(f"Fully manually reviewed: {aggregate['manual_review']['fully_reviewed_cases']}/{aggregate['cases']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
