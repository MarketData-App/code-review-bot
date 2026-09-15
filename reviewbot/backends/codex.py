"""The Codex backend: `codex exec` in its read-only sandbox.

The same flags the website's scripts/marketing/lib/llm-review.mjs uses, with
the result schema this repository ships.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from reviewbot.backends.base import Backend, BackendError
from reviewbot.result import SCHEMA_PATH


class CodexBackend(Backend):
    name = "codex"

    def probe(self) -> bool:
        """Installed and authenticated: an API key, or a login file under CODEX_HOME."""
        if not shutil.which("codex"):
            return False
        if os.environ.get("OPENAI_API_KEY"):
            return True
        home = os.environ.get("CODEX_HOME")
        return bool(home) and Path(home, "auth.json").exists()

    def _command(self, out_path: str, schema_path: str) -> list[str]:
        return [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "-s",
            "read-only",
            "-m",
            self.model,
            "-c",
            f'model_reasoning_effort="{self.policy["codex_reasoning_effort"]}"',
            "--json",
            "--output-schema",
            schema_path,
            "-o",
            out_path,
            "-",
        ]

    def _run(self, text: str, schema: dict | None = None) -> dict:
        with tempfile.TemporaryDirectory(prefix="reviewbot-codex-") as work:
            out_path = str(Path(work) / "result.json")
            if schema is None:
                schema_path = str(SCHEMA_PATH)
            else:
                schema_path = str(Path(work) / "schema.json")
                Path(schema_path).write_text(json.dumps(schema))
            proc = self._exec(self._command(out_path, schema_path), text)
            for line in (proc.stdout or "").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("type") == "error":
                    raise BackendError(f"codex: {str(event.get('message'))[:300]}")
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
                raise BackendError(f"codex: exit {proc.returncode}: {tail[0][:300]}")
            try:
                raw = Path(out_path).read_text()
            except OSError as exc:
                raise BackendError(f"codex: no output file was written ({exc})") from exc
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise BackendError(f"codex: output file was not JSON: {exc}") from exc
            return data if isinstance(data, dict) else {}
