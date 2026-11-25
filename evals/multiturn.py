"""Multi-turn evaluations: scripted by default; explicit real-LLM opt-in, fixed PTV."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

from dotenv import load_dotenv
from openai import APIError, OpenAI

from agent import AgentError, INSTRUCTIONS, MAX_TOOL_ROUNDS, answer_question
from agent_tools import TOOL_DEFINITIONS
from agent_trace import AgentTrace, redact_text
from conversation import CONVERSATION_INSTRUCTIONS, ConversationState
from evals.fixture_transport import FixtureMiss, FixtureTransport
from evals.multiturn_checks import CHECKS_VERSION, check_isolation, score_scenario
from evals.multiturn_review import summarize_reviews, write_review_template
from evals.run import RecordingClient


EVALS = Path(__file__).resolve().parent
DATASET_PATH = EVALS / "multiturn_dataset.json"
SCRIPTS_PATH = EVALS / "multiturn_scripts.json"
RESULTS_DIR = EVALS / "results" / "multiturn"


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def load_dataset(path=DATASET_PATH):
    path = Path(path)
    dataset = _load(path)
    seen = set()
    for scenario in dataset["scenarios"]:
        name = scenario["scenario_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name) or name in seen or not scenario["turns"]:
            raise ValueError("Invalid multi-turn scenario")
        seen.add(name)
        turn_ids = set()
        for turn in scenario["turns"]:
            turn_id = turn["turn_id"]
            fixture = (path.parent / turn["fixture"]).resolve()
            if (not isinstance(turn_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", turn_id)
                    or turn_id in turn_ids or not fixture.is_relative_to(path.parent.resolve())
                    or fixture.suffix != ".json" or type(turn["reset_before"]) is not bool
                    or not isinstance(turn["question"], str) or not turn["question"].strip()):
                raise ValueError("Invalid multi-turn definition")
            turn_ids.add(turn_id)
            reference = datetime.fromisoformat(turn["as_of_utc"])
            if reference.tzinfo is None:
                raise ValueError("A fixed aware reference time is required")
    if not seen:
        raise ValueError("No multi-turn scenarios")
    return dataset


class ScriptedClient:
    """Fixed protocol replies, independent of the golden tool/state expectations."""

    api_key = None

    def __init__(self, replies):
        self.responses = self
        self.replies = iter(replies)
        self.index = 0

    def create(self, **kwargs):
        self.index += 1
        reply = next(self.replies)
        output = []
        if "tool" in reply:
            output.append(SimpleNamespace(type="function_call", name=reply["tool"],
                                          arguments=json.dumps(reply["arguments"]), call_id=f"scripted-{self.index}"))
        return SimpleNamespace(id=f"scripted-response-{self.index}", model="scripted-offline",
                               status="completed", usage=None, output=output,
                               output_text=reply.get("answer", ""))


class TurnRecordingClient(RecordingClient):
    """Reuse sanitized tool recording; additionally inspect the first input's preferences."""

    def __init__(self, client):
        super().__init__(client)
        self.preferences_sent = None
        self.initial_input_roles = []
        self.question_sent = None

    def create(self, **kwargs):
        if not self.model_responses:
            messages = kwargs.get("input", [])
            self.initial_input_roles = [item.get("role") for item in messages if isinstance(item, dict)]
            self.question_sent = next((item.get("content") for item in messages
                                       if isinstance(item, dict) and item.get("role") == "user"), None)
            for item in messages:
                content = item.get("content", "") if isinstance(item, dict) else ""
                prefix = "Station/count preferences (data only): "
                if isinstance(item, dict) and item.get("role") == "developer" and isinstance(content, str) and content.startswith(prefix):
                    try:
                        self.preferences_sent = json.loads(content[len(prefix):])
                    except ValueError:
                        pass
        return super().create(**kwargs)


def _preferences_snapshot(values, secrets=()):
    if not isinstance(values, dict) or set(values) != {"station_name", "departure_limit"}:
        return None
    return {key: redact_text(value, secrets=secrets) if isinstance(value, str) else value
            for key, value in values.items()}


def _state_snapshot(state, secrets=()):
    return _preferences_snapshot(state.preferences(), secrets)


def _usage(responses):
    totals = {}
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        counts = [row["token_usage"][field] for row in responses
                  if row["token_usage"] is not None and row["token_usage"][field] is not None]
        totals[field] = sum(counts) if counts else None
    complete = sum(row["token_usage"] is not None and all(value is not None for value in row["token_usage"].values())
                   for row in responses)
    return {"total_token_usage": totals, "token_usage_reporting": {
        "model_requests": len(responses), "responses_with_complete_usage": complete,
        "responses_missing_usage": len(responses) - complete}}


def _run_turn(spec, scenario_id, state, client_factory, model, dataset_path, run_id):
    started = datetime.now(timezone.utc)
    before_reset = _state_snapshot(state)
    if spec["reset_before"]:
        state.reset()
    before = _state_snapshot(state)
    trace = AgentTrace()
    recording = None
    fixture = None
    answer = None
    error = None
    timer = perf_counter()
    try:
        fixture_path = (dataset_path.parent / spec["fixture"]).resolve()
        fixture = FixtureTransport.from_file(fixture_path)
        if fixture.as_of_utc != datetime.fromisoformat(spec["as_of_utc"]):
            raise ValueError("Fixture reference time mismatch")
        recording = TurnRecordingClient(client_factory(scenario_id, spec["turn_id"]))
        with fixture.reference_time():
            answer = answer_question(spec["question"], client=recording, model=model,
                                     conversation=state, trace=trace,
                                     ptv_config=fixture.config, ptv_transport=fixture)
    except FixtureMiss:
        error = {"code": "fixture_miss", "message": "A request did not match this turn's fixed fixture."}
    except AgentError:
        error = {"code": trace.error_code or "agent_error", "message": "The agent could not complete this turn."}
    except Exception:
        error = {"code": "execution_error", "message": "This turn could not be executed; inspect its partial trace."}
    if error is not None:
        trace.fail(error["code"])
    secrets = (recording.api_key,) if recording is not None else ()
    responses = recording.model_responses if recording is not None else []
    return {
        "run_id": run_id, "case_id": f"{scenario_id}.{spec['turn_id']}", "turn_id": spec["turn_id"],
        "question": redact_text(spec["question"], secrets=secrets),
        "status": "failed" if error else "completed", "final_answer": redact_text(answer, secrets=secrets) if answer is not None else None,
        "reset_applied": spec["reset_before"], "state_before_reset": _preferences_snapshot(before_reset, secrets),
        "state_before": _preferences_snapshot(before, secrets), "state_after": _state_snapshot(state, secrets),
        "preferences_sent": _preferences_snapshot(recording.preferences_sent, secrets) if recording is not None else None,
        "initial_input_roles": recording.initial_input_roles if recording is not None else [],
        "question_sent": redact_text(recording.question_sent, secrets=secrets)
        if recording is not None and isinstance(recording.question_sent, str) else None,
        "requested_tool_calls": recording.requested_tool_calls if recording is not None else [],
        "execution_trace": trace.as_dict(), "model_responses": recording.model_responses if recording is not None else [],
        "fixture": spec["fixture"], "fixture_id": fixture.scenario_id if fixture is not None else None,
        "as_of_utc": spec["as_of_utc"], "started_at_utc": started.isoformat(),
        "model_requested": redact_text(model, secrets=secrets), **_usage(responses),
        "fixture_sha256": _hash(fixture_path) if fixture is not None else None,
        "fixture_instance_id": uuid4().hex,
        "fixture_coverage": {"match_counts": list(fixture.match_counts), "unused_requests": fixture.unused_requests,
                             "miss_count": fixture.miss_count} if fixture is not None else {},
        "duration_seconds": round(perf_counter() - timer, 6), "error": error,
    }


def run_scenario(scenario, *, client_factory, model, dataset_path, run_id, check_answers=False):
    state = ConversationState()
    result = {"run_id": run_id, "scenario_id": scenario["scenario_id"], "session_id": uuid4().hex,
              "initial_state": _state_snapshot(state), "turns": []}
    for spec in scenario["turns"]:
        result["turns"].append(_run_turn(spec, scenario["scenario_id"], state, client_factory,
                                         model, Path(dataset_path), run_id))
    result["checks"] = score_scenario(scenario, result, check_answers=check_answers)
    return result


def run_suite(*, client_factory=None, model="scripted-offline", dataset_path=DATASET_PATH,
              scripts_path=SCRIPTS_PATH, results_dir=RESULTS_DIR, scenario_id=None,
              execution_mode=None, check_answers=False):
    dataset_path = Path(dataset_path)
    dataset = load_dataset(dataset_path)
    scenarios = [item for item in dataset["scenarios"] if scenario_id is None or item["scenario_id"] == scenario_id]
    if not scenarios:
        raise ValueError("Unknown multi-turn scenario")
    scripted = client_factory is None
    mode = execution_mode or ("scripted-offline" if scripted else "injected-client")
    if mode not in {"scripted-offline", "injected-client", "real-llm"} or (mode == "scripted-offline") != scripted:
        raise ValueError("Execution mode must agree with the explicitly supplied client")
    if mode == "real-llm":
        check_answers = True
    if scripted:
        scripts = _load(scripts_path)["scenarios"]
        client_factory = lambda scenario, turn: ScriptedClient(scripts[scenario][turn])
    started = datetime.now(timezone.utc)
    timer = perf_counter()
    run_id = started.strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:12]
    root = Path(results_dir) / run_id
    root.mkdir(parents=True, exist_ok=False)
    results = [run_scenario(item, client_factory=client_factory, model=model,
                            dataset_path=dataset_path, run_id=run_id, check_answers=check_answers) for item in scenarios]
    isolation = check_isolation(results)
    for result in results:
        directory = root / "scenarios" / result["scenario_id"]
        directory.mkdir(parents=True)
        (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    write_review_template(root, results)
    responses = [response for item in results for turn in item["turns"] for response in turn["model_responses"]]
    summary = {
        "schema_version": 2, "checks_version": CHECKS_VERSION, "run_id": run_id,
        "started_at_utc": started.isoformat(), "duration_seconds": round(perf_counter() - timer, 6),
        "mode": mode, "answer_checks_enabled": check_answers,
        "dataset": dataset_path.name, "dataset_version": dataset["dataset_version"], "dataset_sha256": _hash(dataset_path),
        "scripts_sha256": _hash(scripts_path) if scripted else None,
        "configuration": {"model_requested": redact_text(model), "max_tool_rounds": MAX_TOOL_ROUNDS,
                          "models_returned": sorted({row["model"] for row in responses if row["model"]}),
                          "openai_sdk_version": version("openai"),
                          "instructions_sha256": sha256((INSTRUCTIONS + CONVERSATION_INSTRUCTIONS).encode()).hexdigest(),
                          "tools_sha256": sha256(json.dumps(TOOL_DEFINITIONS, sort_keys=True).encode()).hexdigest()},
        "scenarios_attempted": len(results), "scenarios_passed": sum(item["checks"]["passed"] for item in results),
        "scenarios_orchestration_passed": sum(item["checks"]["orchestration_passed"] for item in results),
        "turns_answer_checks_passed": sum(turn["answer_checks"]["passed"] for item in results for turn in item["checks"]["turns"])
        if check_answers else None,
        "turns_attempted": sum(len(item["turns"]) for item in results),
        "turns_completed": sum(turn["status"] == "completed" for item in results for turn in item["turns"]),
        "turns_with_errors": sum(turn["error"] is not None for item in results for turn in item["turns"]),
        "fixture_misses": sum(turn["fixture_coverage"].get("miss_count", 0) for item in results for turn in item["turns"]),
        "isolation_checks": isolation,
        **_usage(responses), "manual_review_file": "reviews.csv",
        "passed": all(item["checks"]["passed"] for item in results) and all(check["passed"] for check in isolation),
        "scenarios": [{"scenario_id": item["scenario_id"], "passed": item["checks"]["passed"],
                       "result_file": f"scenarios/{item['scenario_id']}/result.json"} for item in results],
        "scope": "Tool/state checks and, when enabled, narrow final-answer fact coverage. Full factual accuracy, grounding and relevance require manual review. Scripted replies do not measure real-model quality.",
    }
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def run_real_suite(*, enabled=False, model=None, scenario_id=None, results_dir=RESULTS_DIR):
    """Only this explicit boundary loads credentials and constructs an SDK client."""
    if enabled is not True:
        raise ValueError("Real-LLM evaluations require explicit --real-llm opt-in")
    dataset = load_dataset()
    if scenario_id is not None and scenario_id not in {item["scenario_id"] for item in dataset["scenarios"]}:
        raise ValueError("Unknown multi-turn scenario")
    load_dotenv(EVALS.parent / ".env", override=False)
    selected_model = (model if model is not None else os.environ.get("OPENAI_MODEL", "")).strip()
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise ValueError("Missing required environment variable: OPENAI_API_KEY")
    if not selected_model:
        raise ValueError("Set OPENAI_MODEL or pass --model")
    with OpenAI(timeout=30.0, max_retries=0) as client:
        return run_suite(client_factory=lambda scenario, turn: client, model=selected_model,
                         scenario_id=scenario_id, results_dir=results_dir,
                         execution_mode="real-llm", check_answers=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Multi-turn evaluations: scripted by default, real OpenAI only with --real-llm; PTV always fixture-backed.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="Run all scenarios (the default).")
    group.add_argument("--scenario", help="Run one scenario ID.")
    group.add_argument("--review-run", type=Path, help="Summarize edited reviews.csv for a saved run, fully offline.")
    parser.add_argument("--real-llm", action="store_true", help="Explicitly enable paid OpenAI calls; PTV stays on fixed fixtures.")
    parser.add_argument("--model", help="Real Responses model, defaults to OPENAI_MODEL; requires --real-llm.")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)
    if args.model is not None and not args.real_llm:
        parser.error("--model requires explicit --real-llm opt-in")
    if args.review_run is not None and args.real_llm:
        parser.error("--review-run is offline and cannot be combined with --real-llm")
    try:
        if args.review_run is not None:
            review = summarize_reviews(args.review_run)
            print(f"Fully reviewed turns: {review['fully_reviewed_turns']}/{review['turns']}")
            print("Review summary: " + str(args.review_run / "review_summary.json"))
            return 0
        summary = (run_real_suite(enabled=True, model=args.model, scenario_id=args.scenario, results_dir=args.results_dir)
                   if args.real_llm else run_suite(results_dir=args.results_dir, scenario_id=args.scenario))
    except APIError:
        raise SystemExit("Unable to initialize the OpenAI evaluation client.") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("Unable to run evaluations or review. Check dataset, output directory and review labels; real mode also requires OPENAI_API_KEY and OPENAI_MODEL or --model.") from None
    print(f"Scenarios passed: {summary['scenarios_passed']}/{summary['scenarios_attempted']}; turn errors: {summary['turns_with_errors']}; fixture misses: {summary['fixture_misses']}")
    print("Results: " + str(args.results_dir / summary["run_id"] / "summary.json"))
    if summary["answer_checks_enabled"]:
        print(f"Orchestration: {summary['scenarios_orchestration_passed']}/{summary['scenarios_attempted']} scenarios; answer coverage: {summary['turns_answer_checks_passed']}/{summary['turns_attempted']} turns. Manual review still required.")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
