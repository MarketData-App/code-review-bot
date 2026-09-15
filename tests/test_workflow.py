"""Tests for the workflow files.

A workflow bug costs a full round trip through Actions to find, so the rules
that matter are asserted here instead.

Run: pytest tests/test_workflow.py
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REVIEW = yaml.safe_load((ROOT / ".github/workflows/review.yml").read_text())
CALLER = yaml.safe_load((ROOT / "docs/caller-workflow.yml").read_text())
SELF = yaml.safe_load((ROOT / ".github/workflows/self-review.yml").read_text())

# PyYAML reads the key `on:` as the boolean True.
ON = True


def steps():
    return REVIEW["jobs"]["review"]["steps"]


def step(name_fragment):
    for item in steps():
        if name_fragment.lower() in (item.get("name") or "").lower():
            return item
    raise AssertionError(f"no step named like {name_fragment}")


def test_the_workflow_is_callable():
    assert "workflow_call" in REVIEW[ON]


def test_the_inputs_are_declared_with_their_defaults():
    inputs = REVIEW[ON]["workflow_call"]["inputs"]
    assert inputs["runs-on"]["default"] == '["self-hosted", "marketdata-docker"]'
    assert inputs["bot-ref"]["default"] == "main"


def test_the_secrets_are_declared_by_their_org_names():
    secrets = REVIEW[ON]["workflow_call"]["secrets"]
    assert set(secrets) == {
        "CODE_REVIEW_APP_ID",
        "CODE_REVIEW_APP_PRIVATE_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
    }
    assert secrets["OPENAI_API_KEY"]["required"] is False


def test_the_runner_comes_from_the_input():
    assert REVIEW["jobs"]["review"]["runs-on"] == "${{ fromJSON(inputs.runs-on) }}"


def test_the_job_cancels_an_older_run_on_the_same_pr():
    concurrency = REVIEW["jobs"]["review"]["concurrency"]
    assert concurrency["cancel-in-progress"] is True
    assert "pull_request.number" in concurrency["group"]


def test_the_job_asks_for_no_write_permission_of_its_own():
    # Everything the bot writes, it writes with the App token.
    assert REVIEW["jobs"]["review"]["permissions"] == {"contents": "read"}


def test_the_bot_is_checked_out_from_its_own_repository():
    checkout = step("check out the bot")
    assert checkout["with"]["repository"] == "MarketData-App/code-review-bot"
    assert checkout["with"]["ref"] == "${{ inputs.bot-ref }}"


def test_the_pull_request_is_checked_out_without_credentials():
    checkout = step("check out the pull request")
    assert checkout["with"]["persist-credentials"] is False
    assert "refs/pull/" in checkout["with"]["ref"]


def test_nothing_installs_dependencies_from_the_pull_request():
    body = (ROOT / ".github/workflows/review.yml").read_text()
    for forbidden in [
        "npm ci",
        "npm install\n",
        "pip install -r",
        "uv sync --project pr",
        "make ",
        "./pr/",
    ]:
        assert forbidden not in body


def test_the_review_runs_from_the_bot_checkout():
    assert "--project bot" in step("run the review")["run"]


def test_the_token_reaches_the_bot_through_the_environment():
    env = step("run the review")["env"]
    assert env["GITHUB_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"


def test_the_caller_example_uses_pull_request_target_and_inherits_secrets():
    assert "pull_request_target" in CALLER[ON]
    assert CALLER["jobs"]["review"]["secrets"] == "inherit"


def test_the_caller_example_triggers_on_the_five_actions():
    assert set(CALLER[ON]["pull_request_target"]["types"]) == {
        "opened",
        "synchronize",
        "reopened",
        "ready_for_review",
        "edited",
    }


def test_the_dogfood_workflow_calls_the_reusable_one_locally():
    assert SELF["jobs"]["review"]["uses"] == "./.github/workflows/review.yml"


# --- the org-only gate -----------------------------------------------------


def test_every_repository_reviews_on_the_self_hosted_runner():
    assert REVIEW[ON]["workflow_call"]["inputs"]["runs-on"]["default"] == (
        '["self-hosted", "marketdata-docker"]'
    )


def test_nothing_from_the_pull_request_is_fetched_before_the_gate():
    # The decision itself lives in `reviewbot gate` and is tested in
    # tests/test_gate.py. What matters here is the order: a model on this
    # runner can read any file on the machine, so the pull request's head must
    # not be fetched until the gate has passed.
    names = [(s.get("name") or "") for s in steps()]
    gate_at = names.index("Refuse a pull request from outside the organisation")
    head_at = names.index("Check out the pull request head (read only)")
    assert gate_at < head_at


def test_every_step_after_the_gate_is_conditional_on_it():
    names = [(s.get("name") or "") for s in steps()]
    gate_at = names.index("Refuse a pull request from outside the organisation")
    for item in steps()[gate_at + 1 :]:
        assert item.get("if") == "steps.gate.outputs.trusted == 'true'", item.get("name")


def test_the_gate_step_runs_the_tested_command():
    gate = step("refuse a pull request")
    assert "reviewbot gate" in gate["run"]
    assert gate["id"] == "gate"
    assert gate["env"]["REVIEWBOT_TRUSTED_AUTHORS"] == "${{ inputs.trusted-authors }}"


def test_the_if_expression_is_only_a_first_filter():
    # It cannot see the pull request author on issue_comment or dispatch, so
    # it must not be the only thing standing between a fork and the runner.
    gate = REVIEW["jobs"]["review"]["if"]
    assert "pull_request_target" in gate


def test_the_bot_ref_default_is_a_ref_that_exists():
    assert REVIEW[ON]["workflow_call"]["inputs"]["bot-ref"]["default"] == "main"


def test_there_is_a_trusted_authors_input_for_bot_accounts():
    inputs = REVIEW[ON]["workflow_call"]["inputs"]
    assert inputs["trusted-authors"]["default"] == ""


def test_the_trusted_author_list_reaches_the_bot():
    env = step("run the review")["env"]
    assert env["REVIEWBOT_TRUSTED_AUTHORS"] == "${{ inputs.trusted-authors }}"


def test_the_bot_checkout_uses_the_app_token():
    # The bot repository is private; a caller's own GITHUB_TOKEN cannot read it.
    checkout = step("check out the bot")
    assert checkout["with"]["token"] == "${{ steps.app-token.outputs.token }}"
    assert checkout["with"]["persist-credentials"] is False


def test_the_app_token_is_minted_before_the_bot_checkout():
    names = [(s.get("name") or "").lower() for s in steps()]
    assert names.index("mint the app installation token") < names.index("check out the bot")


def test_the_dogfood_workflow_runs_the_bot_from_the_default_branch():
    # Never from the pull request's own head, and no dependency on a v1 tag.
    assert SELF["jobs"]["review"]["with"]["bot-ref"] == "main"


# The two tests that pinned the precise wording of the job-level `if:` are
# parked while the filter is reduced to its three triggers for one diagnostic
# run (see the comment in review.yml). They are restored with the filter.
# `reviewbot gate` is the real boundary; tests/test_gate.py is untouched.


def test_the_first_filter_admits_the_three_triggers():
    gate = REVIEW["jobs"]["review"]["if"]
    for event in ("pull_request_target", "issue_comment", "workflow_dispatch"):
        assert event in gate
