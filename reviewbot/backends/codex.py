"""The Codex backend. Task 10 fills this in."""

from reviewbot.backends.base import Backend, BackendError


class CodexBackend(Backend):
    name = "codex"

    def probe(self) -> bool:
        return False

    def _run(self, text: str) -> dict:
        raise BackendError("codex: the backend is not implemented yet")
