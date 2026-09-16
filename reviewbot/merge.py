"""Merging two backends into one review.

Located findings merge deterministically: no model sees them. Unlocated
findings go to one short call on the primary backend, because there is nothing
to compare but the wording. When that call fails, the deterministic fallback
groups findings whose titles normalise to the same string, so `mode: all`
never fails for a merge.
"""

import copy

from reviewbot.backends.base import BackendResult
from reviewbot.findings import finding_id

VERDICT_ORDER = ("ready", "needs_changes", "blocked")
PROOF_ORDER = ("not_applicable", "sufficient", "insufficient", "missing")
SEVERITY_ORDER = ("nit", "should_fix", "blocking")

# Findings on the same line are the same issue when they are the same category
# and their line ranges are this close.
LINE_SLACK = 3

MERGE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                # Every key, because OpenAI's strict mode rejects a schema
                # whose `required` omits any property (measured: sdk-py run
                # 35125915397). `evidence` is nullable so "none" stays sayable.
                "required": [
                    "title",
                    "body",
                    "category",
                    "severity",
                    "confidence",
                    "evidence",
                    "sources",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "category": {"type": "string"},
                    "severity": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {"type": ["string", "null"]},
                    "sources": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0},
                        "minItems": 1,
                        "description": "Indexes of the input findings this one stands for.",
                    },
                },
            },
        }
    },
}

MERGE_PROMPT = """\
Two reviewers read the same pull request. Below are the findings neither tied
to a file, numbered from 0.

Group the duplicates. Keep the best wording of each group, word for word from
one of the inputs. Change nothing else: do not add findings, do not soften
them, do not re-judge them. Every input index appears in exactly one group's
`sources`.

Return one JSON object, nothing else.

"""


def weaker_verdict(a: str, b: str) -> str:
    return max(a, b, key=VERDICT_ORDER.index)


def weaker_proof(a: str, b: str) -> str:
    return max(a, b, key=PROOF_ORDER.index)


def _weaker_severity(a: str, b: str) -> str:
    return max(a, b, key=SEVERITY_ORDER.index)


def _normalise(title: str) -> str:
    return " ".join((title or "").split()).lower()


def _located(finding: dict) -> bool:
    return bool(finding.get("file")) and bool(finding.get("line_start"))


def _span(finding: dict) -> tuple[int, int]:
    start = int(finding["line_start"])
    end = int(finding.get("line_end") or start)
    return min(start, end), max(start, end)


def _same_issue(a: dict, b: dict) -> bool:
    if a["file"] != b["file"] or a["category"] != b["category"]:
        return False
    a_start, a_end = _span(a)
    b_start, b_end = _span(b)
    return max(a_start, b_start) - min(a_end, b_end) <= LINE_SLACK


def _combine(a: dict, b: dict) -> dict:
    """One finding from two. The higher confidence supplies the wording."""
    best, other = (a, b) if a["confidence"] >= b["confidence"] else (b, a)
    merged = copy.deepcopy(best)
    merged["severity"] = _weaker_severity(a["severity"], b["severity"])
    merged["confidence"] = max(a["confidence"], b["confidence"])
    if _located(a) and _located(b):
        starts = [_span(a)[0], _span(b)[0]]
        ends = [_span(a)[1], _span(b)[1]]
        merged["line_start"] = min(starts)
        merged["line_end"] = max(ends) if max(ends) != min(starts) else None
    merged["backends"] = sorted(set(a["backends"]) | set(b["backends"]))
    merged.setdefault("evidence", other.get("evidence", ""))
    return merged


def _tag(result: dict, backend: str) -> list[dict]:
    out = []
    for finding in result["findings"]:
        item = copy.deepcopy(finding)
        item["backends"] = [backend]
        out.append(item)
    return out


def _finish(findings: list[dict], order: list[str], solo: bool = False) -> list[dict]:
    """Stamp id and agreed, and put the backends in policy order.

    `solo` is a single-backend run: there is no second opinion to disagree
    with, so every finding counts as agreed and `require_agreement` is a
    no-op, exactly as it is for a repo that never runs `mode: all`.
    """
    out = []
    for finding in findings:
        item = copy.deepcopy(finding)
        names = item.get("backends") or []
        item["backends"] = sorted(set(names), key=lambda n: order.index(n) if n in order else 99)
        item["agreed"] = solo or len(item["backends"]) > 1
        item["id"] = finding_id(item)
        out.append(item)
    return out


def model_merger(backend):
    """A merger backed by one short call on the primary backend."""

    def merger(items: list[dict]) -> list[dict]:
        listing = "\n".join(
            f"{i}. [{f['category']}/{f['severity']}] {f['title']}: {f['body']}"
            for i, f in enumerate(items)
        )
        answer = backend.ask(MERGE_PROMPT + listing, MERGE_SCHEMA)
        out = []
        for group in answer["findings"]:
            sources = [items[i] for i in group["sources"] if 0 <= i < len(items)]
            if not sources:
                continue
            backends = sorted({name for f in sources for name in f["backends"]})
            out.append(
                {
                    "file": None,
                    "line_start": None,
                    "line_end": None,
                    "category": group["category"],
                    "severity": group["severity"],
                    "confidence": group["confidence"],
                    "title": group["title"],
                    "body": group["body"],
                    "evidence": group.get("evidence", ""),
                    "backends": backends,
                }
            )
        return out

    return merger


def _dedup_unlocated(items: list[dict]) -> list[dict]:
    """The fallback: group by normalised title and category."""
    groups: dict[tuple, dict] = {}
    for finding in items:
        key = (_normalise(finding["title"]), finding["category"])
        if key in groups:
            groups[key] = _combine(groups[key], finding)
        else:
            groups[key] = copy.deepcopy(finding)
    return list(groups.values())


def merge(results: list[BackendResult], policy: dict, unlocated_merger=None) -> dict:
    """One review from one or more backend results."""
    order = list(policy["backends"])
    results = sorted(results, key=lambda r: order.index(r.backend) if r.backend in order else 99)
    primary = results[0]

    if len(results) == 1:
        out = copy.deepcopy(primary.result)
        out["findings"] = _finish(_tag(primary.result, primary.backend), order, solo=True)
        return out

    tagged = [(r.backend, _tag(r.result, r.backend)) for r in results]

    # Located findings: deterministic, one pass, never across one backend's own list.
    merged_located: list[dict] = []
    for _, items in tagged:
        for finding in [f for f in items if _located(f)]:
            for index, existing in enumerate(merged_located):
                if set(existing["backends"]) & set(finding["backends"]):
                    continue
                if _same_issue(existing, finding):
                    merged_located[index] = _combine(existing, finding)
                    break
            else:
                merged_located.append(copy.deepcopy(finding))

    unlocated = [f for _, items in tagged for f in items if not _located(f)]
    if unlocated:
        merged_unlocated = None
        if unlocated_merger is not None:
            try:
                merged_unlocated = unlocated_merger(unlocated)
            except Exception:
                merged_unlocated = None
        if merged_unlocated is None:
            merged_unlocated = _dedup_unlocated(unlocated)
    else:
        merged_unlocated = []

    out = copy.deepcopy(primary.result)
    out["findings"] = _finish(merged_located + merged_unlocated, order)

    verdict = primary.result["verdict"]["value"]
    verdict_from = primary
    proof = primary.result["proof"]
    for other in results[1:]:
        weaker = weaker_verdict(verdict, other.result["verdict"]["value"])
        if weaker != verdict:
            verdict, verdict_from = weaker, other
        if weaker_proof(proof["status"], other.result["proof"]["status"]) != proof["status"]:
            proof = other.result["proof"]
    # The comment says which backend blocked, and why (spec section 4.1).
    out["verdict"] = {
        "value": verdict,
        "reason": f"{verdict_from.backend}: {verdict_from.result['verdict']['reason']}",
    }
    out["proof"] = copy.deepcopy(proof)
    out["rating"] = {
        "patch": min(r.result["rating"]["patch"] for r in results),
        "proof": min(r.result["rating"]["proof"] for r in results),
    }

    praise: list[str] = []
    for item in [p for r in results for p in r.result.get("praise", [])]:
        if item not in praise:
            praise.append(item)
    out["praise"] = praise

    # Always present, null when no backend raised one. The schema requires the
    # key since it went strict, and consumers read it with truthiness, so null
    # and absent mean the same thing to them but not to the validator.
    out["decision"] = None
    for candidate in results:
        if candidate.result.get("decision"):
            out["decision"] = copy.deepcopy(candidate.result["decision"])
            break
    return out
