"""Cloudflare R2 client — reads the live turnout.json, writes back updated state.

R2 is S3-compatible, so boto3 works with two adjustments:
  1. Custom endpoint: https://<account_id>.r2.cloudflarestorage.com
  2. Region must be "auto"

Auth: env vars (read once at construction; not loaded from disk by this module).
A small .env file in the repo root, sourced before running, is the simplest local
setup. We deliberately don't pull in python-dotenv — the user's shell can do it.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from votetally.store import Turnout, empty_turnout

log = logging.getLogger(__name__)

TURNOUT_KEY = "turnout.json"


@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str

    @classmethod
    def from_env(cls) -> R2Config:
        try:
            return cls(
                account_id=os.environ["R2_ACCOUNT_ID"],
                access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                bucket=os.environ["R2_BUCKET"],
            )
        except KeyError as e:
            raise RuntimeError(
                f"Missing required env var {e}. "
                "Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET."
            ) from e


class R2Client:
    def __init__(self, cfg: R2Config) -> None:
        self.cfg = cfg
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{cfg.account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    def get_turnout(self) -> Turnout:
        """Fetch turnout.json. Returns empty Turnout if the object doesn't exist."""
        try:
            resp = self.s3.get_object(Bucket=self.cfg.bucket, Key=TURNOUT_KEY)
            data = json.loads(resp["Body"].read())
            return data  # type: ignore[no-any-return]
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                log.info("turnout.json not yet in R2; starting fresh")
                return empty_turnout()
            raise

    def put_turnout(self, data: Turnout) -> None:
        """Replace turnout.json with the new state. Short cache-control so the
        edge re-fetches within a few minutes of a scrape."""
        self.s3.put_object(
            Bucket=self.cfg.bucket,
            Key=TURNOUT_KEY,
            Body=json.dumps(data, indent=2).encode("utf-8"),
            ContentType="application/json",
            CacheControl="public, max-age=300",
        )

    def put_archive(self, data: dict, election_id: str, scraped_at: str) -> None:
        """Write an immutable per-scrape archive copy.

        Filenames are URL-safe (colons in ISO timestamps replaced with hyphens).
        Long cache-control: archives never change.
        """
        safe_ts = scraped_at.replace(":", "-")
        key = f"archive/{election_id}/{safe_ts}.json"
        self.s3.put_object(
            Bucket=self.cfg.bucket,
            Key=key,
            Body=json.dumps(data, indent=2).encode("utf-8"),
            ContentType="application/json",
            CacheControl="public, max-age=31536000, immutable",
        )
