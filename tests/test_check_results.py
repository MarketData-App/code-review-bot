"""The harness fetches CI results; the model never goes looking for them.

Measured on sdk-py#123: every `test (3.x)` check run had summary_len=0,
text_len=0, annotations=0, so the reviewer had no idea whether the tests
passed, what failed, or what was covered. The App has no `actions` permission,
so it cannot read job logs; what it can read is the check runs themselves.

Run: pytest tests/test_check_results.py
"""

import pytest

from reviewbot import brief as brief_mod
from reviewbot import github
from tests.conftest import FakeTransport

TOKEN = "ghs_" + "T" * 36
REPO = "MarketData-App/api"


def run(name, conclusion="success", summary="", text="", annotations=None):
    return {
        "name": name,
        "conclusion": conclusion,
        "status": "completed",
        "html_url": f"https://github.com/{REPO}/runs/1",
        "output": {
            "title": name,
            "summary": summary,
            "text": text,
            "annotations_count": len(annotations or []),
        },
    }


@pytest.fixture
def api():
    transport = FakeTransport()
    client = github.GitHub(REPO, TOKEN, transport=transport, sleep=lambda s: None)
    return client, transport


def test_check_results_are_collected_with_their_output(api):
    client, transport = api
    transport.add(
        "GET",
        f"/repos/{REPO}/commits/abc/check-runs?per_page=100",
        data={"check_runs": [run("test (3.12)", summary="30 passed", text="details here")]},
    )
    out = client.check_results("abc", exclude_check_name="Code review")
    assert out[0]["name"] == "test (3.12)"
    assert out[0]["conclusion"] == "success"
    assert "30 passed" in out[0]["summary"]
    assert "details here" in out[0]["text"]


def test_our_own_check_is_excluded(api):
    client, transport = api
    transport.add(
        "GET",
        f"/repos/{REPO}/commits/abc/check-runs?per_page=100",
        data={"check_runs": [run("Code review"), run("test (3.12)")]},
    )
    names = [c["name"] for c in client.check_results("abc", exclude_check_name="Code review")]
    assert names == ["test (3.12)"]


def test_a_failing_check_is_kept_even_when_it_has_no_output(api):
    # A bare failing Actions job is exactly the case worth telling the model
    # about, even when GitHub gives us nothing but the conclusion.
    client, transport = api
    transport.add(
        "GET",
        f"/repos/{REPO}/commits/abc/check-runs?per_page=100",
        data={"check_runs": [run("test (3.10)", conclusion="failure")]},
    )
    out = client.check_results("abc", exclude_check_name="Code review")
    assert out[0]["conclusion"] == "failure"


def test_an_unreadable_checks_endpoint_is_not_fatal(api):
    # Losing this context must degrade the review, never fail it.
    client, transport = api
    transport.add("GET", f"/repos/{REPO}/commits/abc/check-runs?per_page=100", status=500, text="x")
    assert client.check_results("abc", exclude_check_name="Code review") == []


def test_the_brief_carries_the_check_results():
    text = brief_mod.render_check_results(
        [
            {"name": "test (3.12)", "conclusion": "success", "summary": "30 passed", "text": ""},
            {"name": "Lint", "conclusion": "failure", "summary": "", "text": "E501 line too long"},
        ]
    )
    assert "test (3.12)" in text
    assert "30 passed" in text
    assert "failure" in text
    assert "E501 line too long" in text


def test_the_check_results_are_budgeted():
    # One chatty check must not crowd out the diff.
    huge = [{"name": "big", "conclusion": "success", "summary": "x" * 200_000, "text": ""}]
    out = brief_mod.render_check_results(huge, budget=4000)
    assert len(out) <= 4500
    assert "truncated" in out.lower()


def test_compose_puts_the_check_results_in_the_brief():
    from reviewbot import config
    from tests.test_policy import make_pr

    pr = make_pr(
        check_results=[
            {
                "name": "test (3.12)",
                "conclusion": "failure",
                "summary": "1 failed, 29 passed",
                "text": "FAILED tests/test_x.py::test_y - AssertionError",
            }
        ]
    )
    text = brief_mod.compose(pr, config.defaults(), "rules", "/w/pr")
    assert "What CI said" in text
    assert "1 failed, 29 passed" in text
    assert "test_y" in text


def test_compose_says_so_when_ci_reported_nothing():
    from reviewbot import config
    from tests.test_policy import make_pr

    text = brief_mod.compose(make_pr(check_results=[]), config.defaults(), "rules", "/w/pr")
    assert "Do not assume the tests pass" in text
