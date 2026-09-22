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


def _pull_request_workflow_names(files: dict) -> set[str]:
    """The `name:` of every workflow in the repository that runs on a pull request.

    A file that will not parse is SKIPPED rather than fatal: one broken
    workflow elsewhere in `.github/workflows` must not make a healthy caller
    report as broken.
    """
    names = set()
    for text in (files or {}).values():
        try:
            spec = yaml.safe_load(text or "")
        except yaml.YAMLError:
            continue
        if not isinstance(spec, dict):
            continue
        on = spec.get(True, spec.get("on"))
        if runs_on_pull_request(on) and isinstance(spec.get("name"), str):
            names.add(spec["name"])
    return names


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


def activation(caller_text: str | None, workflow_files: dict) -> Activation:
    """Will this caller start a review when a pull request's CI finishes?

    `workflow_files` maps filename to text for everything in the target's
    `.github/workflows`, the caller included.
    """
    if caller_text is None:
        return Activation(False, [], ["no code-review.yml in .github/workflows"])
    try:
        caller = yaml.safe_load(caller_text)
    except yaml.YAMLError as exc:
        return Activation(False, [], [f"code-review.yml could not be read: {exc}"])

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

    available = _pull_request_workflow_names(workflow_files)
    matched = []
    for name in wanted:
        if name in available:
            matched.append(name)
        else:
            reasons.append(
                f"{name!r} matches no workflow that runs on a pull request "
                f"(available: {', '.join(sorted(available)) or 'none'})"
            )

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
