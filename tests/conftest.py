"""Shared fixtures: fake model CLIs on PATH.

No test in the default run may start a real model. Every backend test puts a
small Python script named `claude` or `codex` on PATH and asserts against
what the backend does with its output.
"""

import json
import stat
import sys
from pathlib import Path

import pytest

from reviewbot import github

VALID_RESULT = {
    "summary": "Adds a retry to the candles fetch. Contained and readable.",
    "findings": [
        {
            "file": "sdk/client.py",
            "line_start": 42,
            "line_end": None,
            "category": "correctness",
            "severity": "should_fix",
            "confidence": 0.7,
            "title": "Retry loop never sleeps",
            "body": "The backoff is computed and discarded.",
            "evidence": "sdk/client.py:43",
        }
    ],
    "proof": {"status": "sufficient", "ask": ""},
    "verdict": {"value": "needs_changes", "reason": "One should-fix finding."},
    "rating": {"patch": 4, "proof": 5},
    "praise": [],
}


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    """The only directory on PATH, so no real CLI can be reached.

    PATH is replaced, not prepended. A real `claude` and `codex` are installed
    on the development machines and on the self-hosted runner; prepending
    would leave `probe()` finding them, and the "not installed" tests would
    pass against a live binary.
    """
    path = tmp_path / "bin"
    path.mkdir()
    monkeypatch.setenv("PATH", str(path))
    return path


def write_script(bin_dir: Path, name: str, body: str) -> Path:
    """Put an executable Python script on PATH under `name`.

    The shebang names this interpreter by absolute path, because PATH holds
    nothing but the fake CLI directory.
    """
    script = bin_dir / name
    script.write_text(f"#!{sys.executable}\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def claude_script(payload, exit_code=0, echo_args=None, sleep=0.0):
    """A fake `claude`: reads the brief on stdin, prints one claude -p JSON envelope.

    `payload` is JSON TEXT, embedded as a Python string literal and parsed at
    run time. Interpolating it as Python source would break on `null`.
    """
    return f"""
import json, sys, time
brief = sys.stdin.read()
time.sleep({sleep!r})
if {echo_args!r}:
    open({echo_args!r}, "w").write(json.dumps({{"argv": sys.argv[1:], "brief": brief}}))
print(json.dumps({{"type": "result", "is_error": False, "total_cost_usd": 0.01,
                  "structured_output": json.loads({payload!r})}}))
sys.exit({exit_code})
"""


def codex_script(payload, exit_code=0, echo_args=None, sleep=0.0):
    """A fake `codex`: writes the result to the path after -o, prints JSONL to stdout.

    `payload` is JSON TEXT and is written to the output file verbatim.
    """
    return f"""
import json, sys, time
brief = sys.stdin.read()
time.sleep({sleep!r})
argv = sys.argv[1:]
if {echo_args!r}:
    open({echo_args!r}, "w").write(json.dumps({{"argv": argv, "brief": brief}}))
out = argv[argv.index("-o") + 1] if "-o" in argv else None
if out:
    open(out, "w").write({payload!r})
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 10}}}}))
sys.exit({exit_code})
"""


class FakeTransport:
    """Answers by (method, path); records every call it is given."""

    def __init__(self):
        self.routes, self.calls = {}, []

    def add(self, method, path, status=200, data=None, text=""):
        self.routes.setdefault((method, path), []).append(
            github.Response(status=status, data=data, text=text)
        )

    def __call__(self, method, url, headers, body):
        path = url.replace("https://api.github.com", "")
        self.calls.append(
            {"method": method, "path": path, "body": json.loads(body) if body else None}
        )
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"no fake route for {method} {path}")
        return queue.pop(0) if len(queue) > 1 else queue[0]
