# code-review-bot

An automated pull-request reviewer for the Market Data repositories. One
durable review comment per PR, findings with file and line, a verdict, a proof
gate, and a re-review on every push.

The engine is shared. The shape of each review comes from the target repo,
through two optional files on the base branch:

- `.github/code-review/REVIEW.md` — the reviewer's standing instructions.
- `.github/code-review/policy.yml` — the knobs (backends, gate, labels, proof).

A repo with neither file gets the default review.

## Calling it

Add this workflow to the target repository:

```yaml
name: Code review

on:
  pull_request_target:
    types: [opened, synchronize, reopened, ready_for_review, edited]
  issue_comment:
    types: [created]
  workflow_dispatch:
    inputs:
      pr:
        description: Pull request number
        required: true

jobs:
  review:
    uses: MarketData-App/code-review-bot/.github/workflows/review.yml@main
    secrets: inherit
```

Every repository reviews on the org's self-hosted runner, which is the
default.

**The bot reviews the organisation's own pull requests and nobody else's.** A
pull request from anyone outside gets no review, no comment and no check run,
and its head is never fetched onto the runner. This is the security boundary,
not a preference: see `docs/setup.md` section 5. A repository whose own bot
opens pull requests adds that bot's login, because a bot reads as `NONE` even
when it is yours:

```yaml
    with:
      trusted-authors: 'sdk-sync[bot]'
```

While this repository is private, other repositories cannot call it yet.
`uses: <owner>/<repo>/.github/workflows/review.yml@<ref>` resolves under the
calling repository's own permissions, and this repository's Actions access
level is `none`.

Two different fixes, for two different callers:

- **Repositories inside MarketData-App** can be allowed in without making
  anything public, by setting this repository's Actions access to
  "Accessible from repositories in the organization".
- **The `sdk-*` repositories cannot**, whatever that setting says: they are
  owned by the `MarketDataApp` **user account**, and Actions access does not
  cross from a user account to a private organisation repository. For them
  this repository has to be public, which is what spec section 2 intends.

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
the test suite. Use them when changing `reviewbot/defaults/REVIEW.md`, so the
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
