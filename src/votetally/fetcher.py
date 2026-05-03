"""Browser-driven fetch: drive the GA SOS form with a 2captcha-minted v3 token.

We don't know the exact Apex method the form's Submit button calls — phase-1
recon never observed that POST because reCAPTCHA blocked it client-side. So
instead of replaying that call directly, we drive the form in a real browser
and only patch the part that's failing: `grecaptcha.execute()`. The site's
own JS does whatever Apex round-trip it needs and renders the signed-URL
link in the DOM, which we read.
"""
# ruff: noqa: E501  -- multi-line JS template strings exceed 100 cols by design
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
from patchright.sync_api import TimeoutError as PatchrightTimeout
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from votetally.captcha import SOS_PAGE_URL, solve_recaptcha_v3

DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"

# patchright (used in CDP mode) raises its own TimeoutError class — distinct
# from playwright's. Catch both anywhere we wait on the browser.
TIMEOUT_ERRORS: tuple[type[Exception], ...] = (PlaywrightTimeout, PatchrightTimeout)

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


_NETWORK_TRACE_SCRIPT = """
(() => {
    if (window.__votetallyTraceInstalled) return;
    window.__votetallyTraceInstalled = true;
    window.__votetallyTrace = [];
    const log = (kind, info) => window.__votetallyTrace.push({kind, t: Date.now(), ...info});

    // fetch
    const origFetch = window.fetch;
    window.fetch = function (...args) {
        const url = typeof args[0] === 'string' ? args[0] : args[0]?.url;
        log('fetch.start', {url});
        return origFetch.apply(this, args).then(r => {
            log('fetch.done', {url, status: r.status});
            return r;
        }, e => { log('fetch.err', {url, err: String(e)}); throw e; });
    };
    // XHR
    const OrigXHR = window.XMLHttpRequest;
    window.XMLHttpRequest = function () {
        const x = new OrigXHR();
        const origOpen = x.open;
        x.open = function (method, url, ...rest) {
            x.__votetally_url = url;
            log('xhr.open', {method, url});
            return origOpen.call(this, method, url, ...rest);
        };
        const origSend = x.send;
        x.send = function (body) {
            log('xhr.send', {url: x.__votetally_url, body: typeof body === 'string' ? body.slice(0, 500) : '(non-string)'});
            x.addEventListener('loadend', () => {
                log('xhr.done', {url: x.__votetally_url, status: x.status});
            });
            return origSend.call(this, body);
        };
        return x;
    };

    // grecaptcha — log every call to any method, even ones we don't patch
    const wrapAllMethods = (api, label) => {
        if (!api || api.__votetallyWrapped) return;
        api.__votetallyWrapped = true;
        for (const k of Object.keys(api)) {
            if (typeof api[k] === 'function') {
                const orig = api[k];
                api[k] = function (...args) {
                    log('grecaptcha.' + label + '.' + k, {args: args.slice(0, 2).map(a => typeof a)});
                    return orig.apply(this, args);
                };
            }
        }
    };
    let _g;
    Object.defineProperty(window, 'grecaptcha', {
        configurable: true,
        get() { return _g; },
        set(v) {
            _g = v;
            wrapAllMethods(v, 'root');
            // enterprise may be added later; check on every set and via short polling
            const checkEnterprise = () => {
                if (v && v.enterprise) wrapAllMethods(v.enterprise, 'enterprise');
            };
            checkEnterprise();
            const intv = setInterval(() => {
                if (v && v.enterprise && !v.enterprise.__votetallyWrapped) {
                    wrapAllMethods(v.enterprise, 'enterprise');
                    clearInterval(intv);
                }
            }, 50);
            setTimeout(() => clearInterval(intv), 5000);
        },
    });

    // global click trace
    document.addEventListener('click', (e) => {
        const t = e.target;
        const txt = (t.textContent || '').trim().slice(0, 40);
        log('click', {tag: t.tagName, txt, defaultPrevented: e.defaultPrevented});
    }, true);
})();
"""


def _patch_grecaptcha_now(token: str) -> str:
    """JS to run via page.evaluate immediately before clicking Submit.

    By this point both grecaptcha (v3) and grecaptcha.enterprise have loaded
    on the page (the site needs them; they appear within the first second).
    We replace .execute on each surface with a stub that returns our token.
    Logs a marker so we can verify it actually ran.
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
            // Some sites await ready() before execute; ensure that resolves too.
            const origReady = grecaptcha.enterprise.ready;
            grecaptcha.enterprise.ready = (fn) => {{
                try {{ origReady.call(grecaptcha.enterprise, fn); }} catch (e) {{}}
                fn && fn();
            }};
        }}
        console.log('[votetally] patched: ' + (patched.join(', ') || 'NOTHING — grecaptcha not yet loaded'));
        return patched;
    }})();
    """


@contextmanager
def fetch_signed_url(
    *,
    election_text_re: re.Pattern[str] = DEFAULT_ELECTION_TEXT_RE,
    headless: bool = True,
    captcha_api_key: str | None = None,
    cdp_endpoint: str | None = None,
    with_captcha: bool = False,
) -> Iterator[str]:
    """Yield the signed S3 URL for the active election, opening a browser context.

    Two modes:
    - Default (Firefox launch): mints a 2captcha v3 token, patches grecaptcha,
      drives a fresh headless Firefox. Hits the Bot_Check_Active__c gate on GA
      SoS — currently fails silently before reCAPTCHA runs.
    - cdp_endpoint set: attaches via Patchright to a user-launched Chrome at
      that CDP endpoint and reuses its existing context (real profile, real
      cookies, real cf_clearance). 2captcha is skipped by default — empirical
      result is that the warm session's native reCAPTCHA score suffices.
      Set with_captcha=True to mint a token anyway as a belt-and-braces fallback.
    """
    # CDP mode skips 2captcha by default; spike confirmed warm-Chrome session
    # passes reCAPTCHA on its own. Firefox mode always needs the token.
    should_solve = (cdp_endpoint is None) or with_captcha
    token: str | None = None
    if should_solve and (captcha_api_key or os.environ.get("TWOCAPTCHA_API_KEY")):
        log.info("requesting v3 token from 2captcha (this typically takes 5-30s)")
        token = solve_recaptcha_v3(captcha_api_key)
    elif cdp_endpoint is not None:
        log.info("CDP mode → skipping 2captcha (warm Chrome's native score should suffice)")
    else:
        log.info("no TWOCAPTCHA_API_KEY set — proceeding without token (will likely fail)")

    if cdp_endpoint:
        from patchright.sync_api import sync_playwright as _sp
    else:
        _sp = sync_playwright

    with _sp() as p:
        owns_browser = True
        if cdp_endpoint:
            log.info("attaching to user-launched Chrome via CDP at %s", cdp_endpoint)
            browser = p.chromium.connect_over_cdp(cdp_endpoint)
            if not browser.contexts:
                raise FetchError(
                    f"Chrome at {cdp_endpoint} reports zero contexts. "
                    "Make sure Chrome is launched with "
                    "--remote-debugging-port=9222 --user-data-dir=<dedicated-dir> "
                    "and has at least one tab open."
                )
            owns_browser = False  # don't close the user's Chrome on exit
        else:
            browser = p.firefox.launch(headless=headless)

        console_log: list[str] = []
        aura_log: list[dict] = []
        try:
            page = None
            if cdp_endpoint:
                context = browser.contexts[0]
                # Reuse an already-open SOS tab if one exists. The chrome
                # subcommand opens the right URL on launch, so this is the
                # normal case — saves a navigation (and dodges the new-tab
                # DNS race we saw in spike testing).
                for existing in context.pages:
                    if "mvp.sos.ga.gov" in existing.url:
                        page = existing
                        log.info("reusing existing SOS tab: %s", existing.url)
                        break
            else:
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                    timezone_id="America/New_York",
                )
                # Init scripts only apply to *future* page loads, so they're
                # only useful when we own the context.
                context.add_init_script(_NETWORK_TRACE_SCRIPT)

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

            # Navigate only if we don't already have the right page. In CDP
            # mode with a reused tab, the page is already loaded.
            if "/s/voter-history-files" not in page.url:
                log.info("navigating to %s", SOS_PAGE_URL)
                # `load` not `domcontentloaded` — LWC bundles + Aura init
                # happen after DCL; the form components don't exist yet at
                # that point.
                page.goto(SOS_PAGE_URL, wait_until="load", timeout=45_000)
            else:
                log.info("page already on target URL — skipping navigation")

            log.info("waiting for form to hydrate (LWC bundles must finish)")
            year_combo = page.get_by_role("combobox", name="Election Year")
            try:
                year_combo.wait_for(state="visible", timeout=40_000)
            except TIMEOUT_ERRORS as e:
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
            # Library-agnostic poll — playwright's expect() rejects patchright
            # Locator instances at runtime, so we can't use it across both modes.
            deadline = time.monotonic() + 10
            while not submit_btn.is_enabled():
                if time.monotonic() > deadline:
                    _dump_diagnostics(page, "submit-disabled", console_log, aura_log)
                    raise FetchError(
                        "Submit button never became enabled within 10s. "
                        "Check the diagnostic screenshot — the form may have "
                        "rejected the year/election selection silently."
                    )
                page.wait_for_timeout(200)

            if token is not None:
                log.info("patching grecaptcha.execute right before submit")
                patched = page.evaluate(_patch_grecaptcha_now(token))
                if not patched:
                    log.warning(
                        "grecaptcha not yet loaded at patch time; retrying in 2s"
                    )
                    page.wait_for_timeout(2000)
                    patched = page.evaluate(_patch_grecaptcha_now(token))
                log.info("patched surfaces: %s", patched)
            else:
                log.info("token=None → letting page's own grecaptcha run unpatched")

            log.info("clicking Submit")
            submit_btn.click()

            log.info("waiting for signed S3 URL to appear")
            # Use the locator selector engine instead of raw querySelector —
            # Salesforce LWC renders into Shadow DOM that document.querySelector
            # can't pierce. Playwright/Patchright locators pierce shadow trees.
            s3_link = page.locator(f'a[href*="{S3_HOST}"]').first
            try:
                s3_link.wait_for(state="attached", timeout=60_000)
            except TIMEOUT_ERRORS as e:
                # Capture our network trace before erroring.
                try:
                    trace = page.evaluate("() => window.__votetallyTrace || []")
                    DIAG_DIR.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    Path(DIAG_DIR / f"fetch-fail-trace-{stamp}.json").write_text(
                        json.dumps(trace, indent=2), encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
                # Did the bot-detection banner appear instead?
                err = page.get_by_text("Seems like you are trying to use automated scripts")
                if err.count() > 0:
                    _dump_diagnostics(page, "banner", console_log, aura_log)
                    raise FetchError(
                        "Submit was rejected: site shows the bot-detection banner. "
                        "Likely the 2captcha token was below the site's score threshold."
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
            if owns_browser:
                browser.close()
            else:
                # CDP-attached: leave the user's Chrome alive; close just our tab.
                with suppress(Exception):
                    page.close()
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
    headless: bool = True,
    captcha_api_key: str | None = None,
    cdp_endpoint: str | None = None,
    with_captcha: bool = False,
) -> Path:
    """High-level helper: (optionally solve captcha →) drive form → download zip.

    Returns the path to the downloaded zip. Names it `A-NNNNN.zip` based on
    the URL path so it matches what the Claude for Chrome shortcut produces.
    """
    out_dir = out_dir or Path(tempfile.gettempdir()) / "votetally"
    out_dir.mkdir(parents=True, exist_ok=True)

    with fetch_signed_url(
        election_text_re=election_text_re,
        headless=headless,
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
