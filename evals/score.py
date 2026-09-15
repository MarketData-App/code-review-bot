"""Scoring a review against a recorded pull request with known findings.

A case is one JSON file under evals/cases/. It holds the recorded PR facts
and the findings a human confirmed are real. The score is recall (did the bot
find them) and precision (how much noise came with them).

Run:
    uv run python evals/score.py evals/cases/*.json
"""

import argparse
import json
import sys
from pathlib import Path

# A finding counts as the same defect when it lands within this many lines.
LINE_SLACK = 10


def matches(known: dict, finding: dict) -> bool:
    """True when a reported finding is the known defect."""
    if (finding.get("file") or "") != known["file"]:
        return False
    if finding.get("category") != known["category"]:
        return False
    line = finding.get("line_start")
    if line is None:
        return known.get("line") is None
    return abs(int(line) - int(known["line"])) <= LINE_SLACK


def score_case(case: dict, findings: list[dict]) -> dict:
    """Recall and precision for one case. Each finding matches at most one defect."""
    known_list = case.get("known_findings", [])
    used = set()
    matched = []
    for item in known_list:
        for index, finding in enumerate(findings):
            if index in used:
                continue
            if matches(item, finding):
                used.add(index)
                matched.append(item["label"])
                break
    missed = [k["label"] for k in known_list if k["label"] not in matched]
    extra = len(findings) - len(used)
    recall = len(matched) / len(known_list) if known_list else 1.0
    precision = len(used) / len(findings) if findings else 1.0
    return {
        "matched": matched,
        "missed": missed,
        "extra": extra,
        "recall": recall,
        "precision": precision,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="evals/score.py")
    parser.add_argument("cases", nargs="+", help="case files to score")
    parser.add_argument(
        "--results", default=None, help="a JSON file of {case name: result} from a real run"
    )
    args = parser.parse_args(argv)

    results = json.loads(Path(args.results).read_text()) if args.results else {}
    total_recall, total_precision = 0.0, 0.0
    for path in args.cases:
        case = json.loads(Path(path).read_text())
        name = Path(path).stem
        findings = results.get(name, {}).get("findings", [])
        report = score_case(case, findings)
        total_recall += report["recall"]
        total_precision += report["precision"]
        print(f"{name}: recall {report['recall']:.2f} precision {report['precision']:.2f}")
        for label in report["missed"]:
            print(f"    missed: {label}")
    count = len(args.cases)
    print(f"mean recall {total_recall / count:.2f}, mean precision {total_precision / count:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
