"""Will a target repository's caller actually start a review?

The bot is installed by copying `code-review.yml` into a target repository.
Whether it ever RUNS depends on a string match GitHub performs silently:
`on.workflow_run.workflows` lists workflow `name:` values, and a name that
matches no workflow in that repository never fires. There is no warning, no
failed run and no log line -- the review simply never happens, and the
repository looks healthy because every other check is green.

Two live examples, both found 2026-09-22:

  * Five SDK callers listed `workflows: ["Tests", "Lint"]`. None of those
    repositories has a workflow named `Lint`. Reviews still ran, via `Tests`,
    so the dead entry survived unnoticed.
  * sdk-js's pull-request workflow was once named `CI` and sdk-java's
    `Pull Request`. Both are `Tests` today. A rename back would switch reviews
    off in silence, and the only symptom would be reviews quietly stopping.

This module is PURE. Text in, verdict out, no network -- so the default test
run keeps its promise of no token and no socket. `reviewbot audit-callers`
does the fetching and calls `activation` per repository.
"""

import dataclasses

import yaml

# The one reusable workflow a caller may invoke. A suffix match accepted any
# `.../review.yml`, so a typoed owner or an unrelated repository's file passed
# the audit while invoking nothing here.
REVIEW_WORKFLOW = "MarketData-App/code-review-bot/.github/workflows/review.yml"
LOCAL_REVIEW_WORKFLOW = "./.github/workflows/review.yml"


@dataclasses.dataclass(frozen=True)
class Activation:
    """Whether a caller will start a review, and why not when it will not."""

    ok: bool
    matched: list[str]
    reasons: list[str]


def triggers(spec: dict) -> dict:
    """The `on:` block, whichever key YAML produced for it.

    `on` is a YAML 1.1 boolean, so PyYAML returns the key `True` rather than
    the string `"on"`. Reading only `"on"` reported every workflow as having
    no triggers at all, which would have made this auditor claim the whole
    fleet was broken.
    """
    if not isinstance(spec, dict):
        return {}
    found = spec.get(True, spec.get("on"))
    return found if isinstance(found, dict) else {}


def runs_on_pull_request(on) -> bool:
    """Does this `on:` block run the workflow for a pull request?

    Accepts all three shapes GitHub allows: a bare string, a list, and a map.
    """
    if isinstance(on, str):
        return on == "pull_request"
    if isinstance(on, list):
        return "pull_request" in on
    if isinstance(on, dict):
        return "pull_request" in on
    return False


def _pull_request_workflows(files: dict) -> dict:
    """{workflow name: [filename, ...]} for every workflow that runs on a pull request.

    The filename comes back too, because a workflow can be switched off in the
    Actions UI while its file stays exactly where it is -- and `state` is keyed
    by file, not by name.

    A file that will not parse is SKIPPED rather than fatal: one broken
    workflow elsewhere in `.github/workflows` must not make a healthy caller
    report as broken.
    """
    found = {}
    for filename, text in (files or {}).items():
        try:
            spec = yaml.safe_load(text or "")
        except yaml.YAMLError:
            continue
        if not isinstance(spec, dict):
            continue
        on = spec.get(True, spec.get("on"))
        if runs_on_pull_request(on) and isinstance(spec.get("name"), str):
            # EVERY filename, not the first. Two workflows may share a
            # `name:`; keeping one meant a disabled duplicate could mask an
            # active workflow and report a false failure.
            found.setdefault(spec["name"], []).append(filename)
    return found


def _as_names(value) -> list[str] | None:
    """`value` as a list of strings, or None when it is not one.

    YAML accepts a bare scalar where GitHub documents a list, so a string is
    read as a one-element list. Anything else -- a mapping above all -- is
    rejected rather than coerced: membership and iteration both succeed on a
    dict and silently answer about its KEYS.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _calls_the_review_workflow(
    caller: dict, workflow_files: dict, known_refs: set | None = None
) -> tuple[bool, str]:
    """Does any job in this caller invoke THIS bot's reusable workflow?

    A caller can carry a flawless `on:` block and still start nothing, so the
    job is checked too -- and against the canonical target, at any ref. A
    suffix match would accept `SomeoneElse/code-review-bot/...`, a typoed
    owner, or `MarketDataApp/...` for `MarketData-App/...`: all of them real
    files that are not this workflow.

    The local form is what self-review.yml uses, and it is accepted only when
    the file it names is actually present.
    """
    jobs = caller.get("jobs") if isinstance(caller, dict) else None
    problems = []
    for job in (jobs or {}).values():
        uses = job.get("uses") if isinstance(job, dict) else None
        if not isinstance(uses, str):
            continue
        # The two forms have DIFFERENT syntax, and each is only valid in its
        # own shape: a cross-repository call requires a non-empty `@ref`, and
        # a local call must carry none at all. Splitting on `@` and looking
        # only at the path accepted both illegal spellings.
        if "@" in uses:
            target, _, ref = uses.partition("@")
            if target != REVIEW_WORKFLOW or not ref:
                continue
            # A ref that does not resolve is a 404 at run time. `known_refs`
            # is None when nobody looked, which is not the same as broken.
            if known_refs is not None and ref not in known_refs:
                problems.append(f"{uses} names ref {ref!r}, which does not resolve in the bot")
                continue
            return True, ""
        if uses != LOCAL_REVIEW_WORKFLOW:
            continue
        local = (workflow_files or {}).get("review.yml")
        if local is None:
            problems.append(f"{uses} names a file that is not in .github/workflows")
            continue
        # A local file is only callable if it is REUSABLE.
        try:
            spec = yaml.safe_load(local) or {}
        except yaml.YAMLError:
            problems.append("review.yml could not be read")
            continue
        if "workflow_call" not in triggers(spec):
            problems.append("review.yml does not declare workflow_call; `uses:` cannot invoke it")
            continue
        return True, ""
    return False, problems[0] if problems else ""


def activation(
    caller_text: str | None,
    workflow_files: dict,
    states: dict | None = None,
    known_refs: set | None = None,
) -> Activation:
    """Will this caller start a review when a pull request's CI finishes?

    `workflow_files` maps filename to text for everything in the target's
    `.github/workflows`, the caller included.

    `states` maps filename to the Actions state GitHub reports (`active`,
    `disabled_manually`, `disabled_inactivity`). A disabled workflow keeps its
    file and runs nothing, so contents alone cannot answer this. `None` means
    we did not ask -- which is not the same as everything being off, so it is
    treated as no opinion.
    """
    if caller_text is None:
        return Activation(False, [], ["no code-review.yml in .github/workflows"])
    try:
        caller = yaml.safe_load(caller_text)
    except yaml.YAMLError as exc:
        return Activation(False, [], [f"code-review.yml could not be read: {exc}"])

    # A PERFECT TRIGGER THAT STARTS NOTHING. The caller must actually call the
    # reusable workflow; without this a file with the right `on:` and an
    # unrelated job reported healthy.
    calls, why = _calls_the_review_workflow(caller, workflow_files, known_refs)
    if not calls:
        return Activation(
            False,
            [],
            [why or f"no job calls {REVIEW_WORKFLOW}; the trigger starts nothing"],
        )

    on = triggers(caller)
    run_on = on.get("workflow_run")
    if not isinstance(run_on, dict):
        # How all five SDK callers shipped: the block present but commented,
        # so the file parses cleanly and starts nothing.
        return Activation(False, [], ["no workflow_run trigger; a push starts no review"])

    reasons: list[str] = []

    # ABSENT `types` IS FINE. GitHub fires a workflow_run for every activity
    # type when `types` is omitted, `completed` among them. Treating a missing
    # key as an empty list called a working caller broken -- a false positive
    # in the very thing meant to catch false negatives.
    types = run_on.get("types")
    if types is not None:
        listed = _as_names(types)
        if listed is None:
            reasons.append(f"workflow_run.types is {types!r}; it must be a list of strings")
        elif "completed" not in listed:
            # The review gate refuses a commit whose checks are still running,
            # so anything but `completed` reviews a pull request that is not
            # ready.
            reasons.append(f"workflow_run.types is {types!r}; it must include 'completed'")

    # `_as_names` rather than truthiness: `"completed" in {"completed": None}`
    # is True and iterating a mapping yields its keys, so a schema-invalid
    # caller that GitHub will not run was being reported healthy.
    wanted = _as_names(run_on.get("workflows"))
    if wanted is None:
        reasons.append(
            f"workflow_run.workflows is {run_on.get('workflows')!r}; "
            f"it must be a list of workflow names"
        )
        wanted = []
    elif not wanted:
        reasons.append("workflow_run.workflows is empty; it matches nothing")

    # BRANCH FILTERS, which apply to the TRIGGERING run's branch -- the pull
    # request's head branch, not the base. A caller filtered to `main`
    # therefore fires for pushes to main and never for a pull request, while
    # every other check on that pull request stays green. Nothing in the
    # fleet uses one today; the point is that adopting one would disable
    # reviews as silently as a renamed workflow.
    for key in ("branches", "branches-ignore"):
        if key not in run_on:
            continue
        patterns = _as_names(run_on[key])
        if patterns is None:
            reasons.append(f"workflow_run.{key} is {run_on[key]!r}; it must be a list of patterns")
        elif key == "branches" and patterns != ["**"]:
            # ONLY a bare `**`. GitHub's branch globs give `*` no authority
            # over `/`, so `branches: ["*"]` silently skips every `feature/x`
            # head -- and a negation such as `["**", "!main"]` excludes again
            # after the catch-all. Anything but the one provably total filter
            # is reported.
            reasons.append(
                f"workflow_run.branches is {patterns!r}; a pull request's head branch is "
                f"arbitrary, and only ['**'] matches every branch (`*` does not cross `/`)"
            )
        elif key == "branches-ignore":
            reasons.append(
                f"workflow_run.branches-ignore is {patterns!r}; a pull request from one of "
                f"those branches starts no review"
            )

    # A DISABLED CALLER runs nothing while looking perfectly correct on disk.
    #
    # `states is None` means we never asked. A MAPPING means we did, and a file
    # missing from it is then unverified rather than fine -- an empty or
    # truncated Actions response must not read as a clean bill of health.
    if states is not None:
        caller_state = states.get("code-review.yml")
        if caller_state is None:
            reasons.append("code-review.yml has no Actions state; it could not be verified")
        elif caller_state != "active":
            reasons.append(f"code-review.yml is {caller_state} in Actions; it will not run")

    available = _pull_request_workflows(workflow_files)
    matched = []
    for name in wanted:
        if name not in available:
            reasons.append(
                f"{name!r} matches no workflow that runs on a pull request "
                f"(available: {', '.join(sorted(available)) or 'none'})"
            )
            continue
        if states is not None:
            # GitHub fires the caller if ANY workflow with this name is
            # active, so the name passes when any of its files does.
            seen = {f: states.get(f) for f in available[name]}
            if not any(state == "active" for state in seen.values()):
                unknown = [f for f, state in seen.items() if state is None]
                if unknown:
                    reasons.append(
                        f"{name!r} ({', '.join(unknown)}) has no Actions state; "
                        f"it could not be verified"
                    )
                else:
                    detail = ", ".join(f"{f} is {s}" for f, s in sorted(seen.items()))
                    reasons.append(f"{name!r} fires no workflow_run: {detail}")
                continue
        matched.append(name)

    if not matched:
        reasons.append("nothing will ever trigger a review")
    return Activation(not reasons, matched, reasons)


def report(results: dict) -> str:
    """One line per repository, then the detail for any that will not activate."""
    lines = []
    for repo in sorted(results):
        got = results[repo]
        if got.ok:
            lines.append(f"  {repo:<34} will activate on: {', '.join(got.matched)}")
        else:
            lines.append(f"  {repo:<34} WILL NOT ACTIVATE")
    broken = {r: g for r, g in results.items() if not g.ok}
    if broken:
        lines.append("")
        for repo in sorted(broken):
            lines.append(f"{repo}:")
            lines += [f"  - {reason}" for reason in broken[repo].reasons]
    return "\n".join(lines)
