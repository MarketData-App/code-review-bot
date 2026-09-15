"""The backend protocol, the probe, and the mode handling.

A backend turns one brief into one validated result. It knows nothing about
GitHub, and the engine knows nothing about which model answered.
"""

import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from reviewbot import brief as brief_mod
from reviewbot import result as result_mod


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

    # Environment variables every CLI needs to function at all. Anything
    # outside this list, plus the backend's own credential, is withheld.
    BASE_ENV = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "TERM",
        "TMPDIR",
        "USER",
        "LOGNAME",
        # Network reachability. A runner behind a proxy or an internal CA
        # cannot reach the model API without these, and the failure arrives as
        # a bare non-zero exit. None of them carries a credential.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    )

    # The credential this backend is allowed to see. Subclasses override.
    CREDENTIAL_ENV: tuple[str, ...] = ()

    def probe(self) -> bool:
        raise NotImplementedError

    def child_env(self, env: dict | None = None) -> dict:
        """The environment the model subprocess gets: its own credential, no more.

        A model can read /proc/self/environ, so anything here is readable by a
        hostile diff. The GitHub App token, the App private key and the other
        backend's credential must never appear (spec section 6). The
        credential this backend is using cannot be hidden from it; the
        org-only gate is what covers that.
        """
        source = os.environ if env is None else env
        out = {k: source[k] for k in self.BASE_ENV if k in source}
        for name in self.CREDENTIAL_ENV:
            if source.get(name):
                out[name] = source[name]
        return out

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
                    env=self.child_env(),
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


def live(policy: dict, checkout: str) -> tuple[list[Backend], list[str]]:
    """The backends that are installed and authenticated, in policy order."""
    ready, dropped = [], []
    for name in policy["backends"]:
        backend = build(name, policy, checkout)
        if backend.probe():
            ready.append(backend)
        else:
            dropped.append(name)
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
        return [ready[0].review(brief)], missing + [b.name for b in ready[1:]]

    if mode == "fallback":
        failures = []
        for backend in ready:
            try:
                return [backend.review(brief)], missing + failures + [
                    b.name for b in ready[ready.index(backend) + 1 :]
                ]
            except BackendError:
                failures.append(backend.name)
        raise BackendError("every backend failed: " + ", ".join(failures))

    results, failures = [], []
    with ThreadPoolExecutor(max_workers=len(ready)) as pool:
        futures = {pool.submit(b.review, brief): b for b in ready}
        for future, backend in futures.items():
            try:
                results.append(future.result())
            except BackendError:
                failures.append(backend.name)
    if not results:
        raise BackendError("every backend failed: " + ", ".join(failures))
    results.sort(key=lambda r: policy["backends"].index(r.backend))
    return results, missing + failures
