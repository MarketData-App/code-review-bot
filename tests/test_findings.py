"""Tests for finding identity across runs.

A finding keeps its id while the file, the category and the title hold still,
so a re-review can say resolved / still open / new (spec section 8).

Run: pytest tests/test_findings.py
"""

from reviewbot import findings as f


def make(
    file="a.py",
    category="correctness",
    title="Retry loop never sleeps",
    severity="blocking",
    line_start=10,
):
    return {
        "file": file,
        "line_start": line_start,
        "line_end": None,
        "category": category,
        "severity": severity,
        "confidence": 0.8,
        "title": title,
        "body": "text",
    }


def test_id_is_eight_hex_characters():
    value = f.finding_id(make())
    assert len(value) == 8
    assert all(c in "0123456789abcdef" for c in value)


def test_id_is_stable_across_calls():
    assert f.finding_id(make()) == f.finding_id(make())


def test_id_ignores_the_line_number():
    # The same defect moves down the file when the author edits above it.
    assert f.finding_id(make(line_start=10)) == f.finding_id(make(line_start=90))


def test_id_ignores_the_body_and_severity():
    one = make()
    two = make(severity="nit")
    two["body"] = "rewritten wording"
    assert f.finding_id(one) == f.finding_id(two)


def test_id_changes_with_the_file():
    assert f.finding_id(make(file="a.py")) != f.finding_id(make(file="b.py"))


def test_id_changes_with_the_category():
    assert f.finding_id(make(category="security")) != f.finding_id(make(category="tests"))


def test_id_changes_with_the_title():
    assert f.finding_id(make(title="One")) != f.finding_id(make(title="Two"))


def test_id_is_case_and_space_insensitive_on_the_title():
    assert f.finding_id(make(title="Retry loop never sleeps")) == f.finding_id(
        make(title="  retry loop NEVER sleeps ")
    )


def test_unlocated_finding_gets_an_id():
    finding = make(file=None)
    finding["line_start"] = None
    assert len(f.finding_id(finding)) == 8


def test_with_ids_does_not_mutate_the_input():
    original = make()
    f.with_ids([original])
    assert "id" not in original


def test_with_ids_adds_the_id():
    assert f.with_ids([make()])[0]["id"] == f.finding_id(make())


def test_since_last_review_splits_resolved_open_and_new():
    old_one = f.with_ids([make(title="One")])[0]
    old_two = f.with_ids([make(title="Two")])[0]
    new_three = f.with_ids([make(title="Three")])[0]

    report = f.since_last_review([old_one["id"], old_two["id"]], [old_one, new_three])

    assert report["resolved"] == [old_two["id"]]
    assert [x["title"] for x in report["still_open"]] == ["One"]
    assert [x["title"] for x in report["new"]] == ["Three"]


def test_first_review_reports_everything_as_new():
    current = f.with_ids([make(title="One")])
    report = f.since_last_review([], current)
    assert report["resolved"] == []
    assert report["still_open"] == []
    assert len(report["new"]) == 1


def test_drop_waived_removes_only_the_waived_ids():
    one, two = f.with_ids([make(title="One"), make(title="Two")])
    assert [x["title"] for x in f.drop_waived([one, two], [two["id"]])] == ["One"]


def test_by_severity_groups_in_order_and_skips_empty_groups():
    items = f.with_ids([make(title="One", severity="nit"), make(title="Two", severity="blocking")])
    grouped = f.by_severity(items)
    assert list(grouped) == ["blocking", "nit"]
    assert grouped["blocking"][0]["title"] == "Two"
