"""Pure merge logic: combine an existing turnout.json with a new snapshot.

No I/O here. The R2 layer (`r2.py`) calls `merge_snapshot()` between the GET
and the PUT. This separation makes the merge trivially testable.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from votetally.aggregator import Snapshot

# Bibb is in America/New_York. Day boundaries on the dashboard match how a
# Georgia reader thinks about "Tuesday's voters", not UTC midnight.
LOCAL_TZ = ZoneInfo("America/New_York")


class HistoryEntry(TypedDict):
    """A trimmed snapshot record kept in turnout.json's snapshots[] array."""

    scraped_at: str
    total: int
    delta: int  # versus immediately previous snapshot; clamped to >= 0


class DayCount(TypedDict):
    """Voters added on one local-time calendar day.

    Computed from snapshot deltas, not from the source CSV (which has only
    the election's official date, not per-voter cast dates). The first
    snapshot's delta is intentionally excluded — it represents a backlog of
    voters who voted across many prior days, not a single day's count.
    """

    date: str  # ISO YYYY-MM-DD in America/New_York
    voters_added: int


class DayPartyCount(TypedDict, total=False):
    """One day of in-person early-voting turnout, broken down by party.

    Sourced from the "Early Voting (In Person) → by Party and Date" Qlik
    sub-tab. Strictly more accurate than the snapshot-delta-derived `by_day`
    (which can't distinguish parties and groups any same-day deltas).
    """

    date: str             # ISO YYYY-MM-DD
    democrat: int
    republican: int
    non_partisan: int
    total: int


class HubData(TypedDict, total=False):
    """Live turnout from the GA SoS Election Data Hub Qlik dashboard.

    Sourced via `votetally hub`. Fresher than the voter-history zip — the
    dashboard refreshes hourly while the file regenerates ~daily. Hub
    carries the headline total + race breakdown that the file doesn't expose.
    """

    scraped_at: str       # when we scraped (ISO UTC)
    data_as_of: str       # the dashboard's "Data as of: ..." stamp (their TZ)
    county: str
    turnout: int
    active_voters: int
    turnout_pct: float
    by_race: dict[str, int]
    by_day_party: list[DayPartyCount]


class Turnout(TypedDict, total=False):
    """The shape of turnout.json that the frontend consumes.

    `current` is the latest full snapshot from the voter-history zip.
    `snapshots` / `by_day` are derived from a series of those.
    `hub` is the live snapshot from the Election Data Hub (separate source,
    refreshed independently — fresher headline, has race breakdown).
    """

    election: dict[str, str]
    county: str
    current: Snapshot
    snapshots: list[HistoryEntry]
    by_day: list[DayCount]
    hub: HubData
    updated_at: str       # ISO UTC of last run that actually changed data
    last_checked_at: str  # ISO UTC of most recent run, change or not


def empty_turnout() -> Turnout:
    """Initial state when turnout.json doesn't yet exist in R2."""
    return Turnout(
        election={}, county="", current={}, snapshots=[], by_day=[],
        hub={}, updated_at="", last_checked_at="",
    )


def _local_date_of(iso_utc: str) -> str:
    """Convert an ISO 8601 UTC timestamp to a local YYYY-MM-DD date string."""
    dt = datetime.fromisoformat(iso_utc)
    return dt.astimezone(LOCAL_TZ).date().isoformat()


def compute_by_day(snapshots: list[HistoryEntry]) -> list[DayCount]:
    """Group snapshot deltas by local calendar day.

    Skips snapshots[0] — the first observation's delta is a backlog of
    voters who voted across many prior days, not a single day's count, so
    attributing it to one date would mislead. Same-day deltas are summed.
    Returns days in ascending order; days with zero added are omitted.
    """
    if len(snapshots) < 2:
        return []
    by_date: dict[str, int] = {}
    for snap in snapshots[1:]:
        date = _local_date_of(snap["scraped_at"])
        by_date[date] = by_date.get(date, 0) + snap["delta"]
    return [
        DayCount(date=d, voters_added=n)
        for d, n in sorted(by_date.items())
        if n > 0
    ]


def merge_snapshot(prev: Turnout | None, new: Snapshot) -> Turnout:
    """Append `new` to `prev` and recompute the derived fields.

    Behavior decisions worth knowing:
    - Delta is versus the immediately previous snapshot's total.
    - If the new total is *lower* than the previous (e.g., GA SOS reissued
      a corrected file with fewer rows), delta is clamped to 0 rather than
      shown as negative — the chart should not appear to lose voters. The
      raw totals still reflect reality; only the delta is clamped.
    - If the new election_id differs from the previous, we treat this as a
      fresh election and start a new history.
    """
    if prev is None or not prev.get("current"):
        prev = empty_turnout()

    prev_election_id = prev.get("current", {}).get("election_id", "")
    prev_total = prev.get("current", {}).get("total", 0)
    prev_snapshots = prev.get("snapshots", [])

    new_election_id = new["election_id"]
    is_new_election = bool(prev_election_id) and prev_election_id != new_election_id

    if is_new_election:
        prev_total = 0
        prev_snapshots = []

    delta = max(0, new["total"] - prev_total)

    history_entry = HistoryEntry(
        scraped_at=new["scraped_at"],
        total=new["total"],
        delta=delta,
    )
    snapshots = [*prev_snapshots, history_entry]

    return Turnout(
        election={
            "id": new["election_id"],
            "date": new["election_date"],
            "type": new["election_type"],
        },
        county=new["county"],
        current=new,
        snapshots=snapshots,
        by_day=compute_by_day(snapshots),
        hub=prev.get("hub", {}),  # preserve hub data on file-snapshot merges
        updated_at=new["scraped_at"],
        last_checked_at=new["scraped_at"],
    )


def merge_hub(prev: Turnout | None, hub: HubData) -> Turnout:
    """Update only the `hub` field of turnout.json. Preserves everything else.

    `votetally hub` runs more often than `votetally fetch` (hub data is
    fresher), so this lets the two sources update independently without
    stepping on each other.
    """
    if prev is None or not prev.get("current"):
        prev = empty_turnout()
    return Turnout(
        election=prev.get("election", {}),
        county=prev.get("county", hub.get("county", "")),
        current=prev.get("current", {}),  # type: ignore[typeddict-item]
        snapshots=prev.get("snapshots", []),
        by_day=prev.get("by_day", []),
        hub=hub,
        updated_at=hub.get("scraped_at", prev.get("updated_at", "")),
        last_checked_at=hub.get("scraped_at", prev.get("last_checked_at", "")),
    )


def is_duplicate_scrape(prev: Turnout | None, new: Snapshot) -> bool:
    """True if this snapshot is byte-identical to the most recent one.

    Useful to avoid writing a new R2 object when nothing changed (e.g., the
    user re-ran the shortcut twice in quick succession). Only checks the
    derived fields — different scraped_at timestamps still count as duplicates.
    """
    if not prev or not prev.get("current"):
        return False
    cur: dict[str, Any] = dict(prev["current"])
    new_d: dict[str, Any] = dict(new)
    cur.pop("scraped_at", None)
    new_d.pop("scraped_at", None)
    return cur == new_d
