"""2captcha v3 reCAPTCHA solver.

Posts a v3 task to 2captcha (https://2captcha.com/2captcha-api), polls
until a token is returned, hands it back. Pure HTTP, no browser, no
external SDK — just `httpx`.

Pricing reference: ~$2.99/1000 v3 solves at time of writing. At the
project's 4-scrapes/day cadence that's ~$0.36/month.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

TWOCAPTCHA_IN = "https://2captcha.com/in.php"
TWOCAPTCHA_RES = "https://2captcha.com/res.php"

# GA SOS voter-history page key (captured during phase-1 recon).
SOS_SITE_KEY = "6LdUOgYfAAAAAGDYBY939FbeWV3bL-Ktw2EKMoua"
SOS_PAGE_URL = "https://mvp.sos.ga.gov/s/voter-history-files"

# 2captcha returns this status string while still working on the puzzle.
NOT_READY = "CAPCHA_NOT_READY"


class CaptchaError(RuntimeError):
    """Raised when 2captcha returns an error or never produces a token."""


@dataclass(frozen=True)
class V3Request:
    site_key: str = SOS_SITE_KEY
    page_url: str = SOS_PAGE_URL
    action: str = "submit"
    min_score: float = 0.3  # SOS appears to demand at least 0.3; 0.7 starts to hurt solve rate.
    enterprise: int = 1     # SOS uses recaptcha/enterprise.js → set the enterprise flag.


def solve_recaptcha_v3(
    api_key: str | None = None,
    *,
    request: V3Request | None = None,
    timeout: float = 180.0,
    poll_interval: float = 5.0,
) -> str:
    """Mint a fresh v3 token. Blocks until 2captcha returns one or `timeout` elapses.

    Raises CaptchaError on any non-success response from 2captcha.
    """
    api_key = api_key or os.environ.get("TWOCAPTCHA_API_KEY")
    if not api_key:
        raise CaptchaError("Missing TWOCAPTCHA_API_KEY env var")
    req = request or V3Request()

    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        # Submit the v3 task.
        resp = client.post(
            TWOCAPTCHA_IN,
            data={
                "key": api_key,
                "method": "userrecaptcha",
                "version": "v3",
                "googlekey": req.site_key,
                "pageurl": req.page_url,
                "min_score": req.min_score,
                "action": req.action,
                "enterprise": req.enterprise,
                "json": 1,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != 1:
            raise CaptchaError(f"2captcha submission failed: {body!r}")
        task_id = body["request"]
        log.info("2captcha task accepted (id=%s); polling for token", task_id)

        # Poll for the result.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            r = client.get(
                TWOCAPTCHA_RES,
                params={"key": api_key, "action": "get", "id": task_id, "json": 1},
            )
            r.raise_for_status()
            body = r.json()
            if body.get("status") == 1:
                token = body["request"]
                log.info("2captcha token received (%d chars)", len(token))
                return token
            err = body.get("request", "")
            if err and err != NOT_READY:
                raise CaptchaError(f"2captcha error: {err}")

    raise CaptchaError(f"2captcha did not return a token within {timeout}s")
