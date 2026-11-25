"""CSV manual review of saved multi-turn answers; never invokes an agent or API."""

import csv
from hashlib import sha256
import json
from pathlib import Path

from evals.checks import MANUAL_FIELDS, _load_reviews


def write_review_template(root, results):
    """New runs start unreviewed; labels are separate from immutable execution data."""
    columns = ["run_id", "case_id", "question", "source_result_file", "final_answer",
               "orchestration_passed", "answer_checks_passed", "review_guidance",
               *(f"manual_{name}" for name in MANUAL_FIELDS), "reviewer_notes"]
    with (root / "reviews.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for result in results:
            for turn, scored in zip(result["turns"], result["checks"]["turns"], strict=True):
                writer.writerow({"run_id": result["run_id"], "case_id": turn["case_id"],
                                 "question": turn["question"], "final_answer": turn["final_answer"],
                                 "source_result_file": f"scenarios/{result['scenario_id']}/result.json",
                                 "orchestration_passed": scored["orchestration_passed"],
                                 "answer_checks_passed": scored["answer_checks"]["passed"] if scored["answer_checks"] else None,
                                 "review_guidance": json.dumps(scored["mechanical"]["review_guidance"]),
                                 "reviewer_notes": ""})


def summarize_reviews(run_dir):
    """Validate this run's existing 0/1/2 labels and save coverage, without rescoring."""
    root = Path(run_dir).resolve()
    manifest = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    ids = []
    for scenario in manifest["scenarios"]:
        path = (root / scenario["result_file"]).resolve()
        if not path.is_relative_to(root) or path.suffix != ".json":
            raise ValueError("Invalid multi-turn result path")
        result = json.loads(path.read_text(encoding="utf-8"))
        ids.extend(turn["case_id"] for turn in result["turns"])
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Invalid turn IDs")
    csv_path = root / "reviews.csv"
    if not csv_path.is_file():
        raise ValueError("Missing review CSV")
    labels = _load_reviews(csv_path, manifest["run_id"], ids)
    dimensions = {}
    for name in MANUAL_FIELDS:
        values = [row[name] for row in labels.values() if row[name] is not None]
        dimensions[name] = {"denominator": len(ids), "reviewed": len(values),
                            "coverage": len(values) / len(ids),
                            "mean": sum(values) / len(values) if values else None}
    summary = {"run_id": manifest["run_id"], "turns": len(ids), "manual_review": dimensions,
               "fully_reviewed_turns": sum(all(row[name] is not None for name in MANUAL_FIELDS) for row in labels.values()),
               "reviews_sha256": sha256(csv_path.read_bytes()).hexdigest(), "labels": labels}
    (root / "review_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary
