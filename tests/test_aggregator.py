"""Tests for parser + aggregator using the real downloaded zip as ground truth."""
from __future__ import annotations

from pathlib import Path

import pytest

from votetally.aggregator import aggregate_county
from votetally.parser import election_id_from_zip_name, parse_voter_zip

REAL_ZIP = Path("/home/kwhatcher/Downloads/A-12599.zip")


@pytest.mark.skipif(not REAL_ZIP.exists(), reason="real GA SOS zip not present")
def test_real_zip_bibb_matches_awk_baseline():
    """Use the actual downloaded May 19, 2026 General Primary file.

    Baseline values were computed via shell awk during recon:
      - Total Bibb rows: 2046
      - Bibb x EARLY IN-PERSON: 1979
      - Bibb x ABSENTEE BY MAIL: 67
      - All Bibb records have election_date = 05/19/2026 and type = GENERAL PRIMARY
    """
    snapshot = aggregate_county(
        parse_voter_zip(REAL_ZIP),
        county="BIBB",
        election_id="A-12599",
        scraped_at="2026-05-02T17:00:00Z",
    )
    assert snapshot["total"] == 2046
    assert snapshot["by_ballot_style"]["EARLY IN-PERSON"] == 1979
    assert snapshot["by_ballot_style"]["ABSENTEE BY MAIL"] == 67
    assert snapshot["election_date"] == "05/19/2026"
    assert snapshot["election_type"] == "GENERAL PRIMARY"
    assert snapshot["county"] == "BIBB"
    assert snapshot["election_id"] == "A-12599"


def test_election_id_from_zip_name_handles_browser_dedup():
    """Chrome de-dups concurrent downloads as 'A-12599 (3).zip' — strip the suffix."""
    assert election_id_from_zip_name(Path("A-12599.zip")) == "A-12599"
    assert election_id_from_zip_name(Path("A-12599 (3).zip")) == "A-12599"
    assert election_id_from_zip_name(Path("/some/dir/A-12605.zip")) == "A-12605"


def test_aggregate_county_with_synthetic_data():
    """Verify counters and election-meta inference on a hand-built record set."""
    from votetally.parser import VoteRecord

    records = [
        VoteRecord("BIBB", "001", "05/19/2026", "GENERAL PRIMARY", "DEMOCRAT",
                   "EARLY IN-PERSON", "Y", "", "N"),
        VoteRecord("BIBB", "002", "05/19/2026", "GENERAL PRIMARY", "REPUBLICAN",
                   "EARLY IN-PERSON", "Y", "", "N"),
        VoteRecord("BIBB", "003", "05/19/2026", "GENERAL PRIMARY", "DEMOCRAT",
                   "ABSENTEE BY MAIL", "Y", "", "N"),
        # different county should be filtered out
        VoteRecord("FULTON", "004", "05/19/2026", "GENERAL PRIMARY", "DEMOCRAT",
                   "EARLY IN-PERSON", "Y", "", "N"),
    ]
    snap = aggregate_county(
        records, county="BIBB", election_id="A-12599", scraped_at="2026-05-02T17:00:00Z"
    )
    assert snap["total"] == 3
    assert snap["by_ballot_style"] == {"EARLY IN-PERSON": 2, "ABSENTEE BY MAIL": 1}
    assert snap["by_party"] == {"DEMOCRAT": 2, "REPUBLICAN": 1}
    assert snap["election_date"] == "05/19/2026"
    assert snap["election_type"] == "GENERAL PRIMARY"
