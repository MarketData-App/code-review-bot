"""Tests for `reviewbot lease-report`.

The store's `codex/lease.json` carries one commit per borrow and one per
return, so its history is the only durable record of who held the shared Codex
credential and for how long. Nothing read it until this command.

What the history CANNOT show is a run that waited: a blocked job writes no
commit. The report says so rather than implying a contention figure it cannot
compute.

Run: pytest tests/test_lease_report.py
"""

import datetime as dt

import pytest

from reviewbot import credentials
from tests.conftest import FakeTransport

API = "MarketData-App/code-review-credentials"


def commit(message: str, when: str) -> dict:
    """One entry in the shape `GET /repos/{repo}/commits` returns."""
    return {"sha": "0" * 40, "commit": {"message": message, "committer": {"date": when}}}


def taken(holder: str, when: str) -> dict:
    return commit(f"lease taken by {holder}", when)


def freed(holder: str, when: str) -> dict:
    return commit(f"lease freed by {holder}", when)


# --- parsing ---------------------------------------------------------------


def test_a_taken_and_its_freed_become_one_span():
    history = [
        freed("MarketData-App/api#462#35665153212-1", "2026-09-21T22:57:32Z"),
        taken("MarketData-App/api#462#35665153212-1", "2026-09-21T22:56:15Z"),
    ]
    spans = credentials.lease_spans(history)
    assert len(spans) == 1
    assert spans[0].repo == "MarketData-App/api"
    assert spans[0].pr == 462
    assert spans[0].run == "35665153212-1"
    assert spans[0].seconds == 77
    assert spans[0].closed is True


def test_the_history_is_read_oldest_first_whatever_order_it_arrives_in():
    # The API returns newest first. A naive walk would pair nothing.
    newest_first = [
        freed("o/r#2#b-1", "2026-09-21T10:05:00Z"),
        taken("o/r#2#b-1", "2026-09-21T10:04:00Z"),
        freed("o/r#1#a-1", "2026-09-21T10:02:00Z"),
        taken("o/r#1#a-1", "2026-09-21T10:00:00Z"),
    ]
    spans = credentials.lease_spans(newest_first)
    assert [s.pr for s in spans] == [1, 2]
    assert [s.seconds for s in spans] == [120, 60]


def test_a_holder_that_never_freed_is_reported_unclosed_not_guessed():
    # The TTL frees it, but the history does not say when the job stopped
    # working. Inventing a duration would be the one number a reader trusts.
    history = [
        freed("o/r#2#b-1", "2026-09-21T10:30:00Z"),
        taken("o/r#2#b-1", "2026-09-21T10:29:00Z"),
        taken("o/r#1#a-1", "2026-09-21T10:00:00Z"),
    ]
    spans = credentials.lease_spans(history)
    dead = [s for s in spans if s.run == "a-1"][0]
    assert dead.closed is False
    assert dead.seconds == 0


def test_a_free_with_no_matching_take_is_ignored():
    # The window's first commit can be a `freed` whose `taken` predates it.
    history = [freed("o/r#1#a-1", "2026-09-21T10:00:00Z")]
    assert credentials.lease_spans(history) == []


def test_an_unreadable_message_is_skipped_not_fatal():
    history = [
        commit("initial commit", "2026-09-20T09:00:00Z"),
        commit("lease taken by", "2026-09-20T09:01:00Z"),
        freed("o/r#1#a-1", "2026-09-21T10:01:00Z"),
        taken("o/r#1#a-1", "2026-09-21T10:00:00Z"),
    ]
    spans = credentials.lease_spans(history)
    assert [s.run for s in spans] == ["a-1"]


def test_a_holder_without_a_pull_request_number_still_parses():
    # `holder` is built by the workflow. A shape change must not crash a report.
    history = [
        freed("o/r#main#a-1", "2026-09-21T10:01:00Z"),
        taken("o/r#main#a-1", "2026-09-21T10:00:00Z"),
    ]
    spans = credentials.lease_spans(history)
    assert spans[0].repo == "o/r"
    assert spans[0].pr is None


# --- overlap ---------------------------------------------------------------


def test_two_holders_at_once_are_counted_as_an_overlap():
    # The lease's whole job. An overlap means a TTL expired under a job that
    # was still working, which the borrowed credential survives but which a
    # reader should see.
    history = [
        freed("o/r#2#b-1", "2026-09-21T10:20:00Z"),
        taken("o/r#2#b-1", "2026-09-21T10:05:00Z"),
        freed("o/r#1#a-1", "2026-09-21T10:10:00Z"),
        taken("o/r#1#a-1", "2026-09-21T10:00:00Z"),
    ]
    assert credentials.overlaps(credentials.lease_spans(history)) == 1


def test_back_to_back_holds_are_not_an_overlap():
    history = [
        freed("o/r#2#b-1", "2026-09-21T10:20:00Z"),
        taken("o/r#2#b-1", "2026-09-21T10:10:00Z"),
        freed("o/r#1#a-1", "2026-09-21T10:10:00Z"),
        taken("o/r#1#a-1", "2026-09-21T10:00:00Z"),
    ]
    assert credentials.overlaps(credentials.lease_spans(history)) == 0


# --- the report ------------------------------------------------------------


@pytest.fixture
def week():
    history = []
    for day, pr in ((21, 1), (21, 2), (20, 3)):
        history += [
            freed(f"MarketData-App/api#{pr}#r{pr}-1", f"2026-09-{day}T10:10:00Z"),
            taken(f"MarketData-App/api#{pr}#r{pr}-1", f"2026-09-{day}T10:00:00Z"),
        ]
    # 40 minutes, not 30: at 30 it ties api's three ten-minute holds exactly
    # and the ordering test below would pass or fail on the name tie-break
    # rather than on the time held.
    history += [
        freed("MarketDataApp/sdk-py#9#r9-1", "2026-09-20T11:40:00Z"),
        taken("MarketDataApp/sdk-py#9#r9-1", "2026-09-20T11:00:00Z"),
    ]
    return credentials.lease_spans(history)


def test_the_report_totals_every_hold(week):
    text = credentials.lease_report(week, API, days=7, now=dt.datetime(2026, 9, 22, tzinfo=dt.UTC))
    assert "4 holds" in text
    assert "1h 10m" in text  # 10 + 10 + 10 + 40 minutes


def test_the_report_groups_by_day(week):
    text = credentials.lease_report(week, API, days=7, now=dt.datetime(2026, 9, 22, tzinfo=dt.UTC))
    assert "2026-09-21" in text and "2026-09-20" in text
    day_line = [line for line in text.splitlines() if "2026-09-20" in line][0]
    assert "2 holds" in day_line
    assert "0h 50m" in day_line


def test_the_report_groups_by_repository(week):
    text = credentials.lease_report(week, API, days=7, now=dt.datetime(2026, 9, 22, tzinfo=dt.UTC))
    api_line = [line for line in text.splitlines() if "MarketData-App/api" in line][0]
    assert "3 holds" in api_line
    assert "0h 30m" in api_line


def test_the_report_orders_repositories_by_time_held(week):
    text = credentials.lease_report(week, API, days=7, now=dt.datetime(2026, 9, 22, tzinfo=dt.UTC))
    # Anchored on the section, not on the word `holds`: a one-hold row reads
    # `1 hold`, so matching the plural silently skipped the row it meant to find.
    lines = text.splitlines()
    section = lines[lines.index("By repository") :]
    order = [line.split()[0] for line in section[1:] if line.startswith("  ")]
    assert order[:2] == ["MarketDataApp/sdk-py", "MarketData-App/api"]


def test_the_report_says_the_history_cannot_count_blocked_runs(week):
    # The question that started this: "how often is the bot blocked?" The store
    # cannot answer it, and the report must not let a reader think it did.
    text = credentials.lease_report(week, API, days=7, now=dt.datetime(2026, 9, 22, tzinfo=dt.UTC))
    assert "lease is held" in text  # names the log string that does answer it


def test_an_empty_history_reports_nothing_rather_than_dividing_by_zero():
    text = credentials.lease_report([], API, days=7, now=dt.datetime(2026, 9, 22, tzinfo=dt.UTC))
    assert "0 holds" in text


def test_the_report_names_unclosed_holds(week):
    spans = week + credentials.lease_spans(
        [taken("MarketData-App/api#99#r99-1", "2026-09-21T23:00:00Z")]
    )
    text = credentials.lease_report(spans, API, days=7, now=dt.datetime(2026, 9, 22, tzinfo=dt.UTC))
    assert "1 never freed" in text


# --- fetching --------------------------------------------------------------


def test_commits_asks_for_the_lease_path_on_the_lease_branch():
    from reviewbot.github import GitHub

    transport = FakeTransport()
    path = (
        f"/repos/{API}/commits?path=codex/lease.json&sha=main"
        "&since=2026-09-15T00%3A00%3A00%2B00%3A00&per_page=100&page=1"
    )
    transport.add("GET", path, data=[])
    api = GitHub(API, "t", transport=transport)
    got = api.commits(
        credentials.LEASE_PATH,
        credentials.LEASE_BRANCH,
        dt.datetime(2026, 9, 15, tzinfo=dt.UTC),
    )
    assert got == []
    # The `since` bound is what keeps a growing store from being read in full.
    asked = transport.calls[0]["path"]
    assert "path=codex/lease.json" in asked
    assert "sha=main" in asked
    assert "2026-09-15" in asked


# --- the command -----------------------------------------------------------


def test_the_command_prints_a_report_for_the_store(monkeypatch, capsys):
    from reviewbot import cli

    history = [
        freed("MarketData-App/api#1#r1-1", "2026-09-21T10:10:00Z"),
        taken("MarketData-App/api#1#r1-1", "2026-09-21T10:00:00Z"),
    ]

    class FakeApi:
        def commits(self, path, branch, since):
            assert path == credentials.LEASE_PATH
            assert branch == credentials.LEASE_BRANCH
            return history

    monkeypatch.setattr(cli, "_store_api", lambda repo, token: FakeApi())
    assert cli.lease_report(API, days=7, token="t") == 0
    out = capsys.readouterr().out
    assert "1 hold" in out and "1 holds" not in out
    assert "0h 10m" in out
    assert "MarketData-App/api" in out


def test_the_command_reports_an_unreachable_store_without_a_traceback(monkeypatch, capsys):
    from reviewbot import cli
    from reviewbot.github import GitHubError

    class FakeApi:
        def commits(self, path, branch, since):
            raise GitHubError("GET /commits failed with 404: Not Found")

    monkeypatch.setattr(cli, "_store_api", lambda repo, token: FakeApi())
    assert cli.lease_report(API, days=7, token="t") == 1
    assert "404" in capsys.readouterr().out


def test_a_single_hold_is_not_pluralised(week):
    spans = credentials.lease_spans(
        [
            freed("o/r#1#a-1", "2026-09-21T10:01:00Z"),
            taken("o/r#1#a-1", "2026-09-21T10:00:00Z"),
        ]
    )
    text = credentials.lease_report(spans, API, days=7, now=dt.datetime(2026, 9, 22, tzinfo=dt.UTC))
    assert "1 holds" not in text
    assert "1 hold" in text


# --- the window boundary and overlap ---------------------------------------
#
# Both found by the bot's own review of PR #19 (findings a73a0e96, 0585bcb6).

WINDOW_START = dt.datetime(2026, 9, 15, tzinfo=dt.UTC)
NOW = dt.datetime(2026, 9, 22, tzinfo=dt.UTC)


def test_a_hold_that_began_before_the_window_is_clipped_not_dropped():
    # The fetch starts at the cutoff, so such a hold arrives as a lone `freed`.
    # Dropping it lost both the hold and the in-window time it really occupied.
    # The fetch now reaches back far enough to carry the `taken` with it.
    spans = credentials.lease_spans(
        [
            freed("o/r#1#a-1", "2026-09-15T00:10:00Z"),
            taken("o/r#1#a-1", "2026-09-14T23:55:00Z"),
        ]
    )
    assert spans[0].seconds == 900
    clipped = credentials.clip(spans, WINDOW_START, NOW)
    assert len(clipped) == 1
    assert clipped[0].seconds == 600  # only the ten minutes inside the window


def test_a_hold_entirely_before_the_window_is_dropped():
    spans = credentials.lease_spans(
        [
            freed("o/r#1#a-1", "2026-09-14T23:50:00Z"),
            taken("o/r#1#a-1", "2026-09-14T23:40:00Z"),
        ]
    )
    assert credentials.clip(spans, WINDOW_START, NOW) == []


def test_an_unfinished_hold_from_before_the_window_still_shows_as_never_freed():
    spans = credentials.lease_spans([taken("o/r#1#a-1", "2026-09-14T23:40:00Z")])
    clipped = credentials.clip(spans, WINDOW_START, NOW)
    assert len(clipped) == 1
    assert clipped[0].closed is False


def test_occupancy_counts_two_overlapping_holds_once():
    # 10:00-10:20 and 10:10-10:30 occupy thirty minutes, not forty.
    spans = credentials.lease_spans(
        [
            freed("o/r#2#b-1", "2026-09-21T10:30:00Z"),
            taken("o/r#2#b-1", "2026-09-21T10:10:00Z"),
            freed("o/r#1#a-1", "2026-09-21T10:20:00Z"),
            taken("o/r#1#a-1", "2026-09-21T10:00:00Z"),
        ]
    )
    assert sum(s.seconds for s in spans) == 2400  # what the report used to divide
    assert credentials.occupied_seconds(spans) == 1800


def test_occupancy_of_disjoint_holds_is_their_sum():
    spans = credentials.lease_spans(
        [
            freed("o/r#2#b-1", "2026-09-21T11:10:00Z"),
            taken("o/r#2#b-1", "2026-09-21T11:00:00Z"),
            freed("o/r#1#a-1", "2026-09-21T10:10:00Z"),
            taken("o/r#1#a-1", "2026-09-21T10:00:00Z"),
        ]
    )
    assert credentials.occupied_seconds(spans) == 1200


def test_the_busy_share_never_exceeds_the_window():
    # Ten holders covering the same hour cannot make the credential 1000% busy.
    history = []
    for i in range(10):
        history += [
            freed(f"o/r#{i}#r{i}-1", "2026-09-21T11:00:00Z"),
            taken(f"o/r#{i}#r{i}-1", "2026-09-21T10:00:00Z"),
        ]
    spans = credentials.lease_spans(history)
    text = credentials.lease_report(spans, API, days=1, now=dt.datetime(2026, 9, 22, tzinfo=dt.UTC))
    share = float(text.split("busy ")[1].split("%")[0])
    assert 0 < share <= 100


def test_the_report_clips_to_its_own_window():
    spans = credentials.lease_spans(
        [
            freed("o/r#1#a-1", "2026-09-15T00:10:00Z"),
            taken("o/r#1#a-1", "2026-09-14T23:55:00Z"),
        ]
    )
    text = credentials.lease_report(spans, API, days=7, now=NOW)
    assert "1 hold" in text
    assert "0h 10m" in text  # not 0h 15m


def test_a_nonpositive_window_is_refused(monkeypatch):
    # GITHUB_TOKEN is set on purpose: without it `main` exits on the missing
    # token instead, and this test would pass without the validation existing.
    from reviewbot import cli

    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(
        cli, "_store_api", lambda repo, token: pytest.fail("the API must not be reached")
    )
    for bad in ("0", "-3"):
        with pytest.raises(SystemExit):
            cli.main(["lease-report", "--days", bad])
