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

The runner follows the target repository's visibility, so neither snippet
names one: a private repository inside MarketData-App gets the self-hosted
runner, and a public repository gets `ubuntu-latest`. Pass `runs-on` only to
override that — a public repository, or any repository on the MarketDataApp
user account, must not ask for the self-hosted runner, because the org's
runner group refuses it and the job queues forever with nothing saying why.

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

A repository that prefers an API key sets `OPENAI_API_KEY` **and forwards it
in the caller's `secrets:` block**, which the template does not do for you.
The key then takes precedence: the job skips the borrow step entirely, so no
borrowed credential is ever written and the shared lease is left for the
repositories that need it. Precedence is decided by the job, not by the Codex
CLI reading two credentials and choosing one. A key that is stored but not
forwarded does nothing, and the repository borrows as before.

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
