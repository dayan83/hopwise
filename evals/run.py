"""Run selected agent evaluations: opt-in LLM calls, fixture-only PTV data."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from time import perf_counter
from uuid import uuid4

from dotenv import load_dotenv
from openai import APIError, OpenAI

from agent import AgentError, INSTRUCTIONS, MAX_TOOL_ROUNDS, answer_question
from agent_tools import TOOL_DEFINITIONS
from agent_trace import AgentTrace, redact_text
from evals.fixture_transport import FixtureMiss, FixtureTransport


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = PROJECT_ROOT / "evals" / "dataset.json"
RESULTS_DIR = PROJECT_ROOT / "evals" / "results"
CASE_ID = "richmond_30_seconds_late"


def _safe_value(value, secrets):
    if isinstance(value, str):
        return redact_text(value, secrets=secrets)
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return "[unsupported value omitted]"


class RecordingClient:
    """Observe selected response metadata without changing the SDK response."""

    def __init__(self, client):
        self._client = client
        self.responses = self
        self.requested_tool_calls = []
        self.model_responses = []

    @property
    def api_key(self):
        return getattr(self._client, "api_key", None)

    def _text(self, value):
        return redact_text(value, secrets=(self.api_key,)) if isinstance(value, str) else None

    def create(self, **kwargs):
        request_index = len(self.model_responses) + 1
        record = {"request_index": request_index, "response_id": None, "model": None,
                  "status": "request_failed", "token_usage": None}
        self.model_responses.append(record)
        response = self._client.responses.create(**kwargs)
        record.update(response_id=self._text(getattr(response, "id", None)),
                      model=self._text(getattr(response, "model", None)),
                      status=self._text(getattr(response, "status", None)))
        usage = getattr(response, "usage", None)
        if usage is not None:
            record["token_usage"] = {
                name: count if type(count := getattr(usage, name, None)) is int and count >= 0 else None
                for name in ("input_tokens", "output_tokens", "total_tokens")}
        for item in response.output:
            if item.type != "function_call":
                continue
            arguments = None
            parse_status = "invalid_json"
            omitted_fields = False
            try:
                parsed = json.loads(item.arguments)
            except (TypeError, ValueError):
                pass
            else:
                parse_status = "parsed" if isinstance(parsed, dict) else "not_an_object"
                if isinstance(parsed, dict):
                    # Invalid/unexpected fields are not copied into evaluation artifacts.
                    omitted_fields = bool(set(parsed) - {"query", "stop_id", "limit"})
                    arguments = {key: _safe_value(value, (self.api_key,))
                                 for key, value in parsed.items() if key in {"query", "stop_id", "limit"}}
            self.requested_tool_calls.append({
                "sequence": len(self.requested_tool_calls) + 1,
                "request_index": request_index,
                "tool_name": self._text(item.name),
                "call_id": self._text(item.call_id),
                "arguments": arguments,
                "arguments_parse_status": parse_status,
                "omitted_argument_fields": omitted_fields,
            })
        return response


def _load_dataset(dataset_path):
    try:
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
        ids = [case["case_id"] for case in dataset["cases"]]
        if (not ids or any(not isinstance(case_id, str)
                           or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", case_id) for case_id in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("invalid_evaluation_dataset: check the JSON and unique case identifiers.") from None
    return dataset


def _load_case(case_id, dataset_path):
    try:
        dataset = _load_dataset(dataset_path)
        matches = [case for case in dataset["cases"] if case["case_id"] == case_id]
        if len(matches) != 1:
            raise ValueError
        case = matches[0]
        if not isinstance(case["question"], str) or not case["question"].strip():
            raise ValueError
        fixture_path = (dataset_path.parent / case["fixture"]).resolve()
        if not fixture_path.is_relative_to(dataset_path.parent.resolve()) or fixture_path.suffix != ".json":
            raise ValueError
        fixture = FixtureTransport.from_file(fixture_path)
        if not isinstance(case["as_of_utc"], str):
            raise ValueError
        reference = datetime.fromisoformat(case["as_of_utc"].replace("Z", "+00:00"))
        if reference.tzinfo is None or fixture.scenario_id != case_id or fixture.as_of_utc != reference:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("invalid_evaluation_case: check the case ID, dataset and fixture reference.") from None
    return dataset, case, fixture


def _new_run_id(started):
    return started.strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:12]


def _configuration(model, secrets):
    return {
        "model_requested": redact_text(model.strip(), secrets=secrets),
        "instructions_sha256": sha256(INSTRUCTIONS.encode()).hexdigest(),
        "tools_sha256": sha256(json.dumps(TOOL_DEFINITIONS, sort_keys=True).encode()).hexdigest(),
        "max_tool_rounds": MAX_TOOL_ROUNDS,
    }


def _write_json(path, value):
    try:
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        raise ValueError("Unable to save evaluation JSON.") from None


class _ResultSaveError(ValueError):
    def __init__(self, result):
        super().__init__("Unable to save the evaluation result JSON.")
        self.result = result


def run_case(case_id: str, *, client, model: str, dataset_path: Path = DATASET_PATH,
             results_dir: Path | None = None, evaluation_run_id: str | None = None) -> dict:
    """Execute and save one case using an explicitly supplied client.

    Tests inject a scripted client. Only main() constructs a real OpenAI client.
    Completion reports execution, not a quality score or a golden-answer match.
    """
    dataset_path = Path(dataset_path)
    dataset, case, fixture = _load_case(case_id, dataset_path)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Set OPENAI_MODEL or pass --model.")
    started = datetime.now(timezone.utc)
    if evaluation_run_id is not None and not re.fullmatch(r"\d{8}T\d{6}Z_[a-f0-9]{12}", evaluation_run_id):
        raise ValueError("Invalid evaluation run ID.")
    run_id = evaluation_run_id or _new_run_id(started)
    result_dir = Path(results_dir if results_dir is not None else RESULTS_DIR) / run_id
    if evaluation_run_id is not None:
        result_dir = result_dir / "cases" / case_id
    try:
        result_dir.mkdir(parents=True, exist_ok=False)
    except OSError:
        raise ValueError("Unable to create the evaluation result directory.") from None
    recording = RecordingClient(client)
    trace = AgentTrace()
    answer = None
    error = None
    timer = perf_counter()
    try:
        with fixture.reference_time():
            answer = answer_question(case["question"], client=recording, model=model,
                                     ptv_config=fixture.config, ptv_transport=fixture, trace=trace)
    except FixtureMiss:
        trace.fail("fixture_miss")
        error = {"code": "fixture_miss", "message": "A PTV request did not match the selected fixture."}
    except AgentError:
        error = {"code": trace.error_code or "agent_error", "message": "The agent could not complete the evaluation."}
        trace.fail(error["code"])
    except Exception:
        # Persist partial diagnostics without SDK bodies, exception text or tracebacks.
        trace.fail("execution_error")
        error = {"code": "execution_error", "message": "An unexpected execution error interrupted the evaluation."}
    duration = perf_counter() - timer
    secrets = (recording.api_key, fixture.config.dev_id, fixture.config.api_key, fixture.config.base_url)
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "case_id": redact_text(case_id, secrets=secrets),
        "question": redact_text(case["question"], secrets=secrets),
        "status": "failed" if error is not None else "completed",
        "started_at_utc": started.isoformat(),
        "duration_seconds": round(duration, 6),
        "dataset_version": _safe_value(dataset.get("dataset_version"), secrets),
        "scenario": {"fixture": redact_text(case["fixture"], secrets=secrets), "as_of_utc": fixture.as_of_utc.isoformat()},
        "configuration": _configuration(model, secrets),
        "requested_tool_calls": recording.requested_tool_calls,
        "execution_trace": trace.as_dict(),
        "final_answer": redact_text(answer, secrets=secrets) if answer is not None else None,
        "model_responses": recording.model_responses,
        "fixture_coverage": {
            "match_counts": list(fixture.match_counts),
            "unused_requests": fixture.unused_requests,
            "miss_count": fixture.miss_count,
        },
        "error": error,
    }
    try:
        _write_json(result_dir / "result.json", result)
    except ValueError:
        raise _ResultSaveError(result) from None
    return result


def run_all(*, client, model: str, dataset_path: Path = DATASET_PATH,
            results_dir: Path | None = None) -> dict:
    """Run dataset cases sequentially, continuing after individual failures."""
    dataset_path = Path(dataset_path)
    dataset = _load_dataset(dataset_path)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Set OPENAI_MODEL or pass --model.")
    started = datetime.now(timezone.utc)
    run_id = _new_run_id(started)
    results_root = Path(results_dir if results_dir is not None else RESULTS_DIR)
    run_dir = results_root / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except OSError:
        raise ValueError("Unable to create the evaluation result directory.") from None
    secrets = (getattr(client, "api_key", None),)
    summary = {
        "schema_version": 1, "run_id": run_id, "started_at_utc": started.isoformat(),
        "dataset_version": _safe_value(dataset.get("dataset_version"), secrets),
        "configuration": _configuration(model, secrets),
        "cases_attempted": 0, "cases_completed": 0, "cases_with_errors": 0,
        "fixture_misses": 0, "duration_seconds": 0,
        "total_token_usage": {}, "token_usage_reporting": {}, "cases": [],
    }
    responses = []
    timer = perf_counter()
    for case in dataset["cases"]:
        case_started = datetime.now(timezone.utc)
        case_timer = perf_counter()
        path = run_dir / "cases" / case["case_id"] / "result.json"
        needs_save = False
        try:
            result = run_case(case["case_id"], client=client, model=model, dataset_path=dataset_path,
                              results_dir=results_root, evaluation_run_id=run_id)
        except _ResultSaveError as exc:
            # Retain already captured usage and traces if persistence failed.
            result = exc.result
            result["status"] = "failed"
            result["error"] = {"code": "result_save_error", "message": "The case result could not be saved."}
            needs_save = True
        except Exception:
            # Preparation failures are also case-local. Do not copy diagnostics.
            result = {
                "schema_version": 1, "run_id": run_id,
                "case_id": redact_text(case["case_id"], secrets=secrets),
                "question": _safe_value(case.get("question"), secrets), "status": "failed",
                "started_at_utc": case_started.isoformat(),
                "duration_seconds": round(perf_counter() - case_timer, 6),
                "dataset_version": summary["dataset_version"], "scenario": None,
                "configuration": summary["configuration"], "requested_tool_calls": [],
                "execution_trace": {"status": "failed", "error_code": "case_setup_error", "tool_calls": []},
                "final_answer": None, "model_responses": [], "fixture_coverage": None,
                "error": {"code": "case_setup_error", "message": "The case could not be prepared."},
            }
            needs_save = True
        if needs_save:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_json(path, result)
            except (OSError, ValueError):
                path = None
        responses.extend(result["model_responses"])
        summary["cases_attempted"] += 1
        summary["cases_completed"] += result["status"] == "completed"
        summary["cases_with_errors"] += result["error"] is not None
        if result["fixture_coverage"] is not None:
            summary["fixture_misses"] += result["fixture_coverage"]["miss_count"]
        summary["cases"].append({
            "case_id": result["case_id"], "status": result["status"],
            "result_file": path.relative_to(run_dir).as_posix() if path is not None else None,
            "duration_seconds": result["duration_seconds"],
            "error_code": result["error"]["code"] if result["error"] else None,
        })
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        values = [response["token_usage"][name] for response in responses
                  if response["token_usage"] is not None and response["token_usage"][name] is not None]
        summary["total_token_usage"][name] = sum(values) if values else None
    complete = sum(response["token_usage"] is not None
                   and all(value is not None for value in response["token_usage"].values()) for response in responses)
    summary["token_usage_reporting"] = {
        "model_requests": len(responses), "responses_with_complete_usage": complete,
        "responses_missing_usage": len(responses) - complete,
        "cases_without_retained_metadata": sum(case["error_code"] == "case_setup_error" for case in summary["cases"]),
    }
    summary["duration_seconds"] = round(perf_counter() - timer, 6)
    _write_json(run_dir / "summary.json", summary)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run Hopwise evaluations with a real LLM and fixture-only PTV data.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--case", help="One dataset case; defaults to richmond_30_seconds_late.")
    selection.add_argument("--all", dest="all_cases", action="store_true", help="Run all dataset cases sequentially.")
    parser.add_argument("--model", help="Responses model; defaults to OPENAI_MODEL.")
    args = parser.parse_args(argv)
    try:
        dataset = _load_dataset(DATASET_PATH)
    except ValueError:
        raise SystemExit("Unable to load the evaluation dataset.") from None
    if args.case is not None and args.case not in {case["case_id"] for case in dataset["cases"]}:
        parser.error("Unknown evaluation case ID.")
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    model = (args.model if args.model is not None else os.environ.get("OPENAI_MODEL", "")).strip()
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise SystemExit("Missing required environment variable: OPENAI_API_KEY")
    if not model:
        raise SystemExit("Set OPENAI_MODEL or pass --model.")
    try:
        with OpenAI(timeout=30.0, max_retries=0) as client:
            if args.all_cases:
                summary = run_all(client=client, model=model)
            else:
                result = run_case(args.case or CASE_ID, client=client, model=model)
    except APIError:
        raise SystemExit("Unable to initialize the OpenAI evaluation client.") from None
    except ValueError:
        raise SystemExit("Unable to start or save the evaluation. Check configuration, dataset and output directory.") from None
    if args.all_cases:
        print("Saved evaluation summary: " + str(RESULTS_DIR / summary["run_id"] / "summary.json"))
        print(f"Attempted {summary['cases_attempted']}; completed {summary['cases_completed']}; errors {summary['cases_with_errors']}.")
        return 1 if summary["cases_with_errors"] else 0
    print("Saved evaluation result: " + str(RESULTS_DIR / result["run_id"] / "result.json"))
    if result["error"] is not None:
        print("Execution error: " + result["error"]["code"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
