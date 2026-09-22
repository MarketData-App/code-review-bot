"""The backend protocol, the probe, and the mode handling.

A backend turns one brief into one validated result. It knows nothing about
GitHub, and the engine knows nothing about which model answered.
"""

import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from reviewbot import brief as brief_mod
from reviewbot import result as result_mod
from reviewbot.redact import scrub


def jsonschema_errors(data, schema: dict) -> list[str]:
    """Validation errors for an arbitrary schema, in the same shape result.validate uses."""
    validator = Draft202012Validator(schema)
    return [
        ("/".join(str(p) for p in e.absolute_path) or "(root)") + f": {e.message}"
        for e in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    ]


class BackendError(RuntimeError):
    """One backend failed: bad exit, timeout, or two malformed answers."""


class NoBackendError(BackendError):
    """No backend is installed and authenticated."""


@dataclass(frozen=True)
class BackendResult:
    backend: str
    model: str
    result: dict


class Backend:
    """Base class. Subclasses implement probe() and _run()."""

    name = ""

    def __init__(self, policy: dict, checkout: str):
        self.policy = policy
        self.checkout = checkout
        self.model = policy["models"][self.name]
        self.timeout = max(1.0, float(policy["timeout_minutes"]) * 60)
        self.last_cwd: str | None = None

    def probe(self) -> bool:
        raise NotImplementedError

    def _run(self, text: str, schema: dict | None = None) -> dict:
        """Run the CLI once against `schema`, or the result schema by default."""
        raise NotImplementedError

    def ask(self, text: str, schema: dict) -> dict:
        """One call against a schema of the caller's choosing. No retry.

        The merge step in `mode: all` uses this, and nothing else does.
        """
        data = self._run(text, schema)
        errors = jsonschema_errors(data, schema)
        if errors:
            raise BackendError(f"{self.name}: merge answer did not match: {'; '.join(errors)}")
        return data

    def review(self, brief: str) -> BackendResult:
        """One review, with a single retry when the answer misses the schema."""
        text = brief
        last_errors = ""
        for attempt in (1, 2):
            data = self._run(text)
            errors = result_mod.validate(data) if isinstance(data, dict) else ["not an object"]
            if not errors:
                return BackendResult(backend=self.name, model=self.model, result=data)
            last_errors = "; ".join(errors)
            if attempt == 1:
                text = brief + brief_mod.retry_note(last_errors)
        raise BackendError(f"{self.name}: answer did not match the schema: {last_errors}")

    def _exec(self, cmd: list[str], stdin_text: str) -> subprocess.CompletedProcess:
        """Run the CLI from an empty directory, never from the checkout.

        The checkout is the pull request's own head. A CLAUDE.md or AGENTS.md
        in it would be read as instructions by a CLI started there, which is
        the one way a pull request could talk to its own reviewer.
        """
        with tempfile.TemporaryDirectory(prefix="reviewbot-") as neutral:
            self.last_cwd = neutral
            try:
                return subprocess.run(
                    cmd,
                    input=stdin_text,
                    cwd=neutral,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise BackendError(f"{self.name}: timed out after {self.timeout:.0f}s") from exc
            except OSError as exc:
                raise BackendError(f"{self.name}: could not start: {exc}") from exc


def build(name: str, policy: dict, checkout: str) -> Backend:
    """Construct one backend by name."""
    from reviewbot.backends.claude import ClaudeBackend
    from reviewbot.backends.codex import CodexBackend

    table = {ClaudeBackend.name: ClaudeBackend, CodexBackend.name: CodexBackend}
    if name not in table:
        raise BackendError(f"unknown backend: {name}")
    return table[name](policy, checkout)


def _report(message: str) -> None:
    """Say why a backend is not in the review, on the job log.

    The footer renders every absent backend as `<name> unavailable`, which
    cannot distinguish a backend that never started from one that started and
    failed. `run` discards the `BackendError` after recording the name, so
    without this line the reason exists nowhere: not in the comment, not in
    the lease history, not in the log. Measured 2026-09-21: eleven consecutive
    sdk-py reviews reported `claude unavailable` with HAVE_CLAUDE true and a
    token present, and no record said what went wrong.

    Scrubbed, because the reason is CLI output and the runner holds a borrowed
    `auth.json` whose `access_token` is a JWT.
    """
    print(f"reviewbot: {scrub(message)}")


def _report_not_invoked(names: list[str], answered: str, mode: str) -> None:
    """Say that a backend was skipped because it was not needed.

    THE COMMONEST CASE, and the one nothing recorded. `first` and `fallback`
    stop at the backend that answers and put every later one in the same
    `missing` list as a failure, so the footer renders `claude unavailable`
    for a backend that is healthy, installed and simply not required.
    Measured 2026-09-21 on sdk-py (`backends: [codex, claude]`,
    `mode: fallback`): eleven consecutive reviews said `claude unavailable`
    and codex had answered first every time. Nothing was wrong, and no record
    said so.
    """
    for name in names:
        _report(f"{name} was not invoked; {answered} answered first under mode: {mode}")


def live(policy: dict, checkout: str) -> tuple[list[Backend], list[str]]:
    """The backends that are installed and authenticated, in policy order."""
    ready, dropped = [], []
    for name in policy["backends"]:
        backend = build(name, policy, checkout)
        if backend.probe():
            ready.append(backend)
        else:
            dropped.append(name)
            # Deliberately different wording from the failure line below. This
            # one means the CLI is absent or holds no credential, so nothing
            # ran and no tokens were spent.
            _report(f"{name} is not installed or not authenticated; it will not review")
    return ready, dropped


def run(policy: dict, brief: str, checkout: str) -> tuple[list[BackendResult], list[str]]:
    """Run the backends the mode calls for. Returns results and missing names."""
    ready, missing = live(policy, checkout)
    if not ready:
        raise NoBackendError(
            "no backend is installed and authenticated: " + ", ".join(policy["backends"])
        )
    mode = policy["mode"]

    if mode == "first":
        result = ready[0].review(brief)
        _report_not_invoked([b.name for b in ready[1:]], ready[0].name, mode)
        return [result], missing + [b.name for b in ready[1:]]

    if mode == "fallback":
        failures = []
        for backend in ready:
            try:
                result = backend.review(brief)
            except BackendError as exc:
                _report(str(exc))
                failures.append(backend.name)
                continue
            later = [b.name for b in ready[ready.index(backend) + 1 :]]
            _report_not_invoked(later, backend.name, mode)
            return [result], missing + failures + later
        raise BackendError("every backend failed: " + ", ".join(failures))

    results, failures = [], []
    with ThreadPoolExecutor(max_workers=len(ready)) as pool:
        futures = {pool.submit(b.review, brief): b for b in ready}
        for future, backend in futures.items():
            try:
                results.append(future.result())
            except BackendError as exc:
                _report(str(exc))
                failures.append(backend.name)
    if not results:
        raise BackendError("every backend failed: " + ", ".join(failures))
    results.sort(key=lambda r: policy["backends"].index(r.backend))
    return results, missing + failures
