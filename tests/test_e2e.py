"""The end-to-end smoke. Opt in with `pytest -m e2e`.

It opens a real pull request on a sandbox repository, runs the real engine
against the real GitHub API with a real model, and asserts the comment, the
check run and the labels. It costs money and it writes to a repository, so it
is excluded from every default run and is called by hand before a release.

    REVIEWBOT_E2E_REPO=MarketData-App/review-sandbox \\
    REVIEWBOT_E2E_TOKEN=ghs_... \\
    CLAUDE_CODE_OAUTH_TOKEN=... \\
    uv run pytest -m e2e -v
"""

import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from reviewbot import cli, markers
from reviewbot.github import GitHub

pytestmark = pytest.mark.e2e

REPO = os.environ.get("REVIEWBOT_E2E_REPO")
TOKEN = os.environ.get("REVIEWBOT_E2E_TOKEN")

requires_sandbox = pytest.mark.skipif(
    not (REPO and TOKEN and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
    reason="set REVIEWBOT_E2E_REPO, REVIEWBOT_E2E_TOKEN and CLAUDE_CODE_OAUTH_TOKEN",
)

# A change with a real defect and no evidence: one blocking finding and a
# missing-proof verdict are the expected answer.
PATCH = '''
def charge(amount_cents, currency):
    """Charge the customer. Returns the receipt id."""
    if amount_cents < 0:
        return None
    return f"{currency}-{amount_cents / 100}"
'''


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    """A branch with one file changed, and the pull request that carries it."""
    api = GitHub(REPO, TOKEN)
    work = tmp_path_factory.mktemp("e2e")
    branch = f"reviewbot-e2e-{uuid.uuid4().hex[:8]}"

    def git(*args, cwd=work / "repo"):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    subprocess.run(
        ["git", "clone", f"https://x-access-token:{TOKEN}@github.com/{REPO}.git", "repo"],
        cwd=work,
        check=True,
        capture_output=True,
    )
    git("checkout", "-b", branch)
    Path(work / "repo" / "billing.py").write_text(PATCH)
    git("add", "billing.py")
    git(
        "-c",
        "user.email=bot@marketdata.app",
        "-c",
        "user.name=reviewbot",
        "commit",
        "-m",
        "feat: charge helper",
    )
    git("push", "origin", branch)

    pull = api._request(
        "POST",
        f"/repos/{REPO}/pulls",
        body={
            "title": "E2E: charge helper",
            "head": branch,
            "base": "main",
            "body": "Adds a charge helper.",
        },
    ).data
    yield api, pull
    api._request("PATCH", f"/repos/{REPO}/pulls/{pull['number']}", body={"state": "closed"})
    subprocess.run(
        ["git", "push", "origin", "--delete", branch],
        cwd=work / "repo",
        check=False,
        capture_output=True,
    )


@requires_sandbox
def test_a_review_appears_with_a_comment_a_check_and_a_label(sandbox, tmp_path):
    api, pull = sandbox
    checkout = tmp_path / "pr"
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            pull["head"]["ref"],
            f"https://github.com/{REPO}.git",
            str(checkout),
        ],
        check=True,
        capture_output=True,
    )

    exit_code = cli.run(
        event={"action": "opened", "pull_request": {"number": pull["number"]}},
        repo=REPO,
        token=TOKEN,
        checkout=str(checkout),
    )
    assert exit_code == 0

    time.sleep(2)
    comments = [c for c in api.issue_comments(pull["number"]) if markers.is_bot_comment(c["body"])]
    assert len(comments) == 1
    state = markers.parse(comments[0]["body"])
    assert state["reviewed_sha"] == pull["head"]["sha"]
    assert state["revision"] == 1

    runs = api._request(
        "GET", f"/repos/{REPO}/commits/{pull['head']['sha']}/check-runs?per_page=100"
    ).data["check_runs"]
    assert any(r["name"] == "Code review" for r in runs)

    labels = [
        label["name"]
        for label in api._request("GET", f"/repos/{REPO}/issues/{pull['number']}").data["labels"]
    ]
    assert any(label.startswith("review: ") for label in labels)


@requires_sandbox
def test_a_second_run_edits_the_same_comment(sandbox, tmp_path):
    api, pull = sandbox
    checkout = tmp_path / "pr2"
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            pull["head"]["ref"],
            f"https://github.com/{REPO}.git",
            str(checkout),
        ],
        check=True,
        capture_output=True,
    )
    cli.run(
        event={"action": "synchronize", "pull_request": {"number": pull["number"]}},
        repo=REPO,
        token=TOKEN,
        checkout=str(checkout),
    )
    comments = [c for c in api.issue_comments(pull["number"]) if markers.is_bot_comment(c["body"])]
    assert len(comments) == 1
    # The same head commit, so the revision must not move.
    assert markers.parse(comments[0]["body"])["revision"] == 1
