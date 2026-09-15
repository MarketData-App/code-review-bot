"""Finding identity, so a re-review can report resolved / still open / new.

The id is a short hash of file, category and title. It deliberately ignores
the line number, the severity and the body: an author who edits the lines
above a defect, or a model that rewords the same defect, must not turn one
open finding into a resolved one plus a new one.
"""

import copy
import hashlib

SEVERITY_ORDER = ("blocking", "should_fix", "nit")


def finding_id(finding: dict) -> str:
    """A stable 8-character id for one finding."""
    title = " ".join(str(finding.get("title") or "").split()).lower()
    parts = [
        str(finding.get("file") or ""),
        str(finding.get("category") or ""),
        title,
    ]
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:8]


def with_ids(findings: list[dict]) -> list[dict]:
    """Copies of `findings`, each carrying its id."""
    out = []
    for finding in findings:
        item = copy.deepcopy(finding)
        item["id"] = finding_id(finding)
        out.append(item)
    return out


def since_last_review(previous_ids: list[str], current: list[dict]) -> dict:
    """Split the current findings against the ids the last review recorded."""
    previous = list(dict.fromkeys(previous_ids or []))
    current_ids = {item["id"] for item in current}
    return {
        "resolved": [pid for pid in previous if pid not in current_ids],
        "still_open": [item for item in current if item["id"] in previous],
        "new": [item for item in current if item["id"] not in previous],
    }


def drop_waived(findings: list[dict], waived_ids: list[str]) -> list[dict]:
    """Findings a maintainer has waived are removed before any decision."""
    waived = set(waived_ids or [])
    return [item for item in findings if item.get("id") not in waived]


def by_severity(findings: list[dict]) -> dict[str, list[dict]]:
    """Group findings by severity, in blocking-first order, skipping empty groups."""
    grouped = {}
    for severity in SEVERITY_ORDER:
        items = [x for x in findings if x.get("severity") == severity]
        if items:
            grouped[severity] = items
    return grouped
