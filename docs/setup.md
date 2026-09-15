# Setting up the code review bot

Creating a GitHub App is a human step. Everything here is done once, in a
browser and in the repository settings. It takes about ten minutes.

## 1. Create the GitHub App

`docs/app-manifest.json` holds these settings in machine-readable form. The
fastest path is to post that manifest to GitHub, which fills the whole form
in for you:

```bash
uv run python -c "
import html, json, pathlib, webbrowser
m = html.escape(pathlib.Path('docs/app-manifest.json').read_text().strip(), quote=True)
f = pathlib.Path('/tmp/create-app.html')
f.write_text('<form action=\"https://github.com/organizations/MarketData-App/settings/apps/new\" method=\"post\">'
             f'<input type=hidden name=manifest value=\"{m}\"><button>Create GitHub App</button></form>')
webbrowser.open(f.as_uri())"
```

GitHub then shows a confirmation page with every setting already filled in.
Nothing is created until you confirm there. Generating the private key stays
a manual step, in section 3 below.

To do it by hand instead, go to the **MarketData-App** organisation settings,
**Developer settings**, **GitHub Apps**, **New GitHub App**.

| Field | Value |
|---|---|
| GitHub App name | `marketdata-code-review` |
| Homepage URL | `https://github.com/MarketData-App/code-review-bot` |
| Webhook | **Uncheck Active.** The bot is driven by Actions, not by webhooks. |
| Where can this GitHub App be installed? | **Any account** |

The handle that appears on comments is `@marketdata-code-review[bot]`.

### Repository permissions

Set exactly these, and nothing else:

| Permission | Access | Why |
|---|---|---|
| Contents | **Read and write** | Arming and disarming native auto-merge. Nothing else writes. |
| Metadata | Read-only | Mandatory. |
| Pull requests | **Read and write** | The review comment, the approval, the labels. |
| Issues | **Read and write** | Issue comments on a pull request are issue comments. |
| Checks | **Read and write** | The check run and its annotations. |

And under **Organization permissions**:

| Permission | Access | Why |
|---|---|---|
| Members | **Read-only** | The org-only gate. It is how the bot knows who "we" are; see section 5. |

Leave every other permission at **No access**. Subscribe to no events.

## 2. Install it

Install the App on both accounts, on **All repositories**:

1. The **MarketData-App** organisation.
2. The **MarketDataApp** user account, where the public `sdk-*` repositories
   live. This is the same cross-account pattern `marketdata-docs-sync`
   (app id 3771308) uses.

## 3. Store the secrets

Generate a private key on the App's page. It downloads as a `.pem` file.

**Only two values are genuinely secret.** The App's numeric id is not: an
unauthenticated `GET /apps/marketdata-code-review` returns it. It is a
workflow input with a default, so a repository outside the organisation has
one fewer value to copy.

| Secret name | Value | Where |
|---|---|---|
| `CODE_REVIEW_APP_PRIVATE_KEY` | The whole `.pem` file, header and footer included | The same two places |
| `CLAUDE_CODE_OAUTH_TOKEN` | From `claude setup-token` | The same two places |
| `OPENAI_API_KEY` | Optional. Only if a repo runs the Codex backend | The same two places |

**Why the MarketDataApp repositories need their own copies.** An organisation
secret can only be granted to repositories *in that organisation*, and the
`sdk-*` repositories are owned by the MarketDataApp user account, so they are
not in the set at all. There is no sharing mechanism across that boundary:
`secrets: inherit` passes the *caller's* secrets, and the caller is the SDK
repository.

Two ways to avoid the duplication, both larger decisions:

- **Move the repository into MarketData-App.** Organisation secrets then
  reach it, and `uses:` can resolve a private bot repository once its Actions
  access is set to "Accessible from repositories in the organization" — so
  this repository would not need to be public at all.
- **Accept two secrets per SDK repository**, which is where things stand. For the organisation secrets, set the repository
access to the repositories that call the bot.

## 4. Turn it on for a repository

1. Copy `docs/caller-workflow.yml` from this repository to
   `.github/workflows/code-review.yml` in the target repository.
2. Every repository reviews on the self-hosted runner, which is the default.
   A repository whose own bot opens pull requests adds that bot's login —
   see section 5 before adding any name:

   ```yaml
       with:
         trusted-authors: 'sdk-sync[bot]'
   ```

3. Optional: add `.github/code-review/policy.yml` and
   `.github/code-review/REVIEW.md` on the default branch. Without them the
   repository gets the default review.

## 5. Who gets reviewed

**The bot reviews the organisation's own pull requests and nobody else's.** A
pull request from anyone outside gets no review, no comment and no check run,
and the job does not start.

This is the security boundary, not a preference. Reviews run on the org's
self-hosted runner, and a model on that machine can read any file on it:
measured on 2026-09-15, `codex exec -s read-only` read a `chmod 600` file
outside its working directory, `claude -p --add-dir <checkout>` did the same,
and `/proc/self/environ` exposes the job's own secrets. The runner mounts no
docker socket, so job-level container isolation is not available. A hostile
diff from a stranger could therefore read the runner's credentials and put
them in a public comment. The gate is what prevents that; there is no second
line of defence behind it.

Trust is decided by `author_association`, which GitHub sets and the pull
request cannot influence: **OWNER** and **MEMBER** pass.

### How trust is decided

**Organisation membership**, checked against `MarketData-App` on every run.
Not `author_association`, which describes a person's relationship to one
*repository* and gets this wrong in both directions on the `sdk-*` repos:
those are owned by the **MarketDataApp user account**, so an org member there
reads as `COLLABORATOR`, and so does a stranger invited to a single repository.
Membership tells them apart.

So a pull request is reviewed when its author is a member of
`MarketData-App`, on any repository, whoever owns it. Otherwise it is not,
and neither a repository invitation nor ownership of the repo changes that.

Two exceptions, both deliberate:

- **Bot accounts** go in `trusted_authors`, because a bot is never an
  organisation member. Check the account is yours first — `github.com/sdk-bot`
  belongs to an unrelated person who registered it in 2020:

  ```bash
  gh api /orgs/MarketData-App/members/<login>   # 204 = member, 404 = not yours
  ```

- **`author_association` is the fallback**, used only when the membership
  lookup cannot answer: the App has not been granted the permission below, or
  GitHub is unwell. Then `trusted_associations` (default `[OWNER, MEMBER]`)
  applies, which is the older, weaker behaviour. It is never consulted when
  membership gives a definite answer.

### The App needs one more permission

The membership lookup calls `GET /orgs/{org}/members/{login}`, which needs
**Organization permissions → Members: Read**. The App does not have it yet.

1. Open the App's settings, **Permissions & events**, **Organization
   permissions**, set **Members** to **Read-only**, and save.
2. GitHub then asks each installation to accept the new permission. Approve it
   on **MarketData-App** and on **MarketDataApp**.

Until that is done the bot still works, but it falls back to
`author_association`, which means org members on the `sdk-*` repositories are
refused. The gate's log line says which rule decided, so you can see at a
glance which mode it is in.

`COLLABORATOR` is deliberately not on that list. GitHub uses it for anyone
invited to the repository at any permission level, read included, and such a
person need not be in the organisation. A repository that does want them adds
`trusted_associations: [OWNER, MEMBER, COLLABORATOR]` to its own
`policy.yml`, knowing it is granting a job on the shared runner.

The check runs twice: once as a cheap filter in the workflow's `if:`, and
once as `reviewbot gate`, a step that runs **before the pull request head is
fetched**. The second one is the gate. The `if:` expression cannot see the
pull request's author on an `issue_comment` or a `workflow_dispatch` event,
so it is not sufficient on its own.

**Bot accounts need an explicit entry.** A bot reads as `NONE` or
`CONTRIBUTOR` even when the bot is yours. Add its login to the caller
workflow:

```yaml
    with:
      trusted-authors: 'sdk-sync[bot]'
```

Before adding any name, check that the account is actually yours:

```bash
gh api /orgs/MarketData-App/members/<login>     # 204 = member, 404 = not yours
gh api /users/<login>                           # who is this really?
```

The login `sdk-bot` on github.com, for example, belongs to an unrelated person
who registered it in 2020. Allow-listing a name you do not control hands a
stranger a job on your runner.

## 6. Optional repository settings

- **Labels.** The bot creates none. Create these four so they carry a colour:
  `review: ready`, `review: needs changes`, `review: needs proof`,
  `review: decision needed`. A maintainer applies `review: proof waived` to
  lift the proof gate; the bot honours it and never sets or clears it.
- **Auto-merge.** Only needed when `policy.auto_merge.enabled` is true. Turn
  on **Allow auto-merge** in the repository settings, and add a branch rule
  that requires the `Code review` check and the repository's CI.
- **Required check.** To make the review a merge gate, add the check named by
  `policy.check_name` (default `Code review`) to the branch protection rule.

  Know what this means for a pull request the bot refuses. A pull request from
  outside the organisation gets no check run at all, so a required check never
  appears and the pull request cannot be merged, with nothing on the pull
  request to explain why. That is the intended outcome — the bot does not
  review those — but choose it deliberately. If you want outside pull requests
  to remain mergeable by hand, do not make the review a required check.

## 7. Check that it works

Open a pull request with a small behaviour change and no evidence in the
description. Within a few minutes it should get one comment, a `Code review`
check run, and a `review: needs proof` label. If nothing appears, open the
Actions tab of the target repository: a bot failure always fails the job, and
its check run says what went wrong.
