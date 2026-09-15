"""Record one real pull request as an eval case.

This reaches GitHub, so it is run by hand, never from a test.

    GITHUB_TOKEN=... uv run python evals/record.py openclaw/wacli 422

It writes evals/cases/<owner>-<repo>-<number>.json with the PR facts and an
empty known_findings list. Fill that list in by hand from the review the
pull request actually received.
"""

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewbot.github import GitHub  # noqa: E402

CASES = Path(__file__).resolve().parent / "cases"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="evals/record.py")
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("number", type=int)
    parser.add_argument("--max-diff-kb", type=int, default=400)
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GITHUB_TOKEN is not set")

    api = GitHub(args.repo, token)
    facts = api.gather(args.number, args.max_diff_kb)
    payload = dataclasses.asdict(facts)
    # The bot's own state from another repository means nothing here.
    payload["previous_comment"] = None
    payload["previous_state"] = {}
    payload["comments_since"] = []
    case = {
        "repo": args.repo,
        "number": args.number,
        "facts": payload,
        "known_findings": [],
    }
    CASES.mkdir(exist_ok=True)
    name = f"{args.repo.replace('/', '-')}-{args.number}.json"
    (CASES / name).write_text(json.dumps(case, indent=2) + "\n")
    print(f"wrote evals/cases/{name}; fill in known_findings by hand")
    return 0


if __name__ == "__main__":
    sys.exit(main())
