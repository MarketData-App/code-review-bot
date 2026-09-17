# Credits

This bot was written from scratch. Its design follows prior work, and the
parts it borrows are named here.

## openclaw/clawsweeper (MIT)

<https://github.com/openclaw/clawsweeper>

ClawSweeper is an automated pull-request reviewer for the openclaw
organisation. This bot borrows its design, not its code:

- one durable review comment per pull request, edited in place, with hidden
  markers carrying the state between runs;
- the "needs proof" gate: runtime evidence requested from the author, with
  the proof states this bot spells `sufficient`, `insufficient`, `missing`
  and `not_applicable`;
- the "decision needed" escalation: a question, options and a recommendation
  handed to a maintainer rather than guessed at;
- rating tiers from 1 to 6 for the patch and for the proof, with the overall
  tier computed as the weaker of the two;
- combining two reviewers by taking the weaker verdict and the weaker tier.

ClawSweeper is MIT licensed. No ClawSweeper code is included in this
repository.

## Alejandro Crosa, "Building my AI code review clone" (2026)

The per-repository house rules come from this article: the idea that a shared
review engine should read its standing instructions from the repository it is
reviewing, in prose, rather than carry one set of rules for every project.

The article scopes those rules by directory. This bot does not. It reads one
`.github/code-review/REVIEW.md` for the whole repository, and a rule that
applies to one directory says so in its own prose. Directory scoping earns its
keep when the engine must decide which rules to load; here the whole file is
loaded either way, so the scoping would only be a second way to say the same
thing.
