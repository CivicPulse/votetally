"""Election Data Hub scraper — pull live turnout numbers from the Qlik dashboard.

The voter-history zip file lags real time by 1+ days. The Election Data Hub
at https://sos.ga.gov/page/election-data-hub-unofficial-turnout shows
near-real-time numbers in a Qlik Sense Cloud Gov dashboard. Same CDP-attach
trick we use for the SOS form works here — drive the user's real Chrome,
click "Go Interactive", wait for Qlik to render, scrape the values out of
the analysis frame's text.

Architecture:
- sos.ga.gov page → embedded iframe to a static S3 mashup
- mashup → after "Go Interactive" click, opens Qlik Cloud Gov tenant in nested iframe
- Qlik renders into the deepest iframe at sos-ga-gov.us.qlikcloudgov.com/sense/app/...
- We extract visible text from that frame and regex-parse it
"""
from __future__ import annotations

import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from patchright.sync_api import Frame, sync_playwright
from patchright.sync_api import TimeoutError as PWTimeout

from votetally.fetcher import DEFAULT_CDP_ENDPOINT, FetchError

HUB_URL = "https://sos.ga.gov/page/election-data-hub-unofficial-turnout"
GO_INTERACTIVE_BTN = "#goInteract"
QLIK_RENDER_TIMEOUT = 120.0  # seconds — Qlik anonymous token + bundle load is slow

# Patterns to extract from the rendered Qlik analysis frame's innerText.
# Each KPI is rendered as a label followed by a number; the labels are stable
# even though the surrounding DOM structure isn't.
# Numbers may include commas (e.g. "2,235") or decimal percentages ("2.1%").
NUMBER_RE = r"([\d,]+)"
PERCENT_RE = r"([\d.]+%)"

log = logging.getLogger(__name__)


@dataclass
class HubSnapshot:
    """Structured snapshot scraped from the dashboard."""
    scraped_at: str
    county: str | None = None
    data_as_of: str | None = None
    turnout: int | None = None
    active_voters: int | None = None
    turnout_pct: float | None = None
    by_race: dict[str, int] = field(default_factory=dict)
    raw_text: str = ""

    def to_dict(self) -> dict:
        return {
            "scraped_at": self.scraped_at,
            "county": self.county,
            "data_as_of": self.data_as_of,
            "turnout": self.turnout,
            "active_voters": self.active_voters,
            "turnout_pct": self.turnout_pct,
            "by_race": self.by_race,
        }


def _parse_int(s: str) -> int:
    return int(s.replace(",", ""))


def _find_qlik_render_frame(page) -> Frame | None:
    """Among all frames, return the one Qlik renders the analysis sheet into."""
    for fr in page.frames:
        if "qlikcloudgov.com" in fr.url and "/sense/app/" in fr.url and "/state/analysis" in fr.url:
            return fr
    return None


def _select_county(qlik_frame: Frame, county: str) -> bool:
    """Filter Qlik to the given county. Returns True on success, False otherwise.

    Strategy: scroll the filter pane (a virtualized list) until the target
    county is in view, then click it. Avoids the "focus click selects a
    county" trap that the press_sequentially approach hit.
    """
    log.info("filtering Qlik to county=%s", county)
    target = county.upper()
    # First check if it's already visible (no scroll needed for nearby counties).
    if qlik_frame.get_by_text(target, exact=True).count() > 0:
        try:
            qlik_frame.get_by_text(target, exact=True).first.click(timeout=5_000)
            time.sleep(2.5)
            return True
        except PWTimeout:
            pass

    # Scroll the County filter's virtualized list. Qlik renders filter panes
    # with a virtual-scroll container; we scroll within it via JS until the
    # target text appears.
    scroll_js = (
        "() => { const re = /^(APPLING|ATKINSON|BACON|BAKER|BALDWIN|BANKS)$/;"
        " const items = Array.from(document.querySelectorAll('div'))"
        " .filter(d => re.test(d.textContent.trim()));"
        " if (!items.length) return false;"
        " let el = items[0];"
        " while (el && el !== document.body) {"
        "   const cs = getComputedStyle(el);"
        "   if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')"
        "       && el.scrollHeight > el.clientHeight) {"
        "     el.scrollTop += 200; return true;"
        "   }"
        "   el = el.parentElement;"
        " }"
        " return false; }"
    )
    for attempt in range(40):
        scrolled = qlik_frame.evaluate(scroll_js)
        if not scrolled:
            log.warning("could not find scrollable filter pane")
            return False
        time.sleep(0.3)
        if qlik_frame.get_by_text(target, exact=True).count() > 0:
            try:
                qlik_frame.get_by_text(target, exact=True).first.click(timeout=5_000)
                time.sleep(2.5)
                log.info("county filter applied to %s after %d scroll(s)", target, attempt + 1)
                return True
            except PWTimeout:
                continue
    log.warning("county %r never appeared in filter list after scrolling", target)
    return False


def _dump_failure(page, diag_dir: Path, label: str) -> None:
    """Save screenshot + per-frame text dump for a failed render."""
    diag_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = diag_dir / f"hub-fail-{label}-{stamp}"
    with suppress(Exception):
        page.screenshot(path=f"{base}.png", full_page=True)
    frames_info = []
    for i, fr in enumerate(page.frames):
        info = {"i": i, "url": fr.url}
        try:
            info["text_preview"] = fr.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 500)"
            )
        except Exception as e:  # noqa: BLE001
            info["text_preview"] = f"<error: {e}>"
        frames_info.append(info)
    import json as _json
    Path(f"{base}.frames.json").write_text(_json.dumps(frames_info, indent=2))
    log.warning("failure diagnostics saved → %s.{png,frames.json}", base)


def _all_frames(page) -> list[Frame]:
    """Walk the frame tree explicitly. `page.frames` caches stale data for
    cross-origin iframes loaded after page open; child_frames doesn't."""
    out: list[Frame] = []
    seen: set[int] = set()

    def walk(fr: Frame) -> None:
        if id(fr) in seen:
            return
        seen.add(id(fr))
        out.append(fr)
        try:
            for child in fr.child_frames:
                walk(child)
        except Exception:  # noqa: BLE001
            pass

    walk(page.main_frame)
    return out


def _wait_for_qlik(page, timeout: float = QLIK_RENDER_TIMEOUT) -> Frame:
    """Poll until a Qlik frame contains the rendered KPI text ("Turnout")."""
    deadline = time.monotonic() + timeout
    last_qlik_frame: Frame | None = None
    iters = 0
    while time.monotonic() < deadline:
        iters += 1
        # Forcing a screenshot once per poll seems to nudge Patchright to
        # refresh its cross-origin frame tracking (we discovered this the
        # hard way during the spike — page.frames stays stale otherwise).
        if iters % 5 == 0:
            with suppress(Exception):
                page.screenshot(timeout=2_000)
        frames = _all_frames(page)
        if iters % 5 == 1:
            log.info("poll %d: %d frames %s", iters, len(frames),
                     [f.url[:60] for f in frames])
        for fr in frames:
            if "qlikcloudgov.com" not in fr.url:
                continue
            last_qlik_frame = fr
            try:
                text = fr.evaluate(
                    "() => document.body && document.body.innerText || ''"
                )
            except Exception:  # noqa: BLE001
                continue
            if "Turnout" in text and re.search(r"\d{1,3}(?:,\d{3})", text):
                log.info("qlik render detected after %d polls", iters)
                return fr
        time.sleep(1.0)
    if last_qlik_frame is not None:
        log.warning(
            "qlik frame existed but KPI text never appeared in %.0fs (%d polls)",
            timeout, iters,
        )
        return last_qlik_frame
    raise FetchError(
        f"No qlikcloudgov.com frame appeared within {timeout:.0f}s. "
        "Possible causes: anonymous-token rate limit, transient Qlik tenant "
        "issue, or DH page state change."
    )


def _parse_snapshot(text: str) -> HubSnapshot:
    """Pull KPIs out of the analysis frame's visible text.

    The Qlik dashboard's text layout (in order top-to-bottom, left-to-right):
      Turnout
      {N}
      Active Voters
      {N}
      Turnout %
      {N.N}%
    Race/Ethnicity bars: {Race}\n{N}.
    Footer: *Data as of: {timestamp}
    """
    snap = HubSnapshot(scraped_at=datetime.now().isoformat(timespec="seconds"))
    snap.raw_text = text

    # KPI cards — label appears immediately before the number on its own line.
    for label, attr, parser in [
        ("Turnout", "turnout", _parse_int),
        ("Active Voters", "active_voters", _parse_int),
    ]:
        m = re.search(rf"{re.escape(label)}\s*\n\s*{NUMBER_RE}", text)
        if m:
            with suppress(ValueError):
                setattr(snap, attr, parser(m.group(1)))

    m = re.search(rf"Turnout %\s*\n\s*{PERCENT_RE}", text)
    if m:
        with suppress(ValueError):
            snap.turnout_pct = float(m.group(1).rstrip("%"))

    # Race/ethnicity bar chart text layout:
    #   Bar chart \n No title \n {labels...} \n {axis values...} \n 0 \n {bar values...}
    # Take the slice AFTER the last race label and BEFORE "*Data as of",
    # then assume the trailing N numbers are the bar values (axis values
    # come first in that slice; bars come last).
    race_pattern = (
        r"^(White|Black|Other/Unknown|Hispanic/Latino"
        r"|Asian/Pacific Islander|American Indian or Alaska\S*)$"
    )
    race_labels = re.findall(race_pattern, text, flags=re.MULTILINE)
    if race_labels:
        last_label_end = max(
            m.end() for m in re.finditer(race_pattern, text, flags=re.MULTILINE)
        )
        end_marker = text.find("*Data as of", last_label_end)
        slice_ = text[last_label_end:end_marker if end_marker > 0 else None]
        nums = re.findall(r"\b(\d[\d,]*)\b", slice_)
        if len(nums) >= len(race_labels):
            for label, raw in zip(
                race_labels, nums[-len(race_labels):], strict=False,
            ):
                with suppress(ValueError):
                    snap.by_race[label] = _parse_int(raw)

    # Data freshness timestamp.
    m = re.search(r"Data as of\s*:\s*([\d/]+\s+[\d:]+\s*(?:AM|PM)?)", text, re.IGNORECASE)
    if m:
        snap.data_as_of = m.group(1).strip()

    return snap


def fetch_hub_snapshot(
    *,
    cdp_endpoint: str = DEFAULT_CDP_ENDPOINT,
    county: str | None = "BIBB",
    election_date: str | None = "05/19/2026",
    diag_dir: Path | None = None,
) -> HubSnapshot:
    """Drive the Election Data Hub via the user's running Chrome → return snapshot.

    The dashboard's default state is statewide + latest election. To get a
    county-filtered view, pass `county`; to lock to a specific election, pass
    `election_date`. Both are optional — defaults match what the user sees.
    """
    log.info("attaching to Chrome at %s", cdp_endpoint)
    with sync_playwright() as p:
        browser = None
        deadline = time.monotonic() + 15
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                browser = p.chromium.connect_over_cdp(cdp_endpoint)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1.0)
        if browser is None:
            raise FetchError(
                f"Could not connect to Chrome at {cdp_endpoint}. "
                "Run `votetally chrome --launch` first."
            ) from last_err

        if not browser.contexts:
            raise FetchError(f"Chrome at {cdp_endpoint} has no contexts.")
        context = browser.contexts[0]

        # Always open a fresh tab for the hub — reuse risks stale Qlik state.
        page = context.new_page()
        try:
            log.info("navigating to %s", HUB_URL)
            page.goto(HUB_URL, wait_until="load", timeout=45_000)
            time.sleep(3)  # let static page settle

            # Click "Go Interactive" inside the mashup iframe
            mashup_frame = next(
                (f for f in page.frames if "DH.ELECTION2024" in f.url), None
            )
            if mashup_frame is None:
                raise FetchError("DH.ELECTION2024 iframe never loaded")

            log.info("clicking Go Interactive")
            try:
                mashup_frame.locator(GO_INTERACTIVE_BTN).click(timeout=10_000)
            except PWTimeout as e:
                raise FetchError("Go Interactive button not found / not clickable") from e

            log.info("waiting up to %ss for Qlik to render", QLIK_RENDER_TIMEOUT)
            try:
                qlik_frame = _wait_for_qlik(page)
            except FetchError:
                if diag_dir:
                    _dump_failure(page, diag_dir, "no-qlik-frame")
                raise
            time.sleep(3)  # let chart numbers settle into final positions

            applied_county = None
            if county and _select_county(qlik_frame, county):
                applied_county = county

            text = qlik_frame.evaluate("() => document.body.innerText")
            snap = _parse_snapshot(text)
            snap.county = applied_county  # None means statewide

            if diag_dir:
                diag_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                page.screenshot(path=str(diag_dir / f"hub-{stamp}.png"), full_page=True)
                (diag_dir / f"hub-{stamp}.txt").write_text(text, encoding="utf-8")
                log.info("diagnostics saved → %s/hub-%s.{png,txt}", diag_dir, stamp)

            return snap
        finally:
            with suppress(Exception):
                page.close()
            with suppress(Exception):
                browser.close()
