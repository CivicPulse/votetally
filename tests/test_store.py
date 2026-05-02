"""Tests for the pure merge logic in store.py."""
from __future__ import annotations

from votetally.aggregator import Snapshot
from votetally.store import (
    compute_by_day,
    empty_turnout,
    is_duplicate_scrape,
    merge_snapshot,
)


def _snap(total: int, scraped_at: str = "2026-05-02T17:00:00Z",
          election_id: str = "A-12599") -> Snapshot:
    """Test factory — a minimally populated snapshot."""
    return Snapshot(
        scraped_at=scraped_at,
        election_id=election_id,
        election_date="05/19/2026",
        election_type="GENERAL PRIMARY",
        county="BIBB",
        total=total,
        by_ballot_style={"EARLY IN-PERSON": total},
        by_party={"DEMOCRAT": total},
    )


def test_first_scrape_starts_history():
    out = merge_snapshot(None, _snap(2046))
    assert out["current"]["total"] == 2046
    assert len(out["snapshots"]) == 1
    assert out["snapshots"][0]["delta"] == 2046  # vs implicit prev=0
    assert out["election"]["id"] == "A-12599"


def test_second_scrape_appends_with_correct_delta():
    out1 = merge_snapshot(None, _snap(2046, "2026-05-02T17:00:00Z"))
    out2 = merge_snapshot(out1, _snap(2100, "2026-05-02T23:00:00Z"))
    assert len(out2["snapshots"]) == 2
    assert out2["snapshots"][1]["delta"] == 54  # 2100 - 2046
    assert out2["current"]["total"] == 2100


def test_negative_delta_is_clamped_to_zero():
    """If SOS reissues a smaller file, the chart should not appear to lose voters."""
    out1 = merge_snapshot(None, _snap(2100))
    out2 = merge_snapshot(out1, _snap(2050, "2026-05-02T23:00:00Z"))
    assert out2["snapshots"][1]["delta"] == 0
    assert out2["current"]["total"] == 2050  # raw total still reflects reality


def test_new_election_resets_history():
    """Switching from A-12599 to A-12600 starts a fresh history."""
    out1 = merge_snapshot(None, _snap(2046, election_id="A-12599"))
    out2 = merge_snapshot(out1, _snap(500, "2026-06-01T00:00:00Z", election_id="A-12600"))
    assert out2["election"]["id"] == "A-12600"
    assert len(out2["snapshots"]) == 1  # history reset
    assert out2["snapshots"][0]["delta"] == 500


def test_is_duplicate_scrape_ignores_timestamp():
    out1 = merge_snapshot(None, _snap(2046, "2026-05-02T17:00:00Z"))
    same_data_later = _snap(2046, "2026-05-02T18:00:00Z")
    assert is_duplicate_scrape(out1, same_data_later) is True

    different_data = _snap(2047, "2026-05-02T18:00:00Z")
    assert is_duplicate_scrape(out1, different_data) is False


def test_empty_turnout_shape_is_safe_to_merge_into():
    out = merge_snapshot(empty_turnout(), _snap(100))
    assert out["current"]["total"] == 100


# --- by_day computation ----------------------------------------------------

def test_by_day_is_empty_with_one_snapshot():
    """First snapshot's delta is a backlog, not a single day's count."""
    out = merge_snapshot(None, _snap(2046, "2026-05-02T17:00:00+00:00"))
    assert out["by_day"] == []


def test_by_day_attributes_delta_to_local_calendar_day():
    """Two snapshots same Eastern-time day → one bar with the second delta."""
    out1 = merge_snapshot(None, _snap(2000, "2026-05-02T15:00:00+00:00"))
    out2 = merge_snapshot(out1, _snap(2150, "2026-05-02T21:00:00+00:00"))
    assert out2["by_day"] == [{"date": "2026-05-02", "voters_added": 150}]


def test_by_day_groups_multiple_same_day_deltas():
    out1 = merge_snapshot(None, _snap(2000, "2026-05-02T15:00:00+00:00"))
    out2 = merge_snapshot(out1, _snap(2050, "2026-05-02T19:00:00+00:00"))
    out3 = merge_snapshot(out2, _snap(2150, "2026-05-02T23:00:00+00:00"))
    assert out3["by_day"] == [{"date": "2026-05-02", "voters_added": 150}]


def test_by_day_uses_eastern_time_for_day_boundaries():
    """A 03:00 UTC scrape is 23:00 EDT *the previous day* — should land
    on the prior calendar day for a Georgia reader."""
    # Snapshot 1: noon UTC May 2 = 8am EDT May 2 (baseline, skipped)
    # Snapshot 2: 03:00 UTC May 3 = 23:00 EDT May 2 (still May 2 in ET)
    out1 = merge_snapshot(None, _snap(2000, "2026-05-02T12:00:00+00:00"))
    out2 = merge_snapshot(out1, _snap(2100, "2026-05-03T03:00:00+00:00"))
    assert out2["by_day"] == [{"date": "2026-05-02", "voters_added": 100}]


def test_by_day_spans_multiple_days_in_order():
    out1 = merge_snapshot(None, _snap(1000, "2026-04-30T12:00:00+00:00"))
    out2 = merge_snapshot(out1, _snap(1500, "2026-05-01T15:00:00+00:00"))
    out3 = merge_snapshot(out2, _snap(2046, "2026-05-02T15:00:00+00:00"))
    assert out3["by_day"] == [
        {"date": "2026-05-01", "voters_added": 500},
        {"date": "2026-05-02", "voters_added": 546},
    ]


def test_by_day_omits_zero_days():
    """Duplicate scrapes (delta=0) shouldn't produce a 0-voter bar."""
    out1 = merge_snapshot(None, _snap(2000, "2026-05-02T12:00:00+00:00"))
    out2 = merge_snapshot(out1, _snap(2000, "2026-05-02T18:00:00+00:00"))
    assert out2["by_day"] == []


def test_compute_by_day_pure_function_handles_empty():
    assert compute_by_day([]) == []
    assert compute_by_day([{"scraped_at": "2026-05-02T12:00:00+00:00",
                            "total": 100, "delta": 100}]) == []
