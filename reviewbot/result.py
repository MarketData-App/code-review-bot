"""The fixed result schema: loading, validation and the engine's rating rule.

Every backend answers against schema/result.json, so nothing downstream of
this module knows which model produced a review.
"""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).parent / "schema" / "result.json"

# The tier meanings. They are part of the schema's semantics, not part of any
# repository's review instructions: a repo's REVIEW.md replaces the default in
# full, so a repo with its own file would otherwise rate against nothing. The
# engine frame emits these, and the comment's rating row reads the same words.
PATCH_TIERS = (
    "harmful",  # 1: merging it breaks something that works today
    "wrong",  # 2: it does not do what it claims
    "incomplete",  # 3: the idea is right, parts are missing
    "works with reservations",  # 4: correct, at a design or clarity cost
    "solid",  # 5: correct, tested, documented, in style
    "exemplary",  # 6: solid, and it leaves the code clearer
)

PROOF_TIERS = (
    "none",  # 1: a behaviour change with nothing shown
    "claimed",  # 2: asserted to work, with nothing to look at
    "partial",  # 3: evidence covers part of the change
    "adequate",  # 4: evidence covers the change as described
    "reproducible",  # 5: anyone can re-run it and see the same thing
    "comprehensive",  # 6: the change and its failure modes
)

_schema_cache: dict | None = None


class ResultError(ValueError):
    """A backend answer was not valid JSON, or did not match the schema."""


def load_schema() -> dict:
    """Return the result schema, read once and cached."""
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = json.loads(SCHEMA_PATH.read_text())
    return _schema_cache


def validate(data: dict) -> list[str]:
    """Return human-readable schema errors for `data`; empty when it is valid.

    The messages go back to the model on the retry, so they name the path.
    """
    validator = Draft202012Validator(load_schema())
    errors = []
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"{where}: {error.message}")
    return errors


def parse(text: str) -> dict:
    """Parse and validate a backend answer, or raise ResultError."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResultError(f"answer was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ResultError("answer was not a JSON object")
    errors = validate(data)
    if errors:
        raise ResultError("answer did not match the schema: " + "; ".join(errors))
    return data


def overall_rating(rating: dict) -> int:
    """The overall tier is the weaker of the two. The engine computes it, never the model."""
    return min(int(rating["patch"]), int(rating["proof"]))


def tier_word(kind: str, tier: int) -> str:
    """The word for one tier. `kind` is "patch" or "proof"; tiers are 1 to 6."""
    table = PATCH_TIERS if kind == "patch" else PROOF_TIERS
    return table[max(1, min(6, int(tier))) - 1]
