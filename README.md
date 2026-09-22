# code-review-bot

An automated pull-request reviewer for the Market Data repositories. One
durable review comment per PR, findings with file and line, a verdict, a proof
gate, and a re-review on every push.

The engine is shared. The shape of each review comes from the target repo,
through two optional files on the base branch:

- `.github/code-review/REVIEW.md` — the reviewer's standing instructions.
- `.github/code-review/policy.yml` — the knobs (backends, gate, labels, proof).

A repo with neither file gets the default review.

`REVIEW.md` opens with an include block naming the shipped rule sets it wants,
then carries whatever is true of that repository alone:

```markdown
@include default
@include sdk

## The public surface of this SDK
...
```

The rule sets live in `reviewbot/rules/` and ship in the wheel, so changing one
is a pull request here and reaches every repository that includes it — no
pull request needed in the repositories themselves. See
[docs/review-rules.md](docs/review-rules.md) for what belongs where.

## Calling it

Add this workflow to the target repository:

```yaml
name: Code review

on:
  workflow_run:
    workflows: ["Tests", "Lint"]   # the `name:` of your CI workflows
    types: [completed]
  issue_comment:
    types: [created]
  workflow_dispatch:
    inputs:
      pr: { description: Pull request number, required: true }
      force: { description: Re-review an unchanged commit, type: boolean, default: false }

jobs:
  review:
    if: >
      (github.event_name != 'workflow_run' ||
       github.event.workflow_run.event == 'pull_request') &&
      (github.event_name != 'issue_comment' ||
       (github.event.issue.pull_request != null &&
        startsWith(github.event.comment.body, '@marketdata-code-review')))
    uses: MarketData-App/code-review-bot/.github/workflows/review.yml@main
    secrets:
      CODE_REVIEW_APP_PRIVATE_KEY: ${{ secrets.CODE_REVIEW_APP_PRIVATE_KEY }}
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
    with:
      force: ${{ inputs.force || false }}
```

**Copy `docs/caller-workflow.yml` rather than this snippet** — it carries the
reasoning for each line, and `docs/setup.md` §4 is the step-by-step. The short
version of why it looks like this: a review must not start until CI is green, so
it triggers on CI *finishing* rather than on the push that starts it; and
`workflow_run` fires for default-branch pushes too, so the job filters to pull
requests.

Private repositories add `with: { runs-on: '["self-hosted", "marketdata-docker"]' }`.

The Codex backend needs no secret: the job borrows a short-lived credential
from a private store. Three things stop it borrowing, and the third surprises
people:

1. `credential-store: ''` in the caller.
2. An `OPENAI_API_KEY` secret on the repository, which takes precedence.
3. **A policy that never reaches Codex.** The shipped default is
   `backends: [claude, codex]` with `mode: first`, which runs Claude and never
   invokes Codex -- so the job declines the shared lease rather than holding
   one plan's credential for a backend that will not run. Put `codex` first, or
   use `mode: all`.

That store also needs a keeper publishing into it; until one does,
`credential-checkout` reports `fetched=false` and reviews run as before. Pass `credential-store: ''` to turn that off.

A repository that prefers an API key sets `OPENAI_API_KEY`, and that key takes
precedence: the job skips the borrow step entirely, so no borrowed credential
is ever written and the shared lease is left for the repositories that need
it. Precedence is decided by the job, not by the Codex CLI reading two
credentials and choosing one.

### Is the shared credential a bottleneck?

`acquire` and `release` each commit `codex/lease.json`, so the store's history
is a durable record of every borrow. `lease-report` reads it:

```bash
GITHUB_TOKEN=$(gh auth token) uv run reviewbot lease-report --days 7
```

It prints holds and time held, by day and by repository, plus the share of the
window the credential was busy and any holds that overlapped another.

It counts **holds, not waits.** A run that blocks on the lease writes no commit
and never appears here; it prints to its own job log instead. To count those,
grep a target repository's Actions log:

| String | Meaning |
|---|---|
| `reviewbot: the credential lease is held` | One line per 20 seconds waited. |
| `reviewbot: the credential lease was still held after` | The run gave up and reviewed without Codex. |

### Why a backend is missing from the footer

The comment footer renders every backend that did not contribute as
`<name> unavailable`. That one word covers three different things, so the job
log names which one:

| Log line | Meaning |
|---|---|
| `<name> is not installed or not authenticated; it will not review` | The CLI is absent or holds no credential. Nothing ran. |
| `<name> was not invoked; <other> answered first under mode: <mode>` | Healthy, and not needed. `first` and `fallback` stop at the backend that answers. |
| `<name>: <reason>` | It ran and failed. The reason is the CLI's own, scrubbed of anything token-shaped. |

The middle row is the common one and reads as a fault in the footer when it is
not: on a repository with `backends: [codex, claude]` and `mode: fallback`,
every successful review reports `claude unavailable` because Codex answered.

### Will a repository actually start a review?

`on.workflow_run.workflows` in a caller lists workflow **`name:`** values, and
GitHub matches them silently. A name matching no workflow never fires: no
warning, no failed run, no log line. Reviews stop and the repository still
looks green.

```bash
GITHUB_TOKEN=$(gh auth token) uv run reviewbot audit-callers MarketDataApp/sdk-py ...
```

It exits non-zero when any repository will not start a review, and says why:
a missing or commented-out `workflow_run`, a name matching no workflow, a
workflow that does not run on pull requests, or a repository it could not
read. `.github/workflows/audit-callers.yml` runs it daily over the whole
fleet.

The target list there is explicit, not discovered. Discovery answers "which
repositories have a caller?", so one that LOST its caller would drop quietly
out of the audit -- the same silent failure in a different place.

## Setting it up

The GitHub App is created by hand, once. `docs/setup.md` has the exact
settings. `docs/CREDITS.md` names the prior work this design follows.

## Developing

```bash
uv sync --extra dev
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The default test run uses no network, no model and no GitHub token. The
end-to-end smoke is opt-in: `uv run pytest -m e2e`.

## Evaluating the review quality

`evals/` holds recorded pull requests with known findings and a scorer:

```bash
GITHUB_TOKEN=... uv run python evals/record.py openclaw/wacli 422
uv run python evals/score.py evals/cases/*.json --results run.json
```

Recording and scoring reach the network and spend tokens, so neither runs in
the test suite. Use them when changing `reviewbot/rules/default.md`, so the
prompt is tuned against measurements rather than guesses.

## The end-to-end smoke

```bash
REVIEWBOT_E2E_REPO=MarketData-App/review-sandbox \
REVIEWBOT_E2E_TOKEN=ghs_... \
CLAUDE_CODE_OAUTH_TOKEN=... \
uv run pytest -m e2e -v
```

It opens a real pull request on a sandbox repository and closes it again. Run
it by hand before tagging a release.
