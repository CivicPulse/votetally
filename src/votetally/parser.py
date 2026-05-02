"""Parse the GA SOS voter history zip → iterator of typed records.

The actual file inside the zip is a comma-delimited CSV with header row
(despite the SOS website still documenting an old fixed-width layout):

    County Name,Voter Registration Number,Election Date,Election Type,
    Party,Ballot Style,Absentee,Provisional,Supplemental

County Name is uppercase plain text (e.g. "BIBB"). Election Date is MM/DD/YYYY.
Ballot Style values seen so far: EARLY IN-PERSON, ABSENTEE BY MAIL,
ELECTRONIC BALLOT DELIVERY, and (post-election-day, expected) blank/ELECTION DAY.
"""
from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class VoteRecord:
    county: str
    voter_id: str
    election_date: str
    election_type: str
    party: str
    ballot_style: str
    absentee: str
    provisional: str
    supplemental: str


REQUIRED_COLUMNS = (
    "County Name",
    "Voter Registration Number",
    "Election Date",
    "Election Type",
    "Party",
    "Ballot Style",
    "Absentee",
    "Provisional",
    "Supplemental",
)


def parse_voter_zip(zip_path: Path) -> Iterator[VoteRecord]:
    """Yield one VoteRecord per row from the CSV inside the zip.

    Raises ValueError if the zip doesn't contain a CSV or has unexpected columns.
    """
    with zipfile.ZipFile(zip_path) as zf:
        csv_members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_members:
            raise ValueError(f"No CSV file inside {zip_path}")
        with zf.open(csv_members[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(
                    f"CSV missing required columns: {missing}. "
                    f"Got: {reader.fieldnames}"
                )
            for row in reader:
                yield VoteRecord(
                    county=row["County Name"].strip(),
                    voter_id=row["Voter Registration Number"].strip(),
                    election_date=row["Election Date"].strip(),
                    election_type=row["Election Type"].strip(),
                    party=row["Party"].strip(),
                    ballot_style=row["Ballot Style"].strip(),
                    absentee=row["Absentee"].strip(),
                    provisional=row["Provisional"].strip(),
                    supplemental=row["Supplemental"].strip(),
                )


def election_id_from_zip_name(zip_path: Path) -> str:
    """Extract the GA election slug (e.g. 'A-12599') from a zip filename.

    Handles browser de-dup suffixes like 'A-12599 (3).zip'.
    """
    stem = zip_path.stem
    return stem.split(" ")[0]
