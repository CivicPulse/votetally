"""Aggregate parsed records into a snapshot for one county.

A "snapshot" describes what was true in the data at one observation point.
The caller supplies `scraped_at` separately — the aggregator never reads the clock.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import TypedDict

from votetally.parser import VoteRecord


class Snapshot(TypedDict):
    """A single observation of turnout for one county in one election."""

    scraped_at: str  # ISO 8601 UTC, supplied by caller
    election_id: str  # e.g. "A-12599"
    election_date: str  # MM/DD/YYYY as it appears in the source CSV
    election_type: str  # e.g. "GENERAL PRIMARY"
    county: str  # e.g. "BIBB"
    total: int
    by_ballot_style: dict[str, int]
    by_party: dict[str, int]


def aggregate_county(
    records: Iterable[VoteRecord],
    *,
    county: str,
    election_id: str,
    scraped_at: str,
) -> Snapshot:
    """Filter records to one county, count by ballot style and party.

    Election metadata (date, type) is derived from the records themselves,
    so we don't need to pass it in. If multiple election dates appear (which
    shouldn't happen in a normal SOS file), the most common one wins.
    """
    by_style: Counter[str] = Counter()
    by_party: Counter[str] = Counter()
    election_dates: Counter[str] = Counter()
    election_types: Counter[str] = Counter()
    total = 0

    for r in records:
        if r.county != county:
            continue
        total += 1
        if r.ballot_style:
            by_style[r.ballot_style] += 1
        if r.party:
            by_party[r.party] += 1
        if r.election_date:
            election_dates[r.election_date] += 1
        if r.election_type:
            election_types[r.election_type] += 1

    election_date = election_dates.most_common(1)[0][0] if election_dates else ""
    election_type = election_types.most_common(1)[0][0] if election_types else ""

    return Snapshot(
        scraped_at=scraped_at,
        election_id=election_id,
        election_date=election_date,
        election_type=election_type,
        county=county,
        total=total,
        by_ballot_style=dict(by_style),
        by_party=dict(by_party),
    )
