# Review rules: what the bot ships, and what a repository owns

A review is built from two places. The bot ships what more than one repository
uses. Each repository owns what is true of that repository alone.

## The shipped rule sets

They live in `reviewbot/rules/`, one Markdown file per name, and they ship
inside the wheel. A repository asks for one by name.

| Name | File | Who it is for |
|------|------|---------------|
| `default` | `reviewbot/rules/default.md` | Every repository. Language-agnostic review mechanics and standards. |
| `sdk` | `reviewbot/rules/sdk.md` | The six SDK client libraries. Version bumps, API alignment, sibling defects, tests, documentation. |

Adding a name is adding a file. `available_rule_sets()` reads the directory,
so nothing else has to be registered.

### What may enter `default`

A rule enters `default` only when it reads as sensible in **sdk-php and in the
api**. Review mechanics and language-neutral standards qualify: how to treat a
stacked pull request, whether to report a pre-existing problem, whether a red
check may be called flaky, credentials in logs, tests that read the clock.

Everything else is a house rule, however good it is. The default file has no
per-repository switch, so anything in it is paid for in the prompt of every
review in every repository, and a rule that cannot apply teaches the model that
the instructions are approximate.

## What a repository owns

`.github/code-review/REVIEW.md` on the repository's base branch. It opens with
an include block, then carries whatever is true of that repository alone:

```markdown
@include default
@include sdk

## The public surface of this SDK

...
```

The rule sets arrive in the order written, then the repository's own text.

A repository's public surface belongs here, not in the bot. Go decides its
major version in the import path, C# compiles default parameter values into
callers, and Java breaks source and binary compatibility by different rules.
None of that can be shared, and a file with exactly one consumer has no drift
to prevent.

**A file with no include block replaces the rule sets entirely.** That is
deliberate, and it is reported: every review's footer names the rule sets it
used, so a repository that dropped its includes shows it rather than going
quiet. A name the bot does not ship fails the run and says so.

## Where the SDK rules come from

- The version bump rule is the team's decision in #engineering, 2026-08-28: a
  non-breaking change takes the next minor, a breaking change takes the next
  major, and each SDK decides on its own.
- The method for deciding it is Lucas's: the reviewer compares the public
  surface before and after rather than forming an opinion, so the bump is a
  calculation. This is what stops a major shipping by accident.
- The separation of the two "breaking" questions — the API schema diff, which
  is one answer for everybody, against each SDK's own public surface — is his
  too.
- The alignment rule is Taylor's: an API improvement that never reaches the
  SDKs is the failure to avoid.
- The sibling-defect rule comes from the candle chunking bug, which was present
  in four of the five SDKs.
- The testing, documentation and changelog rules restate the SDK requirements
  document at https://www.marketdata.app/docs/internal/sdk-requirements/, which
  stays authoritative where the two disagree.

## Changing the rules

Editing a shipped rule set is one pull request here, and it reaches every
repository that includes it. No repository needs a pull request of its own,
because the include is resolved at review time from the installed bot.

Editing a repository's own rules is a pull request in that repository.

Either way the bot reads `REVIEW.md` from the pull request's **base** ref, so a
change takes effect for pull requests opened after it merges, not for the pull
request that carries it.
