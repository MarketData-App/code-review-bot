"""The Claude backend: `claude -p` on the subscription OAuth token.

No API key and no SDK, the same way support-agent and daily-standup run.
"""

import json
import os
import shutil

from reviewbot.backends.base import Backend, BackendError
from reviewbot.result import load_schema

# The only tools the reviewer gets. The brief cannot widen this; the list
# lives here, in the engine, next to the sandbox it belongs to.
READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
DENIED_TOOLS = ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Task"]


class ClaudeBackend(Backend):
    name = "claude"

    def probe(self) -> bool:
        """Installed and authenticated. A missing credential reads as not installed."""
        return bool(shutil.which("claude")) and bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))

    @staticmethod
    def _cli_schema(schema: dict) -> dict:
        """The schema as the CLI will accept it.

        `claude --json-schema` validates the schema itself and cannot resolve
        the draft 2020-12 meta-schema by URL, so it rejects the file outright:
        "no schema with key or ref https://json-schema.org/draft/2020-12/schema".
        Dropping the `$schema` key changes nothing about the shape it asks
        for, and reviewbot.result still validates the answer against the file
        with `$schema` intact.
        """
        return {k: v for k, v in schema.items() if k != "$schema"}

    def _command(self, schema: dict) -> list[str]:
        return [
            "claude",
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(self._cli_schema(schema)),
            "--allowedTools",
            *READ_ONLY_TOOLS,
            "--disallowedTools",
            *DENIED_TOOLS,
            "--no-session-persistence",
            "--add-dir",
            self.checkout,
        ]

    def _run(self, text: str, schema: dict | None = None) -> dict:
        proc = self._exec(self._command(schema or load_schema()), text)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
            raise BackendError(f"claude: exit {proc.returncode}: {tail[0][:300]}")
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise BackendError(f"claude: output was not JSON: {exc}") from exc
        if envelope.get("is_error"):
            raise BackendError(f"claude: {str(envelope.get('result'))[:300]}")
        body = envelope.get("structured_output")
        if body is None:
            body = envelope.get("result")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError as exc:
                raise BackendError(f"claude: result was not JSON: {exc}") from exc
        return body if isinstance(body, dict) else {}
