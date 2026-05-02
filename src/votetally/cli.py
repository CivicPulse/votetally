"""votetally CLI — process a zip, watch a directory, or check R2 status."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from votetally.pipeline import DEFAULT_COUNTY, build_snapshot, process_and_upload
from votetally.r2 import R2Client, R2Config

# Load .env from the project root (or wherever the user runs the CLI from).
load_dotenv()

log = logging.getLogger(__name__)
console = Console()

# Election zips look like 'A-12599.zip' (or 'A-12599 (3).zip' after Chrome dedup).
ELECTION_ZIP_RE = re.compile(r"^A-\d+(?:\s+\(\d+\))?\.zip$", re.IGNORECASE)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool) -> None:
    """Bibb County voter turnout tracker."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@cli.command()
@click.argument("zip_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--county", default=DEFAULT_COUNTY, show_default=True,
              help="County name (uppercase) to aggregate.")
def process(zip_path: Path, county: str) -> None:
    """Parse a zip locally and print the snapshot. No R2 contact."""
    snapshot = build_snapshot(zip_path, county=county)
    console.print_json(data=snapshot)


@cli.command()
@click.argument("zip_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--county", default=DEFAULT_COUNTY, show_default=True)
def upload(zip_path: Path, county: str) -> None:
    """Run the full pipeline: process the zip and upload to R2."""
    snapshot, updated, did_write = process_and_upload(zip_path, county=county)
    if did_write:
        console.print(
            f"[green]✓[/green] uploaded turnout.json — {snapshot['county']} total "
            f"[bold]{snapshot['total']}[/bold] (history len {len(updated['snapshots'])})"
        )
    else:
        console.print(
            f"[yellow]·[/yellow] no change since last scrape "
            f"({snapshot['county']} total {snapshot['total']}); skipped R2 write"
        )


@cli.command()
@click.argument("watch_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--county", default=DEFAULT_COUNTY, show_default=True)
def watch(watch_dir: Path, county: str) -> None:
    """Watch a directory for new election zips and run the pipeline on each.

    Uses inotify under the hood (via watchfiles). Files matching the pattern
    'A-NNNNN.zip' (with optional ' (N)' suffix from browser dedup) trigger
    processing. Files modified during the watcher run are processed; files
    already present at startup are NOT (use `votetally upload <zip>` for those).
    """
    from watchfiles import Change
    from watchfiles import watch as wf_watch

    console.print(f"[blue]watching[/blue] {watch_dir} for election zips…")
    for changes in wf_watch(watch_dir):
        for change, path_str in changes:
            if change == Change.deleted:
                continue
            path = Path(path_str)
            if not ELECTION_ZIP_RE.match(path.name):
                continue
            if not path.exists() or path.stat().st_size == 0:
                continue  # still being written
            console.print(f"[cyan]→[/cyan] {path.name} appeared; processing")
            try:
                snapshot, updated, did_write = process_and_upload(path, county=county)
                if did_write:
                    console.print(
                        f"  [green]uploaded[/green] total={snapshot['total']} "
                        f"history_len={len(updated['snapshots'])}"
                    )
                else:
                    console.print("  [yellow]no change[/yellow] (duplicate)")
            except Exception as e:
                log.exception("pipeline failed for %s", path)
                console.print(f"  [red]error[/red] {e}")


@cli.command()
def status() -> None:
    """Fetch turnout.json from R2 and print a summary."""
    r2 = R2Client(R2Config.from_env())
    data = r2.get_turnout()
    if not data.get("current"):
        console.print("[yellow]No turnout.json in R2 yet.[/yellow]")
        return
    cur = data["current"]
    console.print(
        f"[bold]{cur['county']}[/bold] · {cur['election_type']} "
        f"on {cur['election_date']} · scraped {cur['scraped_at']}"
    )
    console.print(f"Total voters: [bold green]{cur['total']:,}[/bold green]")

    by_style = Table(title="By Ballot Style", show_header=True)
    by_style.add_column("Style")
    by_style.add_column("Count", justify="right")
    for style, n in sorted(cur["by_ballot_style"].items(), key=lambda x: -x[1]):
        by_style.add_row(style, f"{n:,}")
    console.print(by_style)

    history = data.get("snapshots", [])
    if history:
        console.print(f"History: {len(history)} snapshots — "
                      f"latest delta +{history[-1]['delta']:,}")


@cli.command(name="r2-init")
def r2_init() -> None:
    """Verify R2 credentials by listing the bucket. Prints first 10 keys."""
    r2 = R2Client(R2Config.from_env())
    resp = r2.s3.list_objects_v2(Bucket=r2.cfg.bucket, MaxKeys=10)
    keys = [obj["Key"] for obj in resp.get("Contents", [])]
    if not keys:
        console.print(f"[green]✓[/green] connected to '{r2.cfg.bucket}' (empty)")
    else:
        console.print(f"[green]✓[/green] '{r2.cfg.bucket}' contains:")
        for k in keys:
            console.print(f"  · {k}")


@cli.command(name="dump-shape")
def dump_shape() -> None:
    """Process the local A-12599.zip and dump the merged turnout.json shape.

    Useful for designing the frontend — produces what the live R2 file will look
    like after one or more scrapes, without contacting R2.
    """
    from votetally.store import merge_snapshot

    zip_path = Path("/home/kwhatcher/Downloads/A-12599.zip")
    if not zip_path.exists():
        console.print(f"[red]Missing[/red] {zip_path}")
        raise click.Abort
    snap = build_snapshot(zip_path)
    sample = merge_snapshot(None, snap)
    console.print_json(data=json.loads(json.dumps(sample)))
