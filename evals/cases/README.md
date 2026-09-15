# Eval cases

One JSON file per recorded pull request:

```json
{
  "repo": "openclaw/wacli",
  "number": 422,
  "facts": { "...": "the PRFacts as recorded by evals/record.py" },
  "known_findings": [
    {
      "file": "src/session.ts",
      "line": 118,
      "category": "correctness",
      "label": "session id reused after reconnect"
    }
  ]
}
```

`known_findings` is written by hand: the defects a human confirmed are real,
from the review the pull request actually received. `label` is a short name
for the defect and is what the scorer prints.

The first case is **openclaw/wacli#422**, which carried six confirmed
findings across two rounds of review. Record it with:

```bash
GITHUB_TOKEN=... uv run python evals/record.py openclaw/wacli 422
```

Then fill in the six entries and score a run:

```bash
uv run python evals/score.py evals/cases/*.json --results run.json
```

Recording touches the network, so it is never part of the test suite.
