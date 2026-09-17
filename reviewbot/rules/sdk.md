# SDK pull request rules

These rules apply to every Market Data SDK. The acceptance criteria they build
on are the SDK requirements document at
https://www.marketdata.app/docs/internal/sdk-requirements/. Where that document
and this file disagree, that document wins. Where this file and a general
review rule disagree, this file wins.

## 1. The version bump is a calculation, not an opinion

Two different questions get called "breaking". Keep them apart.

1. **Is the API change breaking?** One answer for every SDK, taken from the API
   schema diff: a parameter removed, a type changed, a default changed. It is
   decided upstream in the `api` repository, and it is not yours to decide.
2. **Does this pull request break THIS SDK's public surface?** A separate answer
   for each SDK. This is the question you answer, and it decides the bump.

The two answers are independent. An API change that is purely additive can
still break an SDK: when `dte` used to accept one value and now accepts a
range, a Java `dte(String)` has to become `dte(DteFilter)` and every caller
breaks, while a dynamically typed Python signature may not change at all. A
breaking API change can also need no SDK change at all.

**Do not form an opinion. Compare.**

This repository's own rules, below, say what counts as a public symbol in
this language and what breaks it. Build one row for every public symbol the
diff touches:

| symbol | before | after | breaking |
|--------|--------|-------|----------|

Read "before" from the `-` lines of the diff and from the base ref; read
"after" from the `+` lines. Then apply this test, without judgement:

- a public symbol that disappeared or was renamed: **breaking**
- a parameter removed, renamed, reordered, or made required: **breaking**
- a parameter type or a return type that changed: **breaking**
- a default value that changed: **breaking**
- the error type a call raises, throws or returns, changed: **breaking**
- a new public symbol, a new optional parameter, a widened accepted type:
  **not breaking**

Put the table in `evidence`. A breaking verdict with no table is an opinion,
and opinions are what this rule exists to remove: an accidental major release
is the failure it prevents.

State the result in `summary`, in this form:

`Version impact: major (1.3.0 -> 2.0.0)`, or `minor`, or `patch`, or `none`.

## 2. The bump arrives with the pull request

- a non-breaking change takes the next minor: 2.5 becomes 2.6
- a breaking change takes the next major: 2.5 becomes 3.0
- the decision belongs to each SDK on its own. The same API change can be a
  major here and a minor in the SDK next door.

A pull request that breaks the public surface carries its major bump with it.
The bump arrives as three things in the same pull request. A breaking change
that is missing any of them is `blocking`:

1. **A `CHANGELOG.md` entry under `## [Unreleased]`**, under a heading that
   names it as breaking: `### Removed (BREAKING)`, `### Changed (BREAKING)`.
   The release workflow extracts the release notes from this file and matches
   the `## [X.Y.Z]` heading exactly, so an entry that is absent here never
   reaches the release notes.
2. **A migration line** on every removal and every changed signature: the
   concrete replacement call, and every difference a caller will notice. The
   shape of the answer, a dropped column, a parameter that now has to be
   passed.
3. **The version impact, stated in the pull request description**: the word
   `major` and the two numbers. If the description claims a smaller bump than
   your table computes, that is a blocking finding, and your table is the
   evidence.

Do not ask the author to edit the version in the package metadata. The version
comes from that metadata, and the `tag-and-release` workflow promotes
`## [Unreleased]` and sets the number. The pull request's job is to make the
number unarguable, not to write it.

## 3. What needs no bump

Say this plainly when it applies, and do not ask for a CHANGELOG entry that the
change does not need:

- tests, test fixtures, CI configuration, lint configuration
- comments, docstring wording, formatting
- internal refactoring behind an unchanged public surface

A pull request that only adds tests needs no release. Tests that *find* a bug
do, and the fix carries its own entry.

## 4. The SDK stays aligned with the API

An API improvement that never reaches the SDKs is the failure this rule
prevents. The SDKs and the API stay aligned at all times.

- A pull request that adds support for an API capability covers the whole
  capability: every parameter and every accepted value the API documents, not
  the subset the author needed. Partial coverage is a finding, and you name
  exactly what is missing.
- The canonical REST documentation is authoritative for paths, parameters and
  response shapes. When the SDK and the REST documentation disagree, the SDK is
  wrong.
- Every capability needs a first-class method: `stocks` needs `prices`,
  `quotes`, `candles`, `earnings` and `news`; `options` needs `chain`,
  `expirations`, `quotes` and `lookup`; `funds` needs `candles`; `markets`
  needs `status`; `utilities` needs `status`, `headers` and `user`. Naming is
  idiomatic for the language.

## 5. A defect here is probably a defect in the others

These SDKs are six implementations of one contract, and defects travel between
them. The candle chunking defect is the example: the API treats `to=` as
inclusive, the SDKs assumed it was exclusive, and the boundary day came back
twice. It was present in four of the five SDKs.

When a pull request fixes a defect in logic that every SDK implements, say in
`summary` whether the same defect can exist in the sibling SDKs, and name them.
The logic this covers:

- date-range splitting and chunk merging
- pagination and batch fan-out
- CSV assembly, and the header row of a merged body
- response header parsing and rate-limit accounting
- no-data detection and the empty answer placeholder
- money parsing and decimal precision
- date and time normalisation

Report this as `should_fix`, never as `blocking`. The fix in front of you is
still correct. The purpose is that somebody opens the sibling issues.

## 6. Tests

- Unit tests mock every HTTP call. A new behaviour needs a test that fails
  without the change.
- **Every endpoint needs at least one integration test that calls the live
  API.** A mock proves only that the SDK parses the response the author
  expected, and that is the assumption that goes stale when the API changes. A
  pull request that adds or changes an endpoint method with no live test is a
  finding.
- An integration suite that skips when the token is absent reports success
  while it tests nothing. A change that makes a missing token skip rather than
  fail is `blocking`.
- An integration test asserts on the shape of the decoded answer: that the
  fields the SDK models arrived, and that they carry usable values. A test that
  checks only the status code passes against an endpoint that has silently
  started to answer with an empty body.
- Coverage is 100%. A new branch that nothing reaches is a finding, and so is a
  new branch with no test. Every coverage-ignore carries a comment that says
  why.

## 7. Documentation in the same pull request

A new public method, a changed parameter, a changed default and a changed
return type each need, in the same pull request:

- the docstring or the doc comment on the public method
- the resource page under `docs/`
- the README, when the method list or the quick start changes
- the `CHANGELOG.md` entry

## 8. Gotchas: where a compliant SDK drifts

These SDKs already implement the requirements document. This section is not a
checklist of it. It is the short list of places where an ordinary, well-meant
pull request quietly un-implements something, and where a reviewer reading only
the diff would pass it.

### Money loses its exactness at the decode step, not in the model

Python, Java and C# hold money in an exact decimal type. Three shapes regress
that, and all three read as correct:

- **converting after the fact.** `Decimal(str(value))` applied to a number
  `json.loads` has already made a float. The exact value died at decode, and
  this only locks the error in. The API's decimal string must reach the decimal
  type directly.
- **a new monetary field typed as a float** because the field beside it is one.
  Money is anything denominated in currency: bid, ask, mid, last, change, OHLC,
  strike, intrinsic and extrinsic value, underlying price, and the EPS figures.
  Greeks, implied volatility, percentages and sizes are not money.
- **removing the decimal-preserving decoder** as a simplification.

Two changes that look like improvements and are not. Making DataFrame or CSV
output decimal: both are exempt deliberately, polars included, so that identical
code does not return a different dtype depending on which optional dependency is
installed. And adding a third-party decimal library to Go, PHP or JavaScript:
those SDKs are exempt because the language has no native decimal, and a foreign
money type in a public field forces that dependency on every consumer.

### The no-data path must never widen

404 means no data and returns an empty result. **402 means the API refused a
valid request because the plan does not cover it, and it must throw.** A new
endpoint or a new fallback that funnels any other status into the empty result
is `blocking`. It tells the caller the data does not exist, which is false, and
it leaves no exception for anyone to catch: a consumer walking back through
history to find its depth reads a plan denial as the end of the archive and
truncates silently.

200 and 203 are both success. 203 is cached or historical data.

### New fan-out uses the shared pool

Concurrency is one client-wide pool of 50, as a sliding window: a request starts
the moment one finishes. A new bulk or batch helper that writes its own loop
regresses this twice. It waits for the slowest request in each group, and it can
put more than 50 in flight beside the existing pool.

### `client.rate_limits` is a snapshot, not a per-request answer

It holds the last completed request, so under concurrency it is
non-deterministic. New per-request logic that reads it carries a race no test
will catch reliably. Per-request logic reads the rate-limit metadata attached to
the response it is already holding.

### A new option goes through the configuration cascade

The order is `.env`, then the environment, then client defaults, then the method
parameter. A setting read straight from the environment inside a method cannot
be overridden by either higher tier, and the bug appears only for the caller who
tried.

### The forbidden retries are the tempting ones

Retry above 500 and on network errors. Never a 4xx, and never a 429: adding one
there reads as resilience and spends the customer's credits against a wall.
`Retry-After` still overrides the computed backoff, and a status refresh inside
a retry loop stays non-blocking.

### The timeout is fixed

99 seconds, and not configurable. A pull request that adds a timeout parameter
because somebody asked for one is a finding, whatever the request said.

### A new error type carries the support context

`request_id` (from `cf-ray`), `request_url`, `status_code`, `timestamp`,
`message` and `exception_type` — on the exception, or on the returned error
value in Go. Support staff read these fields, and an error type that omits them
is invisible to them.

No new log line and no error message carries the token. Redacted means the last
four characters.

### Dates normalise to US/Eastern

The API answers in Eastern. A change that normalises to UTC, or that compares an
API date against the machine's clock, is the defect described under "Tests and
the clock" above. It reaches production code as readily as it reaches tests.
