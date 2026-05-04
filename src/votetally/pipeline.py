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
from votetally.store import (
    HubData,
    Turnout,
    empty_turnout,
    is_duplicate_scrape,
    merge_hub,
    merge_snapshot,
)

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

    prev = r2.get_turnout(county)
    if is_duplicate_scrape(prev, snapshot):
        log.info("snapshot is identical to previous; skipping R2 write")
        return snapshot, prev, False

    updated = merge_snapshot(prev, snapshot)
    r2.put_turnout(updated, county)
    r2.put_archive(snapshot, snapshot["election_id"], snapshot["scraped_at"])
    log.info("uploaded turnout JSON + archive for %s (%s)",
             snapshot["election_id"], county)
    return snapshot, updated, True


def process_hub_and_upload(
    hub_data: HubData, *, r2: R2Client | None = None,
) -> tuple[Turnout, bool]:
    """Merge a hub snapshot into turnout.json and upload. Always writes so
    last_checked_at advances (frontend uses it as a cron heartbeat). The
    returned bool reports whether the source data itself actually changed —
    callers can use it to print "data updated" vs "heartbeat only".

    Change detection compares the full hub payload minus volatile timestamps.
    Comparing only data_as_of would silently drop new fields (e.g.
    by_day_party) when they appear or change between runs that share a
    data_as_of stamp.
    """
    if r2 is None:
        r2 = R2Client(R2Config.from_env())

    # County is carried in hub_data (set by the scraper from the active Qlik
    # filter). Fall back to BIBB if missing — the only path that yields an
    # empty county is a deliberate statewide scrape, which currently isn't
    # wired into cron, so this fallback never fires in production.
    county = hub_data.get("county") or DEFAULT_COUNTY
    prev = r2.get_turnout(county)
    prev_hub = dict(prev.get("hub", {})) if prev else {}
    new_hub = dict(hub_data)
    # scraped_at is wall-clock per run; data_as_of is the dashboard's stamp,
    # which only moves when the source updates. Both are compared together
    # via the rest of the payload — strip scraped_at so identical content
    # at different times still dedups.
    prev_hub.pop("scraped_at", None)
    new_hub.pop("scraped_at", None)
    scraped_at = hub_data.get("scraped_at", "")

    if prev_hub == new_hub:
        # No source change. Still write so last_checked_at advances — the
        # frontend uses it as a liveness pulse, distinct from updated_at
        # (which only moves when the data itself changed).
        log.info("hub payload unchanged from previous; refreshing last_checked_at only")
        updated: Turnout = dict(prev) if prev else empty_turnout()  # type: ignore[assignment]
        updated["last_checked_at"] = scraped_at
        r2.put_turnout(updated, county)
        return updated, False

    updated = merge_hub(prev, hub_data)
    r2.put_turnout(updated, county)
    log.info(
        "uploaded turnout JSON for %s: turnout=%d data_as_of=%s by_day_party=%d",
        county,
        hub_data.get("turnout", 0),
        hub_data.get("data_as_of", ""),
        len(hub_data.get("by_day_party", [])),
    )
    return updated, True
