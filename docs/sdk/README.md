# SDK review rules

The standing review instructions for the six SDK repositories. They are
assembled here and copied into each repository, because six hand-maintained
copies of the same rules drift apart.

## What is in each file

| File | Scope |
|------|-------|
| `shared.md` | The rules every SDK follows. Version bumps, API alignment, sibling defects, tests, documentation. |
| `surface/<lang>.md` | What counts as the public surface in that language, and what breaks it. This is the part that cannot be shared: a change that breaks Java may not touch Python's signature at all. |
| `house/<lang>.md` | Rules that belong to one repository only. Today only `py.md` exists. |

## Where the rules come from

- The version bump rule is the team's decision in #engineering, 2026-08-28: a
  non-breaking change takes the next minor, a breaking change takes the next
  major, and each SDK decides on its own.
- The method for deciding it is Lucas's: the reviewer compares the public
  surface before and after rather than forming an opinion, so the bump is a
  calculation. This is what stops a major shipping by accident.
- The separation of the two "breaking" questions — the API schema diff, which is
  one answer for everybody, against each SDK's own public surface — is his too.
- The alignment rule is Taylor's: an API improvement that never reaches the SDKs
  is the failure to avoid.
- The sibling-defect rule comes from the candle chunking bug, which was present
  in four of the five SDKs.
- The testing, documentation and changelog rules restate the SDK requirements
  document at https://www.marketdata.app/docs/internal/sdk-requirements/, which
  stays authoritative where the two disagree.

## Assembling and publishing

```bash
docs/sdk/build.sh                 # writes build/sdk/<repo>/REVIEW.md
```

Each generated file starts with `@include default`, so the bot reads the shipped
default instructions first and then these on top. It does not replace them.

Copy a generated file to `.github/code-review/REVIEW.md` on the repository's
default branch, through a pull request. The bot reads the file from the pull
request's base ref, so a change to the rules takes effect for pull requests
opened after it merges, not for the pull request that carries it.

`sdk-csharp` is not onboarded to the bot yet. Its file is built so it is ready,
and it does nothing until the repository has a `code-review.yml` caller
workflow. See `docs/setup.md`.
