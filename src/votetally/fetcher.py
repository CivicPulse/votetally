"""Browser-driven fetch: drive the GA SOS form via CDP-attached Chrome.

Connects (via Patchright's `connect_over_cdp`) to a Chrome the user has
launched separately with `votetally chrome --launch`. That Chrome runs in
its own dedicated profile dir (`~/.config/chrome-votetally`) with
--remote-debugging-port=9222 and a real, warm browser fingerprint —
which is what passes the GA SoS Bot_Check_Active__c gate plus reCAPTCHA
Enterprise on its own native score.

2captcha is opt-in via `with_captcha=True` as a break-glass fallback if
the warm-Chrome score ever drops below threshold.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path

import httpx
from patchright.sync_api import Page, sync_playwright
from patchright.sync_api import TimeoutError as PWTimeout

from votetally.captcha import SOS_PAGE_URL, solve_recaptcha_v3

DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"

log = logging.getLogger(__name__)

S3_HOST = "prod-ga-sos-vr-data-processing-bucket.s3.amazonaws.com"
DEFAULT_ELECTION_TEXT_RE = re.compile(r"MAY 19, 2026 - GENERAL PRIMARY ELECTION", re.I)
DIAG_DIR = Path(__file__).resolve().parents[2] / "experiments" / "artifacts"


class FetchError(RuntimeError):
    """Raised when the SOS form can't be driven to completion."""


def _dump_diagnostics(page: Page, label: str, console_log: list[str], aura_log: list[dict]) -> None:
    """Snapshot page state for debugging — screenshot, HTML, console, network."""
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = DIAG_DIR / f"fetch-fail-{label}-{stamp}"
    try:
        page.screenshot(path=f"{base}.png", full_page=True)
        Path(f"{base}.html").write_text(page.content(), encoding="utf-8")
        Path(f"{base}.console.log").write_text("\n".join(console_log), encoding="utf-8")
        Path(f"{base}.aura.json").write_text(json.dumps(aura_log, indent=2), encoding="utf-8")
        body_text = page.evaluate("() => document.body && document.body.innerText || ''")
        Path(f"{base}.body.txt").write_text(body_text, encoding="utf-8")
        log.warning("diagnostics saved → %s.{png,html,console.log,aura.json,body.txt}", base)
    except Exception as e:  # noqa: BLE001
        log.error("diagnostic capture itself failed: %s", e)


def _patch_grecaptcha_now(token: str) -> str:
    """JS to run via page.evaluate immediately before clicking Submit.

    Replaces grecaptcha.execute / grecaptcha.enterprise.execute with a stub
    that returns our 2captcha-minted token. Only used when with_captcha=True.
    """
    return f"""
    (() => {{
        const TOKEN = {json.dumps(token)};
        const stub = () => {{
            console.log('[votetally] grecaptcha execute() invoked → returning 2captcha token');
            return Promise.resolve(TOKEN);
        }};
        let patched = [];
        if (typeof grecaptcha !== 'undefined') {{
            grecaptcha.execute = stub;
            patched.push('grecaptcha.execute');
        }}
        if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) {{
            grecaptcha.enterprise.execute = stub;
            patched.push('grecaptcha.enterprise.execute');
        }}
        if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise &&
            typeof grecaptcha.enterprise.ready === 'function') {{
            const origReady = grecaptcha.enterprise.ready;
            grecaptcha.enterprise.ready = (fn) => {{
                try {{ origReady.call(grecaptcha.enterprise, fn); }} catch (e) {{}}
                fn && fn();
            }};
        }}
        return patched;
    }})();
    """


@contextmanager
def fetch_signed_url(
    *,
    election_text_re: re.Pattern[str] = DEFAULT_ELECTION_TEXT_RE,
    captcha_api_key: str | None = None,
    cdp_endpoint: str = DEFAULT_CDP_ENDPOINT,
    with_captcha: bool = False,
) -> Iterator[str]:
    """Yield the signed S3 URL for the active election.

    Attaches to a user-launched Chrome at `cdp_endpoint`, finds the SoS tab,
    drives the form, captures the signed S3 URL from the rendered anchor.
    Pass `with_captcha=True` to also mint a 2captcha token and patch
    grecaptcha.execute (rarely needed — warm Chrome's native score suffices).
    """
    token: str | None = None
    if with_captcha and (captcha_api_key or os.environ.get("TWOCAPTCHA_API_KEY")):
        log.info("requesting v3 token from 2captcha (this typically takes 5-30s)")
        token = solve_recaptcha_v3(captcha_api_key)
    else:
        log.info("skipping 2captcha (warm Chrome's native score should suffice)")

    with sync_playwright() as p:
        log.info("attaching to user-launched Chrome via CDP at %s", cdp_endpoint)
        try:
            browser = p.chromium.connect_over_cdp(cdp_endpoint)
        except Exception as e:  # noqa: BLE001
            raise FetchError(
                f"Could not connect to Chrome at {cdp_endpoint}. "
                "Run `votetally chrome --launch` first to start a CDP-enabled Chrome."
            ) from e

        if not browser.contexts:
            raise FetchError(
                f"Chrome at {cdp_endpoint} reports zero contexts. "
                "Make sure it has at least one tab open."
            )

        console_log: list[str] = []
        aura_log: list[dict] = []
        page: Page | None = None
        try:
            context = browser.contexts[0]
            for existing in context.pages:
                if "mvp.sos.ga.gov" in existing.url:
                    page = existing
                    log.info("reusing existing SOS tab: %s", existing.url)
                    break
            if page is None:
                page = context.new_page()

            page.on("console", lambda msg: console_log.append(f"[{msg.type}] {msg.text}"))

            def on_response(resp):
                # Capture Aura traffic — submit's Apex call is the load-bearing one.
                if "/sfsites/aura" in resp.url and resp.request.method == "POST":
                    try:
                        body_preview = resp.text()[:2000]
                    except Exception:  # noqa: BLE001
                        body_preview = "<body unreadable>"
                    aura_log.append({
                        "url": resp.url,
                        "status": resp.status,
                        "request_body": (resp.request.post_data or "")[:1500],
                        "response_preview": body_preview,
                    })
            page.on("response", on_response)

            # Navigate only if the reused tab isn't already on the right URL.
            # `load` not `domcontentloaded` — LWC bundles + Aura init happen
            # after DCL; the form components don't exist yet at that point.
            if "/s/voter-history-files" not in page.url:
                log.info("navigating to %s", SOS_PAGE_URL)
                page.goto(SOS_PAGE_URL, wait_until="load", timeout=45_000)
            else:
                log.info("page already on target URL — skipping navigation")

            log.info("waiting for form to hydrate (LWC bundles must finish)")
            year_combo = page.get_by_role("combobox", name="Election Year")
            try:
                year_combo.wait_for(state="visible", timeout=40_000)
            except PWTimeout as e:
                _dump_diagnostics(page, "hydrate", console_log, aura_log)
                raise FetchError(
                    "Election Year combobox never appeared within 40s. "
                    "Diagnostics under experiments/artifacts/ — check the "
                    "screenshot to see what the page actually rendered "
                    "(login wall? bot banner? still loading?)."
                ) from e

            log.info("selecting year and election")
            year_combo.click()
            page.get_by_role("option", name="2026").click()
            date_combo = page.get_by_role("combobox", name="Election Date & Name")
            date_combo.wait_for(state="visible")
            date_combo.click()
            page.get_by_role("option", name=election_text_re).click()

            log.info("waiting for Submit to be enabled (LWC commit latency)")
            submit_btn = page.get_by_role("button", name="SUBMIT")
            deadline = time.monotonic() + 10
            while not submit_btn.is_enabled():
                if time.monotonic() > deadline:
                    _dump_diagnostics(page, "submit-disabled", console_log, aura_log)
                    raise FetchError(
                        "Submit button never became enabled within 10s. "
                        "Check the diagnostic screenshot."
                    )
                page.wait_for_timeout(200)

            if token is not None:
                log.info("patching grecaptcha.execute right before submit")
                patched = page.evaluate(_patch_grecaptcha_now(token))
                if not patched:
                    log.warning("grecaptcha not yet loaded at patch time; retrying in 2s")
                    page.wait_for_timeout(2000)
                    patched = page.evaluate(_patch_grecaptcha_now(token))
                log.info("patched surfaces: %s", patched)

            log.info("clicking Submit")
            submit_btn.click()

            log.info("waiting for signed S3 URL to appear")
            # Locator pierces LWC Shadow DOM; document.querySelector wouldn't.
            s3_link = page.locator(f'a[href*="{S3_HOST}"]').first
            try:
                s3_link.wait_for(state="attached", timeout=60_000)
            except PWTimeout as e:
                # Did the bot-detection banner appear instead?
                err = page.get_by_text("Seems like you are trying to use automated scripts")
                if err.count() > 0:
                    _dump_diagnostics(page, "banner", console_log, aura_log)
                    raise FetchError(
                        "Submit was rejected: site shows the bot-detection banner. "
                        "Try `--with-captcha` to mint a token, or refresh your "
                        "Chrome session manually so cf_clearance gets renewed."
                    ) from e
                _dump_diagnostics(page, "indeterminate", console_log, aura_log)
                raise FetchError(
                    "Neither signed URL nor error banner appeared in time. "
                    "Diagnostics saved under experiments/artifacts/."
                ) from e

            href = s3_link.get_attribute("href")
            if not href:
                raise FetchError("Found anchor but href was empty")
            log.info("captured signed URL (%d chars)", len(href))
            yield href
        finally:
            # CDP-attached: leave the user's Chrome alive; close just our tab
            # (and only if we created it — not if we reused an existing one).
            with suppress(Exception):
                browser.close()  # detaches CDP without killing the browser


def download_zip(signed_url: str, dest: Path) -> Path:
    """Download the zip from S3 to disk. The signed URL is auth-bearing —
    nothing else is needed on the request."""
    log.info("downloading zip → %s", dest)
    with httpx.stream("GET", signed_url, timeout=60.0) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(64 * 1024):
                f.write(chunk)
    log.info("downloaded %d bytes", dest.stat().st_size)
    return dest


def fetch_and_download(
    *,
    out_dir: Path | None = None,
    election_text_re: re.Pattern[str] = DEFAULT_ELECTION_TEXT_RE,
    captcha_api_key: str | None = None,
    cdp_endpoint: str = DEFAULT_CDP_ENDPOINT,
    with_captcha: bool = False,
) -> Path:
    """High-level helper: drive form via CDP-attached Chrome → download zip.

    Returns the path to the downloaded zip. Names it `A-NNNNN.zip` based on
    the URL path so it matches what the Claude for Chrome shortcut produces.
    """
    out_dir = out_dir or Path(tempfile.gettempdir()) / "votetally"
    out_dir.mkdir(parents=True, exist_ok=True)

    with fetch_signed_url(
        election_text_re=election_text_re,
        captcha_api_key=captcha_api_key,
        cdp_endpoint=cdp_endpoint,
        with_captcha=with_captcha,
    ) as url:
        # URL path looks like /GAVR/VOTER_ZIP/2026/A-12599/A-12599.zip — last
        # segment is the canonical filename.
        path = httpx.URL(url).path
        filename = path.rsplit("/", 1)[-1] or "voter-history.zip"
        dest = out_dir / filename
        return download_zip(url, dest)
