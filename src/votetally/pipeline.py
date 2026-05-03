"""End-to-end pipeline: zip on disk → R2 turnout.json + archive snapshot.

The CLI and watcher both call `process_and_upload()` so the behavior is
identical whether triggered manually or by a file event.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from votetally.aggregator import Snapshot, aggregate_county
from votetally.parser import election_id_from_zip_name, parse_voter_zip
from votetally.r2 import R2Client, R2Config
from votetally.store import HubData, Turnout, is_duplicate_scrape, merge_hub, merge_snapshot

log = logging.getLogger(__name__)

DEFAULT_COUNTY = "BIBB"


def build_snapshot(zip_path: Path, *, county: str = DEFAULT_COUNTY,
                   scraped_at: str | None = None) -> Snapshot:
    """Pure local computation: zip → snapshot. No R2 contact."""
    if scraped_at is None:
        scraped_at = datetime.now(UTC).isoformat(timespec="seconds")
    election_id = election_id_from_zip_name(zip_path)
    return aggregate_county(
        parse_voter_zip(zip_path),
        county=county,
        election_id=election_id,
        scraped_at=scraped_at,
    )


def process_and_upload(zip_path: Path, *, county: str = DEFAULT_COUNTY,
                       r2: R2Client | None = None) -> tuple[Snapshot, Turnout, bool]:
    """Run the full pipeline. Returns (new_snapshot, updated_turnout, did_write).

    `did_write` is False when the new snapshot is byte-identical to the most
    recent one (so we skipped the R2 PUT to avoid noise in the archive).
    """
    if r2 is None:
        r2 = R2Client(R2Config.from_env())

    snapshot = build_snapshot(zip_path, county=county)
    log.info(
        "processed %s: county=%s total=%d styles=%s",
        zip_path.name, snapshot["county"], snapshot["total"],
        snapshot["by_ballot_style"],
    )

    prev = r2.get_turnout()
    if is_duplicate_scrape(prev, snapshot):
        log.info("snapshot is identical to previous; skipping R2 write")
        return snapshot, prev, False

    updated = merge_snapshot(prev, snapshot)
    r2.put_turnout(updated)
    r2.put_archive(snapshot, snapshot["election_id"], snapshot["scraped_at"])
    log.info("uploaded turnout.json + archive copy for %s", snapshot["election_id"])
    return snapshot, updated, True


def process_hub_and_upload(
    hub_data: HubData, *, r2: R2Client | None = None,
) -> tuple[Turnout, bool]:
    """Merge a hub snapshot into turnout.json and upload. Idempotent: skips
    the write when the hub's `data_as_of` matches what's already in R2.
    """
    if r2 is None:
        r2 = R2Client(R2Config.from_env())

    prev = r2.get_turnout()
    prev_hub = prev.get("hub", {}) if prev else {}
    if prev_hub.get("data_as_of") == hub_data.get("data_as_of") \
            and prev_hub.get("turnout") == hub_data.get("turnout"):
        log.info("hub data_as_of unchanged from previous; skipping R2 write")
        return prev, False

    updated = merge_hub(prev, hub_data)
    r2.put_turnout(updated)
    log.info(
        "uploaded turnout.json with hub data: turnout=%d data_as_of=%s",
        hub_data.get("turnout", 0), hub_data.get("data_as_of", ""),
    )
    return updated, True
