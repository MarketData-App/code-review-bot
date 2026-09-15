"""Tests for the eval scorer.

The scorer is ordinary code and is tested like ordinary code. The evals
themselves are run by hand, because they spend real tokens.

Run: pytest tests/test_evals.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals import score  # noqa: E402


def known(file="sdk/client.py", line=42, category="correctness", label="retry never sleeps"):
    return {"file": file, "line": line, "category": category, "label": label}


def found(
    file="sdk/client.py", line_start=42, category="correctness", title="Retry loop never sleeps"
):
    return {
        "file": file,
        "line_start": line_start,
        "line_end": None,
        "category": category,
        "severity": "blocking",
        "confidence": 0.9,
        "title": title,
        "body": "b",
    }


def test_the_same_place_and_category_matches():
    assert score.matches(known(), found())


def test_a_nearby_line_still_matches():
    assert score.matches(known(line=42), found(line_start=47))


def test_a_far_line_does_not_match():
    assert not score.matches(known(line=42), found(line_start=300))


def test_a_different_file_does_not_match():
    assert not score.matches(known(file="a.py"), found(file="b.py"))


def test_a_different_category_does_not_match():
    assert not score.matches(known(category="security"), found(category="style"))


def test_scoring_counts_matched_missed_and_extra():
    case = {"known_findings": [known(label="one"), known(line=200, label="two")]}
    report = score.score_case(case, [found(line_start=42), found(line_start=900)])
    assert report["matched"] == ["one"]
    assert report["missed"] == ["two"]
    assert report["extra"] == 1
    assert report["recall"] == 0.5
    assert report["precision"] == 0.5


def test_a_case_with_no_known_findings_scores_recall_one():
    report = score.score_case({"known_findings": []}, [])
    assert report["recall"] == 1.0
    assert report["precision"] == 1.0


def test_one_found_finding_never_matches_two_known_ones():
    case = {"known_findings": [known(line=42, label="one"), known(line=43, label="two")]}
    report = score.score_case(case, [found(line_start=42)])
    assert len(report["matched"]) == 1
