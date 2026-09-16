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


# --- job logs: fetched whole, written to a file, greppable ------------------


def test_a_bare_actions_check_gets_its_whole_log(api):
    """An Actions check carries no output; its LOG carries everything.

    Measured on sdk-py#123: `test (3.12)` had summary_len=0 and text_len=0,
    while the same id fetched as an Actions job returned 84,872 bytes
    containing "943 passed in 48.43s". The check run's `id` IS the job id.
    """
    client, transport = api
    transport.add(
        "GET",
        f"/repos/{REPO}/commits/abc/check-runs?per_page=100",
        data={"check_runs": [dict(run("test (3.12)"), id=555)]},
    )
    transport.add(
        "GET",
        f"/repos/{REPO}/actions/jobs/555/logs",
        text="2026-09-16T00:00:00.1234567Z ===== 943 passed in 48.43s =====",
    )
    out = client.check_results("abc", exclude_check_name="Code review")
    assert "943 passed in 48.43s" in out[0]["log"]
    assert "2026-09-16T" not in out[0]["log"], "timestamps should be stripped"


def test_nothing_is_trimmed_or_selected(api):
    """The whole log, not a guess at the interesting part.

    An earlier version picked out lines it thought mattered. A measurement
    killed it: on a real log the pytest summary sat 53,681 bytes from the END,
    after coverage upload and post-action cleanup, so both a tail slice and a
    signal grep dropped results for some CI or other.
    """
    client, transport = api
    body = "\n".join(f"line {i}" for i in range(20000))
    transport.add(
        "GET",
        f"/repos/{REPO}/commits/abc/check-runs?per_page=100",
        data={"check_runs": [dict(run("test (3.12)"), id=555)]},
    )
    transport.add("GET", f"/repos/{REPO}/actions/jobs/555/logs", text=body)
    out = client.check_results("abc", exclude_check_name="Code review")
    assert "line 0" in out[0]["log"] and "line 19999" in out[0]["log"]


def test_a_check_that_already_has_output_is_not_log_fetched(api):
    # codecov fills its own output and is not an Actions job.
    client, transport = api
    transport.add(
        "GET",
        f"/repos/{REPO}/commits/abc/check-runs?per_page=100",
        data={"check_runs": [dict(run("codecov/project", text="coverage 94%"), id=777)]},
    )
    out = client.check_results("abc", exclude_check_name="Code review")
    assert "coverage 94%" in out[0]["text"]
    assert not any("/actions/jobs/" in c["path"] for c in transport.calls)


def test_an_unfetchable_log_is_not_fatal(api):
    client, transport = api
    transport.add(
        "GET",
        f"/repos/{REPO}/commits/abc/check-runs?per_page=100",
        data={"check_runs": [dict(run("test (3.12)"), id=555)]},
    )
    transport.add("GET", f"/repos/{REPO}/actions/jobs/555/logs", status=403, text="no")
    out = client.check_results("abc", exclude_check_name="Code review")
    assert out[0]["name"] == "test (3.12)"
    assert out[0]["log"] == ""


# --- the logs land on disk, and the brief points at them --------------------


def test_each_check_gets_its_own_file(tmp_path):
    from reviewbot import facts

    out = facts.write_check_logs(
        [
            {
                "name": "test (3.12)",
                "conclusion": "success",
                "summary": "",
                "text": "",
                "log": "943 passed in 48.43s",
            },
            {
                "name": "Integration Tests (live)",
                "conclusion": "success",
                "summary": "",
                "text": "",
                "log": "ok",
            },
        ],
        str(tmp_path),
    )
    paths = [e["log_path"] for e in out]
    assert paths[0] == ".reviewbot-ci/test-3.12.log"
    assert paths[1].startswith(".reviewbot-ci/integration-tests-live")
    for entry in out:
        assert (tmp_path / entry["log_path"]).read_text()
        assert "log" not in entry, "the text itself must not travel in the metadata"


def test_a_check_with_no_log_gets_no_path(tmp_path):
    from reviewbot import facts

    out = facts.write_check_logs(
        [{"name": "codecov", "conclusion": "success", "summary": "94%", "text": "", "log": ""}],
        str(tmp_path),
    )
    assert out[0]["log_path"] == ""


def test_the_brief_gives_the_path_and_not_the_log_text():
    text = brief_mod.render_check_results(
        [
            {
                "name": "test (3.12)",
                "conclusion": "success",
                "summary": "",
                "text": "",
                "log_path": ".reviewbot-ci/test-3.12.log",
                "log_bytes": 84872,
            },
        ]
    )
    assert ".reviewbot-ci/test-3.12.log" in text
    assert "84,872 bytes" in text


def test_the_brief_still_inlines_short_output_github_gave_us():
    # codecov's coverage table is already short and worth reading inline.
    text = brief_mod.render_check_results(
        [
            {
                "name": "codecov/project",
                "conclusion": "success",
                "summary": "TOTAL 2902 0 100%",
                "text": "",
                "log_path": "",
            }
        ]
    )
    assert "TOTAL 2902 0 100%" in text


def test_a_failing_check_is_listed_first():
    text = brief_mod.render_check_results(
        [
            {"name": "green", "conclusion": "success", "summary": "", "text": "", "log_path": ""},
            {"name": "red", "conclusion": "failure", "summary": "", "text": "", "log_path": ""},
        ]
    )
    assert text.index("red") < text.index("green")


def test_compose_tells_the_model_the_logs_are_not_part_of_the_diff():
    from reviewbot import config
    from tests.test_policy import make_pr

    pr = make_pr(
        check_results=[
            {
                "name": "test (3.12)",
                "conclusion": "success",
                "summary": "",
                "text": "",
                "log_path": ".reviewbot-ci/test-3.12.log",
                "log_bytes": 100,
            }
        ]
    )
    text = brief_mod.compose(pr, config.defaults(), "rules", "/w/pr")
    assert "What CI said" in text
    assert ".reviewbot-ci/test-3.12.log" in text
    assert "NOT part" in text


def test_compose_says_so_when_ci_reported_nothing():
    from reviewbot import config
    from tests.test_policy import make_pr

    text = brief_mod.compose(make_pr(check_results=[]), config.defaults(), "rules", "/w/pr")
    assert "Do not assume the tests pass" in text


def test_the_brief_tells_the_model_that_red_never_arrives():
    """Because it does not: should_skip refuses a pull request that is not green.

    So the logs are evidence -- counts, coverage, what was exercised -- not
    triage material. A reviewer told to look for failures in a green log wastes
    turns hunting something that cannot be there.
    """
    from reviewbot import config
    from tests.test_policy import make_pr

    pr = make_pr(
        check_results=[
            {
                "name": "test (3.12)",
                "conclusion": "success",
                "summary": "",
                "text": "",
                "log_path": ".reviewbot-ci/test-3.12.log",
                "log_bytes": 100,
            }
        ]
    )
    text = brief_mod.compose(pr, config.defaults(), "rules", "/w/pr")
    assert "already passed" in text
    assert "EVIDENCE" in text
