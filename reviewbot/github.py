"""Every GitHub call the bot makes.

This is the only module that holds the token. It goes into one header and
nowhere else: not into the brief, not into a log line, not into the comment.
The transport is injectable so the tests never open a socket.
"""

import json
import time
import urllib.parse
from dataclasses import dataclass

import requests

from reviewbot import markers
from reviewbot.facts import PRFacts, cap_diff

API = "https://api.github.com"
ANNOTATION_LIMIT = 50
RETRY_STATUSES = (403, 429, 500, 502, 503, 504)
MAX_ATTEMPTS = 5

# GitHub's own words for "this account can act on the repository".
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

_ENABLE_AUTO_MERGE = """
mutation($pr: ID!, $method: PullRequestMergeMethod!) {
  enablePullRequestAutoMerge(input: {pullRequestId: $pr, mergeMethod: $method}) {
    clientMutationId
  }
}
"""

_DISABLE_AUTO_MERGE = """
mutation($pr: ID!) {
  disablePullRequestAutoMerge(input: {pullRequestId: $pr}) { clientMutationId }
}
"""


class GitHubError(RuntimeError):
    """A GitHub call failed after its retries."""


@dataclass(frozen=True)
class Response:
    status: int
    data: object = None
    text: str = ""


def requests_transport(method: str, url: str, headers: dict, body: bytes | None) -> Response:
    """The real transport. Kept tiny so the tests can replace it with a function."""
    reply = requests.request(method, url, headers=headers, data=body, timeout=30)
    data = None
    if reply.headers.get("Content-Type", "").startswith("application/json"):
        try:
            data = reply.json()
        except ValueError:
            data = None
    return Response(status=reply.status_code, data=data, text=reply.text)


class GitHub:
    """The bot's whole view of GitHub."""

    def __init__(self, repo: str, token: str, transport=None, sleep=time.sleep):
        self.repo = repo
        self._token = token
        self._transport = transport or requests_transport
        self._sleep = sleep

    # --- plumbing ---------------------------------------------------------

    def _scrub(self, text: str) -> str:
        return (text or "").replace(self._token, "[redacted]")

    def _request(self, method: str, path: str, body=None, accept=None) -> Response:
        """One call, retried with backoff on the statuses GitHub throttles with."""
        url = path if path.startswith("http") else API + path
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "marketdata-code-review",
        }
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            reply = self._transport(method, url, headers, payload)
            if reply.status < 400:
                return reply
            last = reply
            if reply.status not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                break
            self._sleep(min(2**attempt, 30))
        raise GitHubError(
            f"{method} {path} failed with {last.status}: {self._scrub(last.text)[:300]}"
        )

    def _paged(self, path: str) -> list:
        """Follow pages until one comes back short."""
        out, page = [], 1
        while True:
            joiner = "&" if "?" in path else "?"
            reply = self._request("GET", f"{path}{joiner}per_page=100&page={page}")
            items = reply.data or []
            out.extend(items)
            if len(items) < 100:
                return out
            page += 1

    # --- reads ------------------------------------------------------------

    def pull_request(self, number: int) -> dict:
        return self._request("GET", f"/repos/{self.repo}/pulls/{number}").data

    def changed_files(self, number: int) -> list[dict]:
        return [
            {
                "path": item["filename"],
                "status": item["status"],
                "additions": item.get("additions", 0),
                "deletions": item.get("deletions", 0),
            }
            for item in self._paged(f"/repos/{self.repo}/pulls/{number}/files")
        ]

    def diff(self, number: int) -> str:
        reply = self._request(
            "GET",
            f"/repos/{self.repo}/pulls/{number}",
            accept="application/vnd.github.v3.diff",
        )
        return reply.text or ""

    def issue_comments(self, number: int) -> list[dict]:
        return self._paged(f"/repos/{self.repo}/issues/{number}/comments")

    def file_at_ref(self, path: str, ref: str) -> str | None:
        """A file's text at a ref, or None when it is not there.

        Policy and review rules are read from the base branch, so a pull
        request cannot rewrite the rules it is judged by.
        """
        import base64

        quoted = urllib.parse.quote(path)
        try:
            reply = self._request(
                "GET", f"/repos/{self.repo}/contents/{quoted}?ref={urllib.parse.quote(ref)}"
            )
        except GitHubError as exc:
            if " 404" in str(exc):
                return None
            raise
        data = reply.data or {}
        if data.get("encoding") != "base64" or "content" not in data:
            return None
        return base64.b64decode(data["content"]).decode("utf-8")

    def repository(self) -> dict:
        """The repository object. Read for `allow_auto_merge` before arming."""
        return self._request("GET", f"/repos/{self.repo}").data or {}

    def ci_state(self, sha: str, exclude_check_name: str) -> str:
        """`success`, `failure`, `pending` or `none` for everything but our own check."""
        runs = (
            self._request("GET", f"/repos/{self.repo}/commits/{sha}/check-runs?per_page=100").data
            or {}
        ).get("check_runs", [])
        runs = [r for r in runs if r.get("name") != exclude_check_name]
        legacy = (self._request("GET", f"/repos/{self.repo}/commits/{sha}/status").data or {}).get(
            "state", "pending"
        )

        failed = any(
            r.get("conclusion") in ("failure", "timed_out", "action_required") for r in runs
        )
        running = any(r.get("status") != "completed" for r in runs)
        if failed or legacy == "failure":
            return "failure"
        if running:
            return "pending"
        if not runs:
            # `pending` with no check runs means nothing has reported at all.
            return "none" if legacy == "pending" else legacy
        return "pending" if legacy == "pending" else "success"

    # --- writes -----------------------------------------------------------

    def upsert_review_comment(self, number: int, body: str) -> dict:
        """One comment per pull request: edit the bot's own, or post the first."""
        for comment in self.issue_comments(number):
            if markers.is_bot_comment(comment.get("body")):
                return self._request(
                    "PATCH",
                    f"/repos/{self.repo}/issues/comments/{comment['id']}",
                    body={"body": body},
                ).data
        return self._request(
            "POST", f"/repos/{self.repo}/issues/{number}/comments", body={"body": body}
        ).data

    def write_check_run(
        self,
        sha: str,
        name: str,
        conclusion: str,
        title: str,
        summary: str,
        annotations: list[dict],
    ) -> dict:
        """The check run on the head commit, with located findings as annotations."""
        first = annotations[:ANNOTATION_LIMIT]
        payload = {
            "name": name,
            "head_sha": sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": summary, "annotations": first},
        }
        created = self._request("POST", f"/repos/{self.repo}/check-runs", body=payload).data
        rest = annotations[ANNOTATION_LIMIT:]
        while rest:
            self._request(
                "PATCH",
                f"/repos/{self.repo}/check-runs/{created['id']}",
                body={
                    "output": {
                        "title": title,
                        "summary": summary,
                        "annotations": rest[:ANNOTATION_LIMIT],
                    }
                },
            )
            rest = rest[ANNOTATION_LIMIT:]
        return created

    def apply_labels(
        self, number: int, add: list[str], remove: list[str], current: list[str]
    ) -> list[str]:
        """Add what is missing, remove only what is actually there."""
        missing = [label for label in add if label not in current]
        if missing:
            self._request(
                "POST", f"/repos/{self.repo}/issues/{number}/labels", body={"labels": missing}
            )
        for label in remove:
            if label in current and label not in add:
                quoted = urllib.parse.quote(label)
                self._request("DELETE", f"/repos/{self.repo}/issues/{number}/labels/{quoted}")
        return sorted(set(current) - set(remove) | set(add))

    def approve(self, number: int, body: str) -> dict:
        return self._request(
            "POST",
            f"/repos/{self.repo}/pulls/{number}/reviews",
            body={"event": "APPROVE", "body": body},
        ).data

    def _graphql(self, query: str, variables: dict) -> dict:
        reply = self._request("POST", "/graphql", body={"query": query, "variables": variables})
        data = reply.data or {}
        if data.get("errors"):
            messages = "; ".join(e.get("message", "") for e in data["errors"])
            raise GitHubError(f"GraphQL failed: {self._scrub(messages)[:300]}")
        return data

    def set_auto_merge(self, node_id: str, method: str) -> None:
        """Arm GitHub's own auto-merge. The bot never calls the merge endpoint."""
        self._graphql(_ENABLE_AUTO_MERGE, {"pr": node_id, "method": method.upper()})

    def clear_auto_merge(self, node_id: str) -> None:
        self._graphql(_DISABLE_AUTO_MERGE, {"pr": node_id})

    # --- the whole picture ------------------------------------------------

    def gather(self, number: int, max_diff_kb: int, check_name: str = "Code review") -> PRFacts:
        """Everything the engine needs about one pull request."""
        pull = self.pull_request(number)
        files = self.changed_files(number)
        diff, unseen = cap_diff(self.diff(number), max_diff_kb)
        comments = self.issue_comments(number)

        previous = None
        for comment in comments:
            if markers.is_bot_comment(comment.get("body")):
                previous = comment
        state = markers.parse(previous.get("body") if previous else None)

        cutoff = previous.get("updated_at") if previous else None
        since = [
            {
                "author": (c.get("user") or {}).get("login", ""),
                "body": c.get("body") or "",
                "is_maintainer": c.get("author_association") in MAINTAINER_ASSOCIATIONS,
            }
            for c in comments
            if not markers.is_bot_comment(c.get("body"))
            and (cutoff is None or c.get("created_at", "") > cutoff)
        ]

        user = pull.get("user") or {}
        return PRFacts(
            number=number,
            title=pull.get("title") or "",
            body=pull.get("body") or "",
            author=user.get("login", ""),
            author_is_bot=user.get("type") == "Bot",
            draft=bool(pull.get("draft")),
            labels=[label["name"] for label in pull.get("labels") or []],
            head_sha=(pull.get("head") or {}).get("sha", ""),
            base_ref=(pull.get("base") or {}).get("ref", ""),
            node_id=pull.get("node_id", ""),
            changed_files=files,
            diff=diff,
            unseen_files=unseen,
            ci_state=self.ci_state((pull.get("head") or {}).get("sha", ""), check_name),
            previous_comment=previous,
            previous_state=state,
            comments_since=since,
        )
