"""Will a target repository's caller actually start a review?

The `workflow_run` trigger matches a workflow's `name:`, never its filename.
A name that matches no workflow does not warn, does not fail and does not log
-- it simply never fires. Two live examples, both found 2026-09-22:

  * Five SDK callers named `workflows: ["Tests", "Lint"]` while none of those
    repositories has a workflow called `Lint`.
  * sdk-js's pull-request workflow was once called `CI` and sdk-java's
    `Pull Request`; both are `Tests` today. A rename back would switch
    reviews off in silence.

Everything here is pure: the caller's text and the repository's workflow files
go in, a verdict comes out. `reviewbot audit-callers` does the fetching. No
test in this file opens a socket.

Run: pytest tests/test_callers.py
"""

import pytest

from reviewbot import callers

ARMED = """
name: Code review
on:
  workflow_run:
    workflows: ["Tests"]
    types: [completed]
  issue_comment:
    types: [created]
jobs:
  review:
    uses: MarketData-App/code-review-bot/.github/workflows/review.yml@main
"""

TESTS_WORKFLOW = """
name: Tests
on:
  pull_request:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
"""


def workflows(**files):
    return dict(files)


# --- the happy path --------------------------------------------------------


def test_a_caller_naming_a_real_pull_request_workflow_will_activate():
    got = callers.activation(ARMED, workflows(**{"test.yml": TESTS_WORKFLOW}))
    assert got.ok is True
    assert got.matched == ["Tests"]
    assert got.reasons == []


def test_the_filename_is_irrelevant_only_the_name_counts():
    # sdk-csharp's `Tests` lives in ci.yml, sdk-java's in pull-request.yml.
    got = callers.activation(ARMED, workflows(**{"anything-at-all.yml": TESTS_WORKFLOW}))
    assert got.ok is True


# --- the failures this exists to catch -------------------------------------


def test_a_name_matching_no_workflow_is_reported():
    # THE SILENT RENAME. `Tests` became `CI` and nothing anywhere said so.
    renamed = TESTS_WORKFLOW.replace("name: Tests", "name: CI")
    got = callers.activation(ARMED, workflows(**{"test.yml": renamed}))
    assert got.ok is False
    assert any("Tests" in r and "no workflow" in r for r in got.reasons)


def test_a_dead_name_beside_a_live_one_is_still_reported():
    # The shipped template named ["Tests", "Lint"] into repositories with no
    # `Lint`. Reviews still ran, so nothing ever surfaced the dead entry.
    caller = ARMED.replace('["Tests"]', '["Tests", "Lint"]')
    got = callers.activation(caller, workflows(**{"test.yml": TESTS_WORKFLOW}))
    assert got.ok is False
    assert got.matched == ["Tests"]
    assert any("Lint" in r for r in got.reasons)


def test_a_workflow_that_does_not_run_on_pull_requests_cannot_activate_a_review():
    # A `workflow_run` on a push-only workflow fires, and then the caller's own
    # guard drops it because `workflow_run.event` is not `pull_request`.
    push_only = TESTS_WORKFLOW.replace("  pull_request:\n    branches: [main]", "  push:")
    got = callers.activation(ARMED, workflows(**{"test.yml": push_only}))
    assert got.ok is False
    assert any("pull request" in r for r in got.reasons)


def test_a_caller_with_the_trigger_commented_out_is_reported():
    # How all five SDK repositories shipped.
    held_back = (
        ARMED.replace("  workflow_run:", "  # workflow_run:")
        .replace('    workflows: ["Tests"]', '  #   workflows: ["Tests"]')
        .replace("    types: [completed]", "  #   types: [completed]")
    )
    got = callers.activation(held_back, workflows(**{"test.yml": TESTS_WORKFLOW}))
    assert got.ok is False
    assert any("workflow_run" in r for r in got.reasons)


def test_a_trigger_that_does_not_listen_for_completion_is_reported():
    caller = ARMED.replace("types: [completed]", "types: [requested]")
    got = callers.activation(caller, workflows(**{"test.yml": TESTS_WORKFLOW}))
    assert got.ok is False
    assert any("completed" in r for r in got.reasons)


def test_an_empty_workflows_list_is_reported():
    caller = ARMED.replace('workflows: ["Tests"]', "workflows: []")
    got = callers.activation(caller, workflows(**{"test.yml": TESTS_WORKFLOW}))
    assert got.ok is False


def test_a_missing_caller_is_reported_rather_than_raising():
    got = callers.activation(None, workflows(**{"test.yml": TESTS_WORKFLOW}))
    assert got.ok is False
    assert any("no code-review.yml" in r for r in got.reasons)


def test_unparseable_yaml_is_reported_rather_than_raising():
    got = callers.activation("name: [unclosed\n", workflows(**{"test.yml": TESTS_WORKFLOW}))
    assert got.ok is False
    assert any("could not be read" in r for r in got.reasons)


def test_an_unparseable_workflow_file_does_not_hide_a_good_one():
    # One bad file in .github/workflows must not make a healthy caller fail.
    got = callers.activation(
        ARMED, workflows(**{"broken.yml": "name: [unclosed\n", "test.yml": TESTS_WORKFLOW})
    )
    assert got.ok is True


# --- `on:` comes back from YAML as the boolean True ------------------------


def test_the_yaml_on_key_is_read_whether_it_parses_as_on_or_as_true():
    # `on:` is a YAML 1.1 boolean. PyYAML returns the key True, not "on", and
    # reading only "on" made every caller look like it had no triggers at all.
    assert callers.triggers({True: {"workflow_run": {}}}) == {"workflow_run": {}}
    assert callers.triggers({"on": {"workflow_run": {}}}) == {"workflow_run": {}}
    assert callers.triggers({}) == {}


@pytest.mark.parametrize(
    "shape, expected",
    [
        ("pull_request", True),
        (["pull_request", "push"], True),
        ({"pull_request": None}, True),
        ({"pull_request": {"branches": ["main"]}}, True),
        ({"push": {"branches": ["main"]}}, False),
        ("push", False),
        (None, False),
    ],
)
def test_pull_request_triggers_are_recognised_in_every_yaml_shape(shape, expected):
    assert callers.runs_on_pull_request(shape) is expected


# --- the report ------------------------------------------------------------


def test_the_report_names_the_repository_and_the_failure():
    results = {
        "MarketDataApp/sdk-py": callers.activation(ARMED, {"t.yml": TESTS_WORKFLOW}),
        "MarketDataApp/sdk-go": callers.activation(ARMED, {"t.yml": "name: CI\non:\n  push:\n"}),
    }
    text = callers.report(results)
    assert "MarketDataApp/sdk-py" in text and "MarketDataApp/sdk-go" in text
    assert "will activate" in text
    assert "WILL NOT ACTIVATE" in text


def test_the_report_says_so_when_every_repository_is_healthy():
    results = {"o/r": callers.activation(ARMED, {"t.yml": TESTS_WORKFLOW})}
    assert "WILL NOT ACTIVATE" not in callers.report(results)


# --- the command -----------------------------------------------------------
#
# Still no socket: the GitHub client is replaced with a stub that answers from
# a dict. What is tested here is the exit code, because that is what a
# scheduled run acts on.


class FakeApi:
    def __init__(self, repo, files):
        self.repo = repo
        self._files = files

    def workflow_files(self):
        return self._files.get(self.repo, {})

    def workflow_states(self):
        return {name: "active" for name in self._files.get(self.repo, {})}


def _cli(monkeypatch, files):
    from reviewbot import cli

    monkeypatch.setattr(cli, "_store_api", lambda repo, token: FakeApi(repo, files))
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    return cli


def test_the_command_exits_zero_when_every_repository_will_activate(monkeypatch, capsys):
    cli = _cli(monkeypatch, {"o/a": {"code-review.yml": ARMED, "t.yml": TESTS_WORKFLOW}})
    assert cli.audit_callers(["o/a"], token="t") == 0
    assert "will activate" in capsys.readouterr().out


def test_the_command_exits_nonzero_when_a_repository_will_not_activate(monkeypatch, capsys):
    # The whole point: a scheduled run must go RED, not print a warning into a
    # log nobody reads. Silent breakage is the failure being guarded against.
    cli = _cli(
        monkeypatch,
        {
            "o/a": {"code-review.yml": ARMED, "t.yml": TESTS_WORKFLOW},
            "o/b": {"code-review.yml": ARMED, "t.yml": "name: CI\non:\n  pull_request:\n"},
        },
    )
    assert cli.audit_callers(["o/a", "o/b"], token="t") == 1
    out = capsys.readouterr().out
    assert "WILL NOT ACTIVATE" in out
    assert "o/b" in out


def test_a_repository_that_cannot_be_read_fails_the_audit(monkeypatch, capsys):
    from reviewbot import cli
    from reviewbot.github import GitHubError

    class Boom:
        def __init__(self, *a):
            pass

        def workflow_files(self):
            raise GitHubError("GET /contents failed with 404: Not Found")

        def workflow_states(self):
            return {}

    monkeypatch.setattr(cli, "_store_api", lambda repo, token: Boom())
    # Unreadable is not the same as healthy. An audit that shrugs at a 404
    # would report a deleted caller as fine.
    assert cli.audit_callers(["o/gone"], token="t") == 1
    assert "404" in capsys.readouterr().out


# --- the scheduled workflow ------------------------------------------------
#
# An audit that checks nothing passes. These guard the list itself.


@pytest.fixture
def audit_workflow():
    import pathlib

    import yaml as _yaml

    path = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/audit-callers.yml"
    return _yaml.safe_load(path.read_text())


def _audit_targets(spec):
    env = spec["env"]
    return env["ORG_REPOS"].split() + env["USER_REPOS"].split()


def test_the_audit_runs_on_a_schedule(audit_workflow):
    # Manual-only would not catch a rename. The failure is silent, so the
    # check has to be the thing that is not silent.
    on = audit_workflow.get(True) or audit_workflow.get("on")
    assert "schedule" in on
    assert on["schedule"][0]["cron"]


def test_the_audit_covers_every_sdk_repository(audit_workflow):
    targets = _audit_targets(audit_workflow)
    for repo in (
        "MarketDataApp/sdk-py",
        "MarketDataApp/sdk-go",
        "MarketDataApp/sdk-js",
        "MarketDataApp/sdk-java",
        "MarketDataApp/sdk-csharp",
        "MarketDataApp/sdk-php",
        "MarketData-App/api",
    ):
        assert repo in targets, f"{repo} is not audited"


def test_every_audited_repository_is_named_owner_slash_name(audit_workflow):
    for repo in _audit_targets(audit_workflow):
        assert repo.count("/") == 1 and all(repo.split("/")), repo


def test_no_repository_is_audited_twice(audit_workflow):
    targets = _audit_targets(audit_workflow)
    assert len(targets) == len(set(targets))


def test_the_bots_own_repository_is_not_audited(audit_workflow):
    # It reviews itself through self-review.yml and has no code-review.yml, so
    # auditing it would fail for a reason that is not a fault.
    assert "MarketData-App/code-review-bot" not in _audit_targets(audit_workflow)


def test_both_owners_are_audited_with_their_own_token(audit_workflow):
    # An organisation installation token cannot read the user account's
    # repositories, so one step per owner is load-bearing rather than tidy.
    steps = audit_workflow["jobs"]["audit"]["steps"]
    audits = [s for s in steps if s.get("run", "").strip().startswith("uv run reviewbot audit")]
    assert len(audits) == 2
    tokens = {s["env"]["GITHUB_TOKEN"] for s in audits}
    assert len(tokens) == 2, "both audit steps use the same token"


def test_the_second_audit_runs_even_when_the_first_fails(audit_workflow):
    # One broken repository must not hide the rest of the fleet.
    steps = audit_workflow["jobs"]["audit"]["steps"]
    audits = [s for s in steps if s.get("run", "").strip().startswith("uv run reviewbot audit")]
    assert audits[1].get("if") == "always()"


# --- fixes from the bot's review of PR #21 ---------------------------------


def test_the_audit_never_runs_on_a_pull_request(audit_workflow):
    # c12ba7a7, and it was a real hole rather than a theoretical one. The job
    # mints installation tokens covering EVERY repository of each owner and
    # then runs `uv run reviewbot ...` from the checkout. With a
    # `pull_request` trigger that checkout is the PULL REQUEST'S OWN CODE, so
    # any author could edit cli.py and read the token out of the environment.
    # review.yml refuses to let a pull request near a credential for exactly
    # this reason; this workflow must hold the same line.
    on = audit_workflow.get(True) or audit_workflow.get("on")
    assert "pull_request" not in on
    assert "pull_request_target" not in on
    assert set(on) <= {"schedule", "workflow_dispatch"}


def test_omitted_types_is_accepted():
    # 2c4f6f10. GitHub runs a workflow_run for every activity type when
    # `types` is absent, `completed` included. Rejecting that called a working
    # caller broken -- the audit's own false positive.
    caller = ARMED.replace("    types: [completed]\n", "")
    got = callers.activation(caller, workflows(**{"test.yml": TESTS_WORKFLOW}))
    assert got.ok is True, got.reasons


def test_an_explicit_types_list_without_completed_is_still_rejected():
    caller = ARMED.replace("types: [completed]", "types: [requested]")
    got = callers.activation(caller, workflows(**{"test.yml": TESTS_WORKFLOW}))
    assert got.ok is False
    assert any("completed" in r for r in got.reasons)


@pytest.mark.parametrize(
    "block",
    [
        "    workflows:\n      Tests: null\n    types: [completed]",  # a map, not a list
        "    workflows: Tests\n    types:\n      completed: null",  # types as a map
    ],
)
def test_a_malformed_trigger_is_rejected_rather_than_passing_on_key_membership(block):
    # c5b6e436. `"completed" in {"completed": None}` is True and iterating a
    # mapping yields its keys, so a schema-invalid caller GitHub will not run
    # was reported healthy.
    caller = ARMED.replace('    workflows: ["Tests"]\n    types: [completed]', block)
    got = callers.activation(caller, workflows(**{"test.yml": TESTS_WORKFLOW}))
    assert got.ok is False


def test_a_non_string_workflow_name_is_rejected():
    caller = ARMED.replace('workflows: ["Tests"]', "workflows: [123]")
    got = callers.activation(caller, workflows(**{"test.yml": TESTS_WORKFLOW}))
    assert got.ok is False


# --- branch filters (2ef2f9cc, from the bot's review of PR #21) ------------
#
# `workflow_run` accepts `branches` / `branches-ignore`, and GitHub applies
# them to the TRIGGERING run's branch -- the pull request's head branch, not
# the base. A caller filtered to `main` therefore never fires for a pull
# request, while every other check stays green: the exact silent failure this
# auditor exists to catch, one level deeper than where it was looking.


def _with_branches(key, value):
    return ARMED.replace(
        '    workflows: ["Tests"]', f'    workflows: ["Tests"]\n    {key}: {value}'
    )


def test_a_branch_filter_that_excludes_feature_branches_is_reported():
    got = callers.activation(_with_branches("branches", '["main"]'), {"t.yml": TESTS_WORKFLOW})
    assert got.ok is False
    assert any("branches" in r for r in got.reasons)


def test_a_catch_all_branch_filter_is_accepted():
    # `["*"]` was accepted here in the first version of this fix, which was
    # wrong: GitHub's `*` does not cross `/`, so it skips every `feature/x`
    # head. The case now lives in
    # `test_a_single_star_branch_filter_is_rejected_because_it_skips_slashes`.
    got = callers.activation(_with_branches("branches", '["**"]'), {"t.yml": TESTS_WORKFLOW})
    assert got.ok is True, got.reasons


def test_branches_ignore_is_reported_because_it_disables_some_branches():
    got = callers.activation(
        _with_branches("branches-ignore", '["dependabot/**"]'), {"t.yml": TESTS_WORKFLOW}
    )
    assert got.ok is False
    assert any("branches-ignore" in r for r in got.reasons)


def test_a_malformed_branch_filter_is_reported():
    got = callers.activation(
        _with_branches("branches", "\n      main: null"), {"t.yml": TESTS_WORKFLOW}
    )
    assert got.ok is False


def test_no_branch_filter_at_all_stays_healthy():
    got = callers.activation(ARMED, {"t.yml": TESTS_WORKFLOW})
    assert got.ok is True


# --- round three (2ef2f9cc refined, 34cb249a) ------------------------------


def test_a_single_star_branch_filter_is_rejected_because_it_skips_slashes():
    # GitHub's branch globs: `*` matches anything EXCEPT `/`, `**` matches
    # across `/` too. So `branches: ["*"]` silently skips every `feature/x`
    # and `claude/y` head -- which is most of them here.
    got = callers.activation(_with_branches("branches", '["*"]'), {"t.yml": TESTS_WORKFLOW})
    assert got.ok is False


def test_only_a_bare_double_star_branch_filter_is_accepted():
    got = callers.activation(_with_branches("branches", '["**"]'), {"t.yml": TESTS_WORKFLOW})
    assert got.ok is True, got.reasons


def test_a_negation_beside_the_catch_all_is_rejected():
    got = callers.activation(
        _with_branches("branches", '["**", "!main"]'), {"t.yml": TESTS_WORKFLOW}
    )
    assert got.ok is False


# A workflow can be switched off in the Actions UI. The file stays exactly
# where it is, `state` becomes `disabled_manually`, and nothing ever runs.
# Reading contents alone cannot see it.

FILES = {"code-review.yml": ARMED, "t.yml": TESTS_WORKFLOW}
ACTIVE = {"code-review.yml": "active", "t.yml": "active"}


def test_active_workflows_pass():
    got = callers.activation(ARMED, FILES, states=ACTIVE)
    assert got.ok is True, got.reasons


def test_a_disabled_caller_is_reported():
    states = dict(ACTIVE, **{"code-review.yml": "disabled_manually"})
    got = callers.activation(ARMED, FILES, states=states)
    assert got.ok is False
    assert any("code-review.yml" in r and "disabled" in r for r in got.reasons)


def test_a_disabled_source_workflow_is_reported():
    states = dict(ACTIVE, **{"t.yml": "disabled_manually"})
    got = callers.activation(ARMED, FILES, states=states)
    assert got.ok is False
    assert any("Tests" in r and "disabled" in r for r in got.reasons)


def test_a_workflow_disabled_for_inactivity_is_reported():
    # GitHub switches scheduled workflows off after 60 days of repository
    # inactivity, which is how a quiet SDK would lose its reviews.
    states = dict(ACTIVE, **{"t.yml": "disabled_inactivity"})
    got = callers.activation(ARMED, FILES, states=states)
    assert got.ok is False


def test_unknown_states_are_not_treated_as_disabled():
    # `states=None` means we did not ask, not that everything is off.
    assert callers.activation(ARMED, FILES, states=None).ok is True


def test_workflow_states_reads_the_objects_the_actions_api_actually_returns():
    # Regression: this used `_paged`, which extends a list with whatever it is
    # handed. The Actions endpoint answers with an OBJECT, so `_paged` added
    # its KEYS and every repository failed with
    # "'str' object has no attribute 'get'". The stubs in this file could not
    # catch that; only a real response shape can.
    from reviewbot.github import GitHub
    from tests.conftest import FakeTransport

    transport = FakeTransport()
    transport.add(
        "GET",
        "/repos/o/r/actions/workflows?per_page=100",
        data={
            "total_count": 2,
            "workflows": [
                {"name": "Tests", "path": ".github/workflows/ci.yml", "state": "active"},
                {
                    "name": "Code review",
                    "path": ".github/workflows/code-review.yml",
                    "state": "disabled_manually",
                },
            ],
        },
    )
    got = GitHub("o/r", "t", transport=transport).workflow_states()
    assert got == {"ci.yml": "active", "code-review.yml": "disabled_manually"}


def test_workflow_states_survives_an_unexpected_body():
    from reviewbot.github import GitHub
    from tests.conftest import FakeTransport

    transport = FakeTransport()
    transport.add("GET", "/repos/o/r/actions/workflows?per_page=100", data=["not", "an", "object"])
    assert GitHub("o/r", "t", transport=transport).workflow_states() == {}
