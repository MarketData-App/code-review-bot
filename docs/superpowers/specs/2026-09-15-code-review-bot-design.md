# Code review bot — design

Date: 2026-09-15. Status: approved design, ready for an implementation plan.

## 1. Purpose

An automated pull-request reviewer for the Market Data repositories, in the
style of openclaw's ClawSweeper (MIT, https://github.com/openclaw/clawsweeper):
one durable review comment per PR, findings with file and line, a verdict, a
"needs proof" gate that asks the author for runtime evidence, a "decision
needed" escalation to a maintainer, and a re-review on every push. The
engine is shared; **the shape of each review is defined by the target repo**
through two optional files. A repo with nothing configured gets a sensible
default review.

PRs are opened by employees today and, soon, by agents (automated SDK
development). For an agent PR the bot may be the only reader before merge, so
its verdict must be reliable and its loop must be closable by the agent alone.

Non-goals for the first release: running the PR's code (a later opt-in
`verify` job), issue triage and weekly sweeps, autofix pushes to the PR
branch, a dashboard, inline review comments (check-run annotations are used
instead).

## 2. Decisions already taken

- **From scratch, not a fork.** ClawSweeper is ~154k lines of TypeScript plus
  a Cloudflare Worker control plane, a GitHub App, Blacksmith runners and the
  OpenAI Codex CLI, tightly coupled to the openclaw org. The parts we want
  (review prompt, decision schema, rating rules, comment markers, proof
  states) are small and MIT-licensed; they are borrowed as design, with
  attribution in `docs/CREDITS.md`.
- **Python 3.12 + uv**, the org's convention for agents (`support-agent`,
  `api`). Tests with pytest.
- **Reusable GitHub Actions workflow** (`workflow_call`) in a **public** bot
  repo `MarketData-App/code-review-bot`, the same reason `MarketDataApp/actions`
  is public: a workflow in a public SDK repo cannot use a private one. The
  repo holds code, prompts and the workflow only; never secrets. Reviews and
  logs live in the target repo and inherit its visibility.
- **A new GitHub App**, owned by the MarketData-App org, visibility "any
  account", installed on the org and on the MarketDataApp user account (where
  the public `sdk-*` repos live), the same cross-account pattern as
  `marketdata-docs-sync` (app id 3771308). Permissions: contents **write**
  (only used to arm/disarm native auto-merge), metadata read, pull requests
  write, issues write, checks write. App id and private key are org secrets
  for MarketData-App repos and repo secrets on MarketDataApp repos (a user
  account has no org secrets); callers pass `secrets: inherit`.
- **Runners.** Public repos review on `ubuntu-latest` (free for public repos,
  and an ephemeral VM). Private repos review on the org's self-hosted runner
  `[self-hosted, marketdata-docker]`, because hosted minutes are capped. The
  reusable workflow takes a `runs-on` input.
- **Two model backends, Claude first, Codex second**, both via their CLIs:
  `claude -p` on the subscription OAuth token (`CLAUDE_CODE_OAUTH_TOKEN`, as
  `daily-standup` and `support-agent` do) and `codex exec` (as the website's
  `scripts/marketing/lib/llm-review.mjs` does). No SDKs, no API keys for
  Claude.
- **No server, no database.** The review comment and the check run are the
  only state; every run rebuilds them.
- **Read-only review.** The job never executes anything from the PR (see §6).
  CI results are read, not re-run; the proof gate moves runtime evidence to
  the author.

## 3. Components

Bot repo layout:

```
.github/workflows/review.yml   # reusable workflow (workflow_call)
.github/workflows/ci.yml       # bot's own tests/lint
reviewbot/
  cli.py          # entry point: `reviewbot run --event <path> [--pr N]`
  github.py       # ALL GitHub I/O: PR facts, diff, comments, check run, labels,
                  # auto-merge arm/disarm. The only module that holds the token.
  config.py       # policy.yml loading, layering over defaults, validation
  facts.py        # PRFacts dataclass, the pure diff cap, path globs
  markers.py      # hidden state markers: emit (render) and parse (github)
  brief.py        # prompt composition: engine frame + REVIEW.md + PR facts
  backends/
    base.py       # Backend protocol: probe(), review(brief) -> Result
    claude.py     # claude -p, read-only tools, JSON result
    codex.py      # codex exec --ephemeral -s read-only --output-schema
  merge.py        # dedup/merge of results in `all` mode
  findings.py     # stable finding ids; resolved / open / new diff vs last review
  policy.py       # pure decisions: verdict+policy -> check conclusion, labels,
                  # gate, auto-merge arm/disarm, auto-approve
  render.py       # Result -> comment markdown (sections per policy) + markers
  schema/result.json
  rules/default.md, rules/sdk.md
  defaults/policy.yml
tests/            # unit + backend fakes; opt-in e2e behind a marker
evals/            # past PRs with known findings + scoring script
docs/
```

Target repo: a ~10-line caller workflow plus optional
`.github/code-review/REVIEW.md` and `.github/code-review/policy.yml`.

## 4. Backends and modes

`policy.backends` is a priority list, default `[claude, codex]`. Before a
run the engine **probes** each backend: binary on PATH and authenticated
(Claude: OAuth token present; Codex: `OPENAI_API_KEY` or a login file
supplied through the workflow's optional Codex credential input). A missing
credential means "not installed"; a repo with only the Claude secret keeps
working.

`policy.mode`:

- `first` (default): run the first live backend only.
- `fallback`: like `first`, but a failure (nonzero exit, timeout, two
  malformed results) moves to the next backend.
- `all`: run every live backend in parallel and merge (§4.1). Roughly double
  the cost and wall time.

If every backend fails the check run reports an error (§9); never a silent
pass. The comment footer names the backend(s) and model(s) used.

Claude invocation: `claude -p` inside the checkout, `--model` from policy
(default `claude-opus-5`, pinned like `support-agent`), read-only tools only
(`Read`, `Grep`, `Glob`), JSON output. The result is validated against
`schema/result.json`; one retry on a malformed answer with the validation
error appended to the prompt.

Codex invocation: `codex exec --skip-git-repo-check --ephemeral -s read-only
-m <model> --json --output-schema schema/result.json -o <file> -`, model
default `gpt-5.6-sol`, reasoning effort from policy. Same schema, so the
renderer does not know which backend ran.

### 4.1 Merging (`mode: all`)

One review, one finding per issue, agreement visible.

- **Located findings** (file + line) merge deterministically: same issue when
  same file, line ranges overlap or are within 3 lines, and same category.
  The merged finding keeps the wording of the higher-confidence one, the
  higher severity, and records the backends that raised it. Raised by both →
  tagged `both`; the render shows `claude` / `codex` / `both` per finding.
- **Unlocated findings** are sent, from both backends, to one short merge
  call on the primary backend with a strict instruction: group duplicates,
  keep the best wording, change nothing else. Input is small; this is the
  only model-touched step of the merge.
- **Verdict, proof status and ratings** combine as the weaker one (as
  ClawSweeper combines proof and patch tiers). The comment says which
  backend blocked and why.
- `policy.require_agreement` (default `false`): when true, a finding raised
  by one backend only is listed under "one reviewer noted" and does not
  affect the check conclusion.

## 5. Data flow

Caller workflow triggers:

- `pull_request_target`: opened, synchronize, reopened, ready_for_review,
  edited (body edits carry new proof). Drafts skip unless
  `policy.review_drafts`.
- `issue_comment` created on a PR whose body starts with the bot handle and
  `re-review` (other commands: `waive <finding id>`, maintainers only).
- `workflow_dispatch` with a PR number.
- `concurrency: { group: review-${{ pr }}, cancel-in-progress: true }`.

Reusable workflow job, in order:

1. Mint the App installation token (`actions/create-github-app-token`).
2. Check out the PR head read-only. Install the bot **from the bot repo**
   (never from the target repo).
3. Read `.github/code-review/*` from the **base branch** (a PR cannot rewrite
   its own review rules). Layer policy over defaults; validate.
4. Gather PR facts: title, body, author + bot flag, labels, changed files,
   diff (capped at `policy.max_diff_kb`; file list kept whole), CI status of
   the head commit, the bot's previous comment (last reviewed sha, previous
   finding ids), and PR comments since the last review (author pushback,
   maintainer waivers).
5. Compose the brief; run backend(s) per mode; validate; merge.
6. Decide in pure code: check conclusion, labels, gate, auto-merge, approve.
7. Publish: upsert the marker comment; write the check run on the head sha
   with the verdict as summary and each located finding as an annotation;
   apply/clear labels; approve via PR review only if policy allows and the
   verdict is ready; arm/disarm auto-merge (§8).
8. Stamp hidden markers: reviewed sha, revision, backends, proof state,
   decision state, finding ids.

Skips decided before any model runs: drafts (per policy), all changed files
matching `ignore_paths`, author in `ignore_authors`, `[skip review]` in the
title, the bot's own comments. An ignored PR gets no comment and no check
run.

Idempotent: a rerun on the same sha rebuilds the same review and edits the
same comment; only a one-line revision history accumulates.

## 6. Security invariants

- `pull_request_target` is used so fork PRs on the public SDK repos get
  secrets; it is safe only because **the job never executes anything from
  the PR**: the bot installs itself from its own repo; Claude gets read-only
  tools; Codex runs in its read-only sandbox; no target-repo script, hook or
  dependency install ever runs.
- Policy and brief are read from the base branch.
- The token exists only in `github.py`; never in the brief, logs or comment.
  Tests assert the rendered comment and brief contain no token-shaped
  strings.
- The brief cannot grant tools or change the sandbox; those live in the
  workflow.
- **No agent file from the PR head is ever loaded.** A `CLAUDE.md`,
  `AGENTS.md` or `.claude/settings.json` in the pull request is not executed,
  but a CLI started inside the checkout would read it as instructions. Both
  backends start in an empty temporary directory and reach the code by
  absolute path (`--add-dir` for Claude; Codex reads `AGENTS.md` from its cwd,
  so the empty cwd covers it). A test asserts that a hostile agent file in the
  checkout changes neither the brief nor the tool list.
- **PR-authored text is fenced.** The title, body, diff and comments are
  wrapped in sentinel lines in the brief, any forged sentinel inside them is
  defused, and the engine frame states that the fenced regions are data. Text
  in them that asks the reviewer to change its rules is reported as a security
  finding.
- The App's contents-write permission is used by exactly one code path:
  arm/disarm native auto-merge. The bot never pushes to a branch.

## 7. Per-repo files

Both optional, in `.github/code-review/` on the base branch.

`policy.yml` with bot-wide defaults:

```yaml
backends: [claude, codex]      # priority order; a missing credential drops one
mode: first                    # first | fallback | all
require_agreement: false       # all mode: single-backend findings do not block
models:
  claude: claude-opus-5
  codex: gpt-5.6-sol
codex_reasoning_effort: high
gate: true                     # check = failure on blocked, else success
require_ci_green: true         # blocked while the head commit's CI is red
proof:
  required: true               # runtime evidence expected for behavior changes
  # ANY-match over the whole PR, not a per-file filter: one matching file
  # turns the gate on for the entire change. Use it as a FLOOR under the
  # model's judgement -- a PR touching nothing here can never be blocked for
  # missing proof. Empty means every PR. Contrast auto_approve_paths (ALL).
  paths: []
ratings: true                  # show the 1-6 tier row
decision_packets: true         # allow "decision needed" escalation
auto_approve: false            # approve via PR review when clean
auto_approve_paths: []         # approve only if all changed files match
auto_merge:
  enabled: false               # default off; SDK repos may enable
  method: squash               # squash | merge | rebase
  authors: []                  # allow list; empty = nobody
review_drafts: false
ignore_paths: ["**/*.lock", "**/dist/**"]
ignore_authors: ["dependabot[bot]", "marketdata-docs-sync[bot]"]
check_name: "Code review"
max_diff_kb: 400
timeout_minutes: 15
allow_ready_with_unseen_files: false   # the diff cap hid files; ready is refused
```

A malformed `policy.yml` fails the run with a clear error in the check run;
it never silently falls back to defaults.

`REVIEW.md`: free prose, treated as the reviewer's standing instructions. A
repo file replaces the default in full; a first line `@include default`
keeps the default and appends. Suggested headings (none required): what this
repo is; house rules by area (directory-scoped, e.g. `sdk/**`: every public
method needs a docstring and an example); what counts as proof per kind of
change; what to ignore; tone and length. The default brief covers
correctness, contract with docs and tests, security basics, and evidence.

## 8. Result schema, comment, check run, labels, loop

`schema/result.json` is fixed for every repo and backend:

- `summary`: 2–3 sentences.
- `findings[]`: `file`, `line_start` (both nullable: a finding with both set
  is "located" and becomes a check-run annotation; an unlocated finding has
  neither), `line_end` (optional), `category` ∈
  {correctness, security, contract, tests, docs, style, performance},
  `severity` ∈ {blocking, should_fix, nit}, `confidence` 0–1, `title`,
  `body`, `evidence` (a file reference or quoted line).
- `proof`: `status` ∈ {sufficient, missing, insufficient, not_applicable},
  `ask` (plain-language request when not sufficient).
- `verdict`: `ready` | `needs_changes` | `blocked`, plus `reason`.
- `rating`: `patch`, `proof` tiers 1–6; `overall` = weaker, computed by the
  engine, never by the model. The tier meanings are schema semantics, so they
  live in the engine frame that `brief.py` always emits, never in
  `rules/default.md`: a repo file replaces the rule sets in full (§7), so a
  repo with its own `REVIEW.md` would otherwise rate against nothing.

  `patch`: 1 harmful, 2 wrong, 3 incomplete, 4 works with reservations,
  5 solid, 6 exemplary.

  `proof`: 1 none, 2 claimed, 3 partial, 4 adequate, 5 reproducible,
  6 comprehensive.

  `render.py` uses the same words in the rating row.
- `decision` (optional): `question`, `options[]`, `recommendation`.
- There is no praise field. It was removed on 2026-09-17: a reviewer that
  compliments weak work is one the team stops believing.

Engine adds `backend`, `model`, `reviewed_sha`, `revision`.

**Comment** (one per PR, edited in place): verdict line with check state;
summary; rating row (if enabled); "Decision needed" (if present); "Before
merge" checklist from blocking findings + proof ask; findings grouped by
severity with file:line links and backend tag; "since last review":
resolved / still open / new; footer with backends, models, sha,
revision. Hidden markers carry state. Disabled sections do not render.

**Check run** named `policy.check_name` on the head sha. Conclusion:
`success` for ready; `failure` for blocked when `gate`; `neutral` for
needs_changes and for blocked when the gate is off; `neutral` + error
summary on bot failure. Located findings become annotations (inline on the
diff).

**Labels** the bot owns and clears itself: `review: ready`, `review: needs
changes`, `review: needs proof`, `review: decision needed`. Maintainer
override labels the bot honors: `review: proof waived`.

**The loop.** Every push, body edit or `re-review` reruns everything. A
finding clears when (1) fixed, (2) the author pushes back in a comment and
the re-review agrees (the reviewer must reply to the argument either way),
or (3) a maintainer waives it (`review: proof waived` label, or a
`@<bot> waive <id>` comment; recorded with who waived). Findings have stable
ids (short hash of file + category + title) carried in the markers so the
re-review reports resolved / open / new.

**Auto-merge** (`policy.auto_merge`, default off). The bot never calls the
merge endpoint. It arms GitHub's native auto-merge when all of: verdict
ready; proof sufficient or waived; no open decision; CI green on head; the
author is on `auto_merge.authors`. When any condition later fails it disarms
and says so. Prerequisites the bot reports: "Allow auto-merge" enabled in
repo settings, and a branch rule requiring the review check (and the repo's
CI). Reading a branch rule needs an Administration read permission the App
does not have (§2) and will not be given, so the bot arms and reports
GitHub's own error text when arming is refused. `GET /repos/{owner}/{repo}`
carries `allow_auto_merge`; when the installation token returns that field
the bot uses it for a clearer message before arming, and falls through to the
arming error when the field is absent. `docs/setup.md` lists both
prerequisites for the operator.

## 9. Error handling

- Bot failure (no live backend, timeout, two malformed results, GitHub API
  error, malformed policy): check run `neutral` with a short error summary;
  the existing comment is left untouched; the job fails so it is visible and
  re-runnable in Actions. A PR is never marked failing for a bot bug.
- Partial results in `all` mode: post from the survivor; footer names the
  missing backend.
- Diff too large: hunks truncate at the limit, the file list stays whole, the
  brief and the comment say which files were unseen; the verdict cannot be
  ready with unseen files unless policy allows. Files matching `ignore_paths`
  are dropped before the budget is counted and are never reported as unseen —
  otherwise one large ignored fixture spends the whole budget, hides every
  file after it, and the pull request can never be ready. `ignore_paths` does
  two jobs: the all-match skip, and this.
- GitHub calls retry with backoff on 403/5xx. Backend calls do not retry on
  timeout; they fail over.
- Cancelled runs write nothing.
- Secrets never enter brief, logs or comment (asserted by tests).

## 10. Testing and evaluation

- Unit tests for the pure parts: policy layering/validation, brief
  composition, schema validation, merge/dedup, finding ids and the
  resolved/open/new diff, decisions, rendering, marker parsing. Fixtures are
  real PR payloads and real model outputs, recorded once and redacted.
- Backend tests against a fake `claude`/`codex` on PATH returning canned
  JSON, malformed JSON or nonzero exit: failover, retry, the missing-credential
  probe.
- One end-to-end smoke behind an opt-in pytest marker (excluded by default,
  as `support-agent` does): opens a PR on a sandbox repo, runs the workflow,
  asserts comment, check run and labels. Run by hand before releases.
- `evals/`: past PRs with known findings and a scoring script; the first
  case is openclaw/wacli#422 (six known real findings across two rounds).
  Used to tune `rules/default.md` without guessing.

## 11. Rollout

1. Bot repo: package, defaults, workflow, tests. Tag `v1` once the smoke
   passes.
2. Create the GitHub App named `marketdata-code-review` (handle
   `@marketdata-code-review`; org-owned, any-account visibility, permissions
   in §2), install on MarketData-App and MarketDataApp, store secrets. App
   creation is a human step; the bot repo documents it in `docs/setup.md`.
3. Add the Claude OAuth token secret where the App secrets live; Codex
   credential optional.
4. First target: `MarketData-App/code-review-bot` itself (dogfood), then one
   public SDK repo with `auto_merge` off, then the rest by policy.

## 12. Credits

Review structure, proof states, decision packets, rating tiers and the
durable marker comment follow openclaw/clawsweeper (MIT). The per-repo,
directory-scoped house-rules idea follows Alejandro Crosa's "Building my AI
code review clone" (2026).
