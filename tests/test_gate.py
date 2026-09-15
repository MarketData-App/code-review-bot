"""Tests for `reviewbot gate`, the check that runs before any checkout.

The review job's `if:` expression is a cheap first filter. It cannot be unit
tested, it cannot see the pull request author on an issue_comment or a
workflow_dispatch event, and asserting substrings of it proves nothing. So the
decision itself lives here, in Python, and runs before the pull request head
is fetched onto the runner.

Run: pytest tests/test_gate.py
"""

import json

import pytest

from reviewbot import cli


class FakeGitHub:
    def __init__(self, pull, policy_text=None):
        self.pull = pull
        self.policy_text = policy_text
        self.calls = []

    def pull_request(self, number):
        self.calls.append(("pull_request", number))
        return self.pull

    def file_at_ref(self, path, ref):
        return self.policy_text

    def is_org_member(self, org, login):
        # The plain fake predates the membership lookup. Answering None means
        # "cannot tell", so these tests still exercise the association path.
        return None


def pull(login="alice", association="MEMBER"):
    return {
        "number": 7,
        "user": {"login": login},
        "author_association": association,
        "base": {"ref": "main"},
    }


def gate(api, event=None, trusted_env=""):
    return cli.gate(
        event=event or {"action": "synchronize", "pull_request": {"number": 7}},
        repo="MarketData-App/api",
        token="ghs_x",
        api=api,
        trusted_authors=trusted_env,
    )


def test_an_org_member_is_trusted():
    assert gate(FakeGitHub(pull(association="MEMBER")))["trusted"] is True


def test_the_owner_is_trusted():
    assert gate(FakeGitHub(pull(association="OWNER")))["trusted"] is True


def test_an_outside_contributor_is_refused():
    out = gate(FakeGitHub(pull(association="CONTRIBUTOR")))
    assert out["trusted"] is False
    assert "CONTRIBUTOR" in out["reason"]


def test_a_collaborator_is_refused_by_default():
    assert gate(FakeGitHub(pull(association="COLLABORATOR")))["trusted"] is False


def test_a_listed_bot_is_trusted():
    api = FakeGitHub(pull(login="sdk-sync[bot]", association="NONE"))
    assert gate(api, trusted_env="sdk-sync[bot]")["trusted"] is True


def test_the_list_tolerates_spaces():
    # The old YAML gate used a substring match and silently dropped every
    # entry after the first when the list had spaces.
    api = FakeGitHub(pull(login="other[bot]", association="NONE"))
    assert gate(api, trusted_env="sdk-sync[bot], other[bot]")["trusted"] is True


def test_an_unlisted_bot_is_refused():
    api = FakeGitHub(pull(login="stranger[bot]", association="NONE"))
    assert gate(api, trusted_env="sdk-sync[bot]")["trusted"] is False


# --- the triggers the YAML gate could not cover ----------------------------


def test_a_comment_on_an_outsiders_pull_request_is_refused():
    # An org member commenting on a fork PR must not start the job: the
    # pull request's own author decides, not the commenter.
    event = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "u"}},
        "comment": {"body": "@marketdata-code-review re-review", "author_association": "MEMBER"},
    }
    api = FakeGitHub(pull(login="mallory", association="CONTRIBUTOR"))
    out = gate(api, event=event)
    assert out["trusted"] is False


def test_a_comment_by_an_outsider_on_a_member_pull_request_is_refused():
    event = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "u"}},
        "comment": {
            "body": "@marketdata-code-review re-review",
            "author_association": "CONTRIBUTOR",
        },
    }
    api = FakeGitHub(pull(association="MEMBER"))
    out = gate(api, event=event)
    assert out["trusted"] is False
    assert out["undecided"] is False
    # The lookup happens first now, on purpose: the commenter is judged
    # against the repository's own policy, and reading that policy needs the
    # pull request's base ref. One API call is the price of the two gates
    # agreeing about who is trusted.
    assert api.calls == [("pull_request", 7)]


def test_a_comment_by_a_member_on_a_member_pull_request_is_trusted():
    event = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "u"}},
        "comment": {"body": "@marketdata-code-review re-review", "author_association": "MEMBER"},
    }
    assert gate(FakeGitHub(pull(association="MEMBER")), event=event)["trusted"] is True


def test_workflow_dispatch_still_checks_the_pull_request_author():
    # Dispatch needs write access, but an org member must not be able to aim
    # the bot at an outsider's fork.
    event = {"inputs": {"pr": "7"}}
    assert gate(FakeGitHub(pull(association="CONTRIBUTOR")), event=event)["trusted"] is False


def test_workflow_dispatch_on_a_member_pull_request_is_trusted():
    event = {"inputs": {"pr": "7"}}
    assert gate(FakeGitHub(pull(association="MEMBER")), event=event)["trusted"] is True


def test_the_repo_policy_can_widen_the_association_list():
    api = FakeGitHub(
        pull(association="COLLABORATOR"),
        policy_text="trusted_associations: [OWNER, MEMBER, COLLABORATOR]\n",
    )
    assert gate(api)["trusted"] is True


def test_an_event_with_no_pull_request_is_refused():
    out = gate(FakeGitHub(pull()), event={"action": "created", "issue": {"number": 7}})
    assert out["trusted"] is False


def test_the_main_command_writes_the_github_output(tmp_path, monkeypatch, capsys):
    out_file = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "MarketData-App/api")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "synchronize", "pull_request": {"number": 7}}))

    def fake_gate(**kwargs):
        return {"trusted": False, "reason": "not in the organisation"}

    monkeypatch.setattr(cli, "gate", fake_gate)
    assert cli.main(["gate", "--event", str(event)]) == 0
    assert "trusted=false" in out_file.read_text()


def test_the_main_command_reports_trusted(tmp_path, monkeypatch):
    out_file = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "MarketData-App/api")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "synchronize", "pull_request": {"number": 7}}))
    monkeypatch.setattr(cli, "gate", lambda **k: {"trusted": True, "reason": "MEMBER"})
    assert cli.main(["gate", "--event", str(event)]) == 0
    assert "trusted=true" in out_file.read_text()


@pytest.mark.parametrize("association", ["NONE", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", ""])
def test_every_other_association_is_refused(association):
    assert gate(FakeGitHub(pull(association=association)))["trusted"] is False


def test_the_reason_says_why_a_bot_was_admitted():
    api = FakeGitHub(pull(login="sdk-sync[bot]", association="NONE"))
    out = gate(api, trusted_env="sdk-sync[bot]")
    assert out["trusted"] is True
    assert "trusted-authors" in out["reason"]


def test_the_reason_names_the_association_for_a_member():
    assert "MEMBER" in gate(FakeGitHub(pull(association="MEMBER")))["reason"]


# --- findings from the second review ---------------------------------------


def test_a_waive_comment_still_starts_a_run():
    # Spec section 8 lists `waive` as a maintainer command. The gate must not
    # refuse the event that carries it.
    event = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "u"}},
        "comment": {
            "body": "@marketdata-code-review waive 1234abcd",
            "author_association": "MEMBER",
        },
    }
    assert gate(FakeGitHub(pull(association="MEMBER")), event=event)["trusted"] is True


def test_an_explicit_pr_number_is_authoritative():
    event = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "u"}},
        "comment": {"body": "unrelated chatter", "author_association": "MEMBER"},
    }
    api = FakeGitHub(pull(association="MEMBER"))
    assert cli.gate(event=event, repo="r", token="t", api=api, pr_number=7)["trusted"] is True


def test_the_commenter_check_honours_a_widened_repository_policy():
    # A repo that opts collaborators in must get that widening for commenters
    # too, or one of its collaborators can never ask for a re-review.
    event = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "u"}},
        "comment": {
            "body": "@marketdata-code-review re-review",
            "author_association": "COLLABORATOR",
        },
    }
    api = FakeGitHub(
        pull(association="COLLABORATOR"),
        policy_text="trusted_associations: [OWNER, MEMBER, COLLABORATOR]\n",
    )
    assert gate(api, event=event)["trusted"] is True


def test_a_listed_bot_may_ask_for_a_re_review():
    event = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "u"}},
        "comment": {
            "body": "@marketdata-code-review re-review",
            "author_association": "NONE",
            "user": {"login": "sdk-sync[bot]"},
        },
    }
    api = FakeGitHub(pull(login="sdk-sync[bot]", association="NONE"))
    assert gate(api, event=event, trusted_env="sdk-sync[bot]")["trusted"] is True


def test_an_api_failure_is_undecided_not_refused():
    # "Could not decide" must not look like "refused": a transient failure
    # would otherwise pass silently and the review would never happen.
    class Broken(FakeGitHub):
        def pull_request(self, number):
            from reviewbot.github import GitHubError

            raise GitHubError("503 upstream")

    out = gate(Broken(pull()))
    assert out["trusted"] is False
    assert out["undecided"] is True


def test_a_plain_refusal_is_decided():
    assert gate(FakeGitHub(pull(association="CONTRIBUTOR")))["undecided"] is False


def test_the_main_command_fails_the_step_when_undecided(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "MarketData-App/api")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "synchronize", "pull_request": {"number": 7}}))
    monkeypatch.setattr(
        cli, "gate", lambda **k: {"trusted": False, "undecided": True, "reason": "503"}
    )
    assert cli.main(["gate", "--event", str(event)]) == 1


def test_a_malformed_repository_policy_does_not_widen_the_gate():
    api = FakeGitHub(
        pull(association="COLLABORATOR"), policy_text="trusted_associations: [OWNER, NONE]\n"
    )
    out = gate(api)
    assert out["trusted"] is False
    assert out["undecided"] is False


def test_a_malformed_policy_still_admits_a_real_member():
    api = FakeGitHub(pull(association="MEMBER"), policy_text="mode: sometimes\n")
    assert gate(api)["trusted"] is True


def test_an_unreadable_policy_is_undecided_not_refused():
    from reviewbot.github import GitHubError

    class Broken(FakeGitHub):
        def file_at_ref(self, path, ref):
            raise GitHubError("GET contents failed with 500")

    out = gate(Broken(pull(association="MEMBER")))
    assert out["undecided"] is True


def test_the_policy_is_read_from_the_pull_requests_base_branch():
    seen = {}

    class Recording(FakeGitHub):
        def file_at_ref(self, path, ref):
            seen["path"], seen["ref"] = path, ref
            return None

    api = Recording(pull(association="MEMBER"))
    api.pull["base"]["ref"] = "release/2.0"
    gate(api)
    assert seen["ref"] == "release/2.0"
    assert seen["path"] == ".github/code-review/policy.yml"


def test_a_pull_request_with_no_base_ref_falls_back_to_head():
    class Recording(FakeGitHub):
        def file_at_ref(self, path, ref):
            self.ref = ref
            return None

    api = Recording({"number": 7, "user": {"login": "a"}, "author_association": "MEMBER"})
    gate(api)
    assert api.ref == "HEAD"


# --- organisation membership is the real trust model -----------------------
#
# author_association describes a person's relationship to one REPOSITORY.
# Trust is a property of the ORGANISATION, and the two differ exactly where it
# matters: on the MarketDataApp user-account repos, an org member reads as
# COLLABORATOR, and a stranger invited to one repo reads as COLLABORATOR too.


class OrgGitHub(FakeGitHub):
    UNSET = object()

    def __init__(self, pull, members=(), policy_text=None, membership=UNSET):
        super().__init__(pull, policy_text)
        self.members = set(members)
        # `membership` overrides the member list. It has to distinguish "not
        # given" from the meaningful answer None, which is "cannot tell".
        self.membership = membership
        self.member_calls = []

    def is_org_member(self, org, login):
        self.member_calls.append((org, login))
        if self.membership is not OrgGitHub.UNSET:
            return self.membership
        return login in self.members


def test_an_org_member_is_trusted_on_a_user_account_repo():
    # COLLABORATOR association, which the old default refused, but the person
    # is in the organisation. This is the sdk-* case.
    api = OrgGitHub(
        pull(login="MarketDataDev01", association="COLLABORATOR"), members=["MarketDataDev01"]
    )
    out = gate(api)
    assert out["trusted"] is True
    assert "organisation" in out["reason"]


def test_a_non_member_collaborator_is_refused():
    # Invited to the repository, not in the organisation. Refused.
    api = OrgGitHub(pull(login="outsider", association="COLLABORATOR"), members=[])
    out = gate(api)
    assert out["trusted"] is False
    assert out["undecided"] is False


def test_a_non_member_is_refused_even_as_repository_owner():
    api = OrgGitHub(pull(login="someone-else", association="OWNER"), members=[])
    assert gate(api)["trusted"] is False


def test_membership_is_checked_against_the_configured_organisation():
    api = OrgGitHub(pull(login="MarketDataDev01", association="NONE"), members=["MarketDataDev01"])
    gate(api)
    assert api.member_calls == [("MarketData-App", "MarketDataDev01")]


def test_a_listed_bot_needs_no_membership_lookup():
    api = OrgGitHub(pull(login="sdk-sync[bot]", association="NONE"), members=[])
    assert gate(api, trusted_env="sdk-sync[bot]")["trusted"] is True
    assert api.member_calls == []


def test_an_unknown_membership_falls_back_to_the_association():
    # Until the App is granted Organization members:read the lookup 403s.
    # Behaviour must degrade to what it was, not refuse the whole org.
    api = OrgGitHub(pull(login="someone", association="MEMBER"), membership=None)
    out = gate(api)
    assert out["trusted"] is True
    assert "association" in out["reason"]


def test_an_unknown_membership_still_refuses_an_outsider():
    api = OrgGitHub(pull(login="mallory", association="CONTRIBUTOR"), membership=None)
    assert gate(api)["trusted"] is False


def test_the_commenter_must_also_be_an_org_member():
    event = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "u"}},
        "comment": {
            "body": "@marketdata-code-review re-review",
            "author_association": "COLLABORATOR",
            "user": {"login": "outsider"},
        },
    }
    api = OrgGitHub(
        pull(login="MarketDataDev01", association="COLLABORATOR"), members=["MarketDataDev01"]
    )
    assert gate(api, event=event)["trusted"] is False


def test_an_org_member_commenter_is_accepted():
    event = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "u"}},
        "comment": {
            "body": "@marketdata-code-review re-review",
            "author_association": "COLLABORATOR",
            "user": {"login": "MarketDataDev02"},
        },
    }
    api = OrgGitHub(
        pull(login="MarketDataDev01", association="COLLABORATOR"),
        members=["MarketDataDev01", "MarketDataDev02"],
    )
    assert gate(api, event=event)["trusted"] is True


# --- the organisation token ------------------------------------------------
#
# Measured 2026-09-15 with both installations of app 4955329, asking
# GET /orgs/MarketData-App/members/MarketDataDev02:
#
#   MarketData-App (org)  installation -> 204  MEMBER
#   MarketDataApp  (user) installation -> 404  not a member
#
# 404, not 403. A user-account installation has no `members` permission at
# all, and GitHub answers as though the person were a stranger. Treating that
# as definitive refuses every org member on every sdk-* repository, which is
# where most review volume is. So the membership question is asked with a
# separate, organisation-scoped token.


class TwoTokenGitHub(FakeGitHub):
    """Repo calls answer from one client, membership from another."""

    def __init__(self, pull, members=(), policy_text=None):
        super().__init__(pull, policy_text)
        self.members = set(members)
        self.asked_with = None

    def is_org_member(self, org, login):
        self.asked_with = "repo-token"
        return False  # what a user-account installation answers: 404


class OrgTokenGitHub:
    def __init__(self, members, owner):
        self.members = set(members)
        self.owner = owner

    def is_org_member(self, org, login):
        self.owner.asked_with = "org-token"
        return login in self.members


def test_the_membership_question_uses_the_org_client_when_given():
    api = TwoTokenGitHub(pull(login="MarketDataDev02", association="COLLABORATOR"))
    org_api = OrgTokenGitHub({"MarketDataDev02"}, api)
    out = cli.gate(
        event={"action": "synchronize", "pull_request": {"number": 7}},
        repo="MarketDataApp/sdk-py",
        token="t",
        api=api,
        org_api=org_api,
    )
    assert out["trusted"] is True
    assert api.asked_with == "org-token"


def test_without_an_org_client_the_repo_token_is_used():
    api = TwoTokenGitHub(pull(login="MarketDataDev02", association="COLLABORATOR"))
    out = cli.gate(
        event={"action": "synchronize", "pull_request": {"number": 7}},
        repo="MarketDataApp/sdk-py",
        token="t",
        api=api,
    )
    assert out["trusted"] is False
    assert api.asked_with == "repo-token"


def test_the_org_client_decides_a_refusal_too():
    api = TwoTokenGitHub(pull(login="stranger", association="COLLABORATOR"))
    org_api = OrgTokenGitHub({"MarketDataDev02"}, api)
    out = cli.gate(
        event={"action": "synchronize", "pull_request": {"number": 7}},
        repo="MarketDataApp/sdk-py",
        token="t",
        api=api,
        org_api=org_api,
    )
    assert out["trusted"] is False
    assert out["undecided"] is False
