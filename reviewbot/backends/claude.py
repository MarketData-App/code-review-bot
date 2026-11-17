"""The Claude backend: `claude -p` on the subscription OAuth token.

No API key and no SDK, the same way support-agent and daily-standup run.
"""

import json
import os
import shutil

from reviewbot.backends.base import Backend, BackendError
from reviewbot.redact import scrub
from reviewbot.result import load_schema

# The fields of a `claude -p --output-format json` envelope that name why a
# turn failed, in the order the log prints them. Claude Code 2.1 writes the
# usage, the cost and the per-model usage FIRST and these last, so cutting the
# envelope line by length kept only numbers: api run 44023357767 logged 300
# characters of token counts and no cause.
CAUSE_FIELDS = (
    "subtype",
    "is_error",
    "terminal_reason",
    "api_error_status",
    "api_error_code",
    "api_error_origin",
    "api_error",
    "errors",
    "result",
    "num_turns",
    "stop_reason",
)
CAUSE_VALUE_CHARS = 600


def _envelope(stdout: str | None) -> dict | None:
    """The result envelope on stdout, or None when stdout holds none.

    `--output-format json` prints one line. The last line that parses as a
    result object is taken, so a stray line before it does not hide it.
    """
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and (data.get("type") == "result" or "subtype" in data):
            return data
    return None


def _cause(envelope: dict) -> str:
    """The fields of a failed envelope that say why, each one cut and scrubbed.

    The result text of an API error echoes what the API said, which can quote
    the request, so it goes through the same redaction as every other line
    the bot prints.
    """
    parts = []
    for key in CAUSE_FIELDS:
        value = envelope.get(key)
        if value is None or value == "" or value == []:
            continue
        rendered = json.dumps(value, ensure_ascii=False, default=str)
        if len(rendered) > CAUSE_VALUE_CHARS:
            rendered = rendered[:CAUSE_VALUE_CHARS] + "..."
        parts.append(f'"{key}": {rendered}')
    if not parts:
        return "the envelope names no cause"
    return scrub(", ".join(parts))


# The only tools the reviewer gets. The brief cannot widen this; the list
# lives here, in the engine, next to the sandbox it belongs to.
READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
DENIED_TOOLS = ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Task"]


class ClaudeBackend(Backend):
    name = "claude"

    def probe(self) -> bool:
        """Installed and authenticated, by token or by the credential on disk.

        The CLI needs no environment variable: measured 2026-09-16, `claude -p`
        answers with CLAUDE_CODE_OAUTH_TOKEN unset, reading
        ~/.claude/.credentials.json. Demanding the variable reported a working
        CLI as "not installed", which is why the self-hosted runner could not
        use the subscription already sitting on it.
        """
        if not shutil.which("claude"):
            return False
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return True
        config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude"
        )
        return os.path.exists(os.path.join(config, ".credentials.json"))

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
            envelope = _envelope(proc.stdout)
            if envelope is not None:
                raise BackendError(f"claude: exit {proc.returncode}: {_cause(envelope)}")
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
            raise BackendError(f"claude: exit {proc.returncode}: {scrub(tail[0][:300])}")
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise BackendError(f"claude: output was not JSON: {exc}") from exc
        if envelope.get("is_error"):
            raise BackendError(f"claude: {_cause(envelope)}")
        body = envelope.get("structured_output")
        if body is None:
            body = envelope.get("result")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError as exc:
                raise BackendError(f"claude: result was not JSON: {exc}") from exc
        return body if isinstance(body, dict) else {}
