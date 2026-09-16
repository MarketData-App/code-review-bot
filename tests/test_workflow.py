"""Tests for the workflow files.

A workflow bug costs a full round trip through Actions to find, so the rules
that matter are asserted here instead.

Run: pytest tests/test_workflow.py
"""

import re
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
    assert inputs["runs-on"]["default"] == ""
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


def test_the_runner_comes_from_the_input_when_one_is_given():
    assert "inputs.runs-on" in REVIEW["jobs"]["review"]["runs-on"]


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
    # Scans the COMMANDS, not the file. Grepping the raw text matched the word
    # "make" inside a comment, which is the kind of false positive that gets a
    # safety test deleted rather than fixed.
    commands = []
    for item in steps():
        for line in (item.get("run") or "").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                commands.append(line)
    body = "\n".join(commands)
    for forbidden in [
        "npm ci",
        "pip install -r",
        "uv sync --project pr",
        "make ",
        "./pr/",
        "bash pr/",
        "python pr/",
    ]:
        assert forbidden not in body, forbidden


def test_the_review_runs_from_the_bot_checkout():
    assert "--project bot" in step("run the review")["run"]


def test_the_token_reaches_the_bot_through_the_environment():
    env = step("run the review")["env"]
    assert env["GITHUB_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"


def test_the_caller_example_uses_pull_request_target():
    assert "pull_request_target" in CALLER[ON]


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
        condition = item.get("if") or ""
        assert "steps.gate.outputs.trusted == 'true'" in condition, item.get("name")


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


def test_the_first_filter_admits_the_three_triggers():
    gate = REVIEW["jobs"]["review"]["if"]
    for event in ("pull_request_target", "issue_comment", "workflow_dispatch"):
        assert event in gate


def test_the_filter_does_not_authorise_on_author_association():
    # Measured 2026-09-15 on pull request #1, author MarketDataApp:
    #   REST API      author_association: MEMBER
    #   event payload author_association: CONTRIBUTOR
    # The payload reports PUBLIC organisation membership, and this org has
    # none, so a filter on the payload value refuses every member of the
    # organisation. `reviewbot gate` asks the API instead. Do not "restore"
    # an association check here.
    assert "author_association" not in REVIEW["jobs"]["review"]["if"]


def test_an_organisation_token_is_minted_for_the_membership_check():
    # A user-account installation answers the membership question with 404,
    # not 403, so the repository's own token cannot be trusted with it.
    mint = step("mint an organisation token")
    assert mint["with"]["owner"] == "MarketData-App"
    assert "repositories" not in mint["with"]
    assert mint["continue-on-error"] is True


def test_the_org_token_reaches_the_gate():
    assert step("refuse a pull request")["env"]["REVIEWBOT_ORG_TOKEN"] == (
        "${{ steps.org-token.outputs.token }}"
    )


def test_the_org_token_is_minted_before_the_gate():
    names = [(s.get("name") or "").lower() for s in steps()]
    assert names.index("mint an organisation token for the membership check") < names.index(
        "refuse a pull request from outside the organisation"
    )


def test_the_app_id_is_an_input_not_a_required_secret():
    # The App ID is not secret material: an unauthenticated
    # GET /apps/marketdata-code-review returns it. Making it a secret forced a
    # repository outside the organisation -- which cannot read org secrets --
    # to copy a third value for no security benefit.
    inputs = REVIEW[ON]["workflow_call"]["inputs"]
    assert inputs["app-id"]["default"] == "4955329"
    assert REVIEW[ON]["workflow_call"]["secrets"]["CODE_REVIEW_APP_ID"]["required"] is False


def test_the_secret_still_overrides_the_input():
    for name in ("mint the app installation token", "mint an organisation token"):
        assert step(name)["with"]["app-id"] == (
            "${{ secrets.CODE_REVIEW_APP_ID || inputs.app-id }}"
        )


def test_only_two_values_are_genuinely_secret():
    # What a repository outside the organisation must actually copy.
    secrets = REVIEW[ON]["workflow_call"]["secrets"]
    required = {k for k, v in secrets.items() if v.get("required")}
    assert required == {"CODE_REVIEW_APP_PRIVATE_KEY"}
    assert "CLAUDE_CODE_OAUTH_TOKEN" in secrets


def test_the_dogfood_runner_follows_the_repository_visibility():
    # allows_public_repositories is FALSE on the org runner group, so
    # skynet-org can take this job only while the repo is private. Making the
    # repo public -- required before any sdk-* repo can call this workflow --
    # would otherwise queue the job forever with no runner and no error.
    expr = SELF["jobs"]["review"]["with"]["runs-on"]
    assert "github.event.repository.private" in expr
    assert "self-hosted" in expr and "ubuntu-latest" in expr


def test_that_expression_has_no_newlines():
    # A folded scalar keeps the newlines of any line indented past the first.
    assert "\n" not in SELF["jobs"]["review"]["with"]["runs-on"]


def test_the_runner_is_derived_when_no_override_is_given():
    # The prose and the template used to disagree with the input default, and
    # a reader who believed the prose would delete the `with:` block and land
    # in a silent infinite queue. Deriving removes the trap instead of
    # documenting it.
    assert REVIEW[ON]["workflow_call"]["inputs"]["runs-on"]["default"] == ""
    expr = REVIEW["jobs"]["review"]["runs-on"]
    assert "github.event.repository.private" in expr
    assert "self-hosted" in expr and "ubuntu-latest" in expr
    assert "\n" not in expr


def test_an_explicit_runner_still_wins():
    assert "inputs.runs-on != ''" in REVIEW["jobs"]["review"]["runs-on"]


def test_the_caller_template_names_its_secrets_explicitly():
    # `secrets: inherit` only delivers to a reusable workflow in the SAME
    # organisation. The sdk-* repositories are on the MarketDataApp user
    # account and this one is in MarketData-App, so inherit crosses an owner
    # boundary and delivers nothing -- and the call then fails naming a secret
    # that is present in the repository's settings.
    secrets = CALLER["jobs"]["review"]["secrets"]
    assert secrets != "inherit"
    assert set(secrets) == {"CODE_REVIEW_APP_PRIVATE_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
    for name, value in secrets.items():
        assert value == "${{ secrets." + name + " }}"


def test_both_halves_get_the_same_organisation_token():
    # sdk-py run 35022651232: the gate said "is a member of the MarketData-App
    # organisation" and the run said "is not in the organisation", seconds
    # apart, because only one of them had this.
    for name in ("refuse a pull request", "run the review"):
        assert step(name)["env"]["REVIEWBOT_ORG_TOKEN"] == (
            "${{ steps.org-token.outputs.token }}"
        ), name


# --- the borrowed Codex credential -----------------------------------------


def test_the_credential_is_borrowed_only_after_the_gate():
    # The store token is the org App token. Borrowing before the gate would
    # hand a credential to a job started by an outsider's pull request.
    names = [(s.get("name") or "") for s in steps()]
    gate = next(i for i, n in enumerate(names) if "Refuse a pull request" in n)
    borrow = next(i for i, n in enumerate(names) if "Borrow the Codex credential" in n)
    assert gate < borrow


def test_the_credential_is_written_to_a_job_scoped_codex_home():
    # The self-hosted runner is deliberately not --ephemeral, so its filesystem
    # persists between jobs. A credential written anywhere durable would wait
    # there for the next job, possibly from another repository.
    borrow = step("Borrow the Codex credential")
    assert "$RUNNER_TEMP/codex-home" in borrow["run"]


def test_the_credential_is_returned_whatever_happens():
    # always() so it runs on failure and cancellation too, but only for a
    # trusted, borrowing job -- a refused pull request must not reach the
    # private credential store at all.
    give_back = step("Return the Codex credential")
    condition = give_back["if"]
    assert "always()" in condition
    assert "steps.gate.outputs.trusted == 'true'" in condition
    assert "inputs.credential-store != ''" in condition
    assert give_back is steps()[-1]


def test_the_codex_cli_is_installed_when_either_credential_is_present():
    install = step("Install the model CLIs")
    assert "steps.codex.outputs.fetched" in install["env"]["HAVE_CODEX"]
    assert "OPENAI_API_KEY" in install["env"]["HAVE_CODEX"]


def codex_home_argument(run_text):
    """The `--codex-home` value one step passes, unquoted."""
    match = re.search(r'--codex-home\s+"([^"]+)"', run_text)
    assert match, f"no --codex-home argument in: {run_text}"
    return match.group(1)


def test_the_review_step_reads_the_directory_the_borrow_step_wrote():
    # This asserted `"codex-home" in ...` and so would have passed with the
    # two steps pointing at different directories -- the borrow writing to
    # $RUNNER_TEMP/codex-home and the review reading somewhere else, which is
    # silent: the review would simply find no credential and drop the backend.
    # `$RUNNER_TEMP` and `${{ runner.temp }}` are two spellings of one
    # directory, so both are named here and the tail must match exactly.
    borrowed = codex_home_argument(step("Borrow the Codex credential")["run"])
    returned = codex_home_argument(step("Return the Codex credential")["run"])
    used = step("Run the review")["env"]["CODEX_HOME"]
    assert borrowed == "$RUNNER_TEMP/codex-home"
    assert returned == borrowed
    assert used == "${{ runner.temp }}/codex-home"
    assert used.split("}}", 1)[1] == borrowed.split("$RUNNER_TEMP", 1)[1]


# --- fix round 2: a credential problem must never fail a review -----------


def test_the_borrow_step_cannot_fail_the_job():
    # `reviewbot credential-checkout` exits 0 on every failure it can name,
    # but it cannot report on a failure BEFORE its body runs. The organisation
    # token above is continue-on-error, so an empty GITHUB_TOKEN is an
    # expected state, and argparse answers it with SystemExit(2) before
    # `credential_checkout` -- and its total `except Exception` -- is entered.
    # Without a shell-level fallback the step fails and the review goes red.
    borrow = step("Borrow the Codex credential")
    assert borrow.get("continue-on-error") is True or "||" in borrow["run"]
    assert '|| echo "fetched=false" >> "$GITHUB_OUTPUT"' in borrow["run"]


def test_the_borrow_fallback_names_a_definite_output():
    # `fetched=false`, not silence: a later `== 'true'` comparison must read a
    # definite value rather than an empty string.
    borrow = step("Borrow the Codex credential")
    fallback = borrow["run"].split("||", 1)[1]
    assert "fetched=false" in fallback
    assert "fetched=true" not in borrow["run"]
    # Exactly one fallback, so the output cannot be written twice in one run.
    assert borrow["run"].count("fetched=") == 1


def holder_argument(run_text):
    """The `--holder` value one step passes, unquoted."""
    match = re.search(r'--holder\s+"([^"]+)"', run_text)
    assert match, f"no --holder argument in: {run_text}"
    return match.group(1)


def test_the_borrowed_lease_holder_names_the_run_not_only_the_pull_request():
    # cancel-in-progress is true, so two runs for one repository and pull
    # request overlap routinely: the cancelled run's `always()` check-in can
    # land after the new run has taken the lease. While the holder was
    # `<repo>#<pr>` the two strings were equal, `release` could not tell them
    # apart, and the dying run freed the live run's lease.
    holder = holder_argument(step("Borrow the Codex credential")["run"])
    assert "github.run_id" in holder
    assert "github.repository" in holder
    assert "steps.pr.outputs.number" in holder


def test_the_check_in_frees_the_lease_under_the_very_same_holder():
    # A check-in under a different string frees nothing, and the lease then
    # waits out its full TTL denying Codex to every other repository.
    assert holder_argument(step("Return the Codex credential")["run"]) == holder_argument(
        step("Borrow the Codex credential")["run"]
    )


def test_a_repository_with_its_own_api_key_never_borrows():
    # Both the README and the design promise such a repository is unaffected
    # and that its key takes precedence. While it still borrowed, it held the
    # shared lease for the whole review -- denying Codex to everyone else --
    # and put a second credential on disk whose precedence nothing promises.
    assert "env.HAS_OPENAI_API_KEY != 'true'" in step("Borrow the Codex credential")["if"]
    assert "env.HAS_OPENAI_API_KEY != 'true'" in step("Return the Codex credential")["if"]


def test_the_api_key_test_is_computed_where_secrets_can_be_read():
    # A step's `if:` cannot read the `secrets` context; GitHub allows it in
    # `env:` and `with:` only. Writing `secrets.OPENAI_API_KEY == ''` straight
    # into the `if:` would not be a stricter condition, it would be a broken
    # workflow.
    assert REVIEW["jobs"]["review"]["env"]["HAS_OPENAI_API_KEY"] == (
        "${{ secrets.OPENAI_API_KEY != '' }}"
    )
    for name in ("Borrow the Codex credential", "Return the Codex credential"):
        assert "secrets." not in (step(name)["if"] or ""), name


def test_a_failed_cli_install_does_not_fail_the_review():
    # HAVE_CODEX used to be true only for the rare API-key repository. Now the
    # store publishes a credential and it is true on every review, so a
    # transient npm failure under `set -eu` would red every review. `probe()`
    # already returns False for a CLI that is not installed.
    install = step("Install the model CLIs")
    lines = [ln.strip() for ln in install["run"].splitlines() if ln.strip().startswith("npm ")]
    assert len(lines) == 2
    for line in lines:
        assert line.endswith("|| true"), line


def test_the_borrow_step_tells_the_bot_which_repository_it_is_for():
    # Without --repo/--pr the bot cannot read the target's policy, and every
    # repository would take the shared lease even when its policy never runs
    # codex -- the default is `backends: [claude, codex]` with `mode: first`,
    # so Claude runs and Codex does not.
    run = step("Borrow the Codex credential")["run"]
    assert "--repo" in run
    assert "--pr" in run


def test_the_job_timeout_exceeds_the_credential_wait_plus_the_review():
    # The wait outlasts the lease TTL (2 x timeout_minutes + 5 = 35, wait 40)
    # and one backend can take 2 x timeout_minutes = 30, so a job limit below
    # 70 would kill a review that was only ever queueing politely.
    assert REVIEW["jobs"]["review"]["timeout-minutes"] >= 70


def test_the_job_budget_the_cli_assumes_matches_the_workflow():
    # The borrow step sizes its wait against the job's budget. If these drift,
    # a job can be killed while politely queueing and it looks like a failure.
    from reviewbot import cli

    assert REVIEW["jobs"]["review"]["timeout-minutes"] == cli.JOB_BUDGET_MINUTES


def test_the_force_input_reaches_the_review_step():
    # A skip you cannot override is a trap: `waive` and a deliberate re-review
    # both need a way back in.
    assert REVIEW[ON]["workflow_call"]["inputs"]["force"]["default"] is False
    assert "inputs.force" in step("Run the review")["env"]["REVIEWBOT_FORCE"]
