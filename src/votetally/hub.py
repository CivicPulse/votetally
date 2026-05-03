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
    # Per-day per-party early-voting totals scraped from the Early Voting
    # (In Person) → "by Party and Date" Qlik sub-tab. Each entry has
    # keys: date (YYYY-MM-DD), democrat, republican, non_partisan, total.
    by_day_party: list[dict] = field(default_factory=list)
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
            "by_day_party": self.by_day_party,
        }

    def to_hub_data(self) -> dict:
        """The subset stored in turnout.json's `hub` field. Drops raw_text."""
        return {
            k: v for k, v in self.to_dict().items()
            if v is not None and v != {} and v != []
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

    Strategy:
      1. If the target county is already in the DOM (Qlik's listbox happened
         to be scrolled to its alphabet range), click it directly.
      2. Otherwise find the listbox's scrollable container by looking up the
         tree from any visible county-shaped row, scroll it to the top, then
         scroll forward 200px at a time until the target appears.

    The earlier version assumed the pane always started near "APPLING…BANKS"
    — true in interactive Chrome where prior selections kept BIBB visible,
    but a fresh Chrome session can land anywhere in the alphabet (e.g.
    "CAMDEN…CHATTAHOOCHEE"), at which point the alphabet anchor regex
    finds nothing and the function silently bails.
    """
    log.info("filtering Qlik to county=%s", county)
    target = county.upper()
    if qlik_frame.get_by_text(target, exact=True).count() > 0:
        try:
            qlik_frame.get_by_text(target, exact=True).first.click(timeout=5_000)
            time.sleep(2.5)
            return True
        except PWTimeout:
            pass

    # Find the scrollable listbox by walking up from any uppercase
    # county-shaped row (a div whose text is ≥3 uppercase letters/spaces
    # only — APPLING, BIBB, CAMDEN, etc.) and reset its scroll to the top.
    # Returns True if a scrollable container was found and scrolled, else
    # False — which means the filter pane structure has changed and the
    # caller should bail.
    reset_scroll_js = (
        "() => {"
        " const isCounty = t => /^[A-Z][A-Z .'-]+$/.test(t)"
        "   && t.length >= 3 && t.length <= 30;"
        " const items = Array.from(document.querySelectorAll('div'))"
        "   .filter(d => isCounty((d.textContent||'').trim())"
        "     && (d.textContent||'').trim().split('\\n').length === 1);"
        " for (const start of items) {"
        "   let el = start;"
        "   while (el && el !== document.body) {"
        "     const cs = getComputedStyle(el);"
        "     if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')"
        "         && el.scrollHeight > el.clientHeight) {"
        "       el.scrollTop = 0; return true;"
        "     }"
        "     el = el.parentElement;"
        "   }"
        " }"
        " return false;"
        "}"
    )
    if not qlik_frame.evaluate(reset_scroll_js):
        log.warning("could not find scrollable filter pane to reset")
        return False
    time.sleep(0.5)

    # Scroll the same container forward 200px at a time. The scroll-step JS
    # walks every county-shaped row until it finds one whose ancestor is a
    # scrollable element, then advances scrollTop. Any candidate works since
    # they all share the listbox container.
    scroll_step_js = (
        "() => {"
        " const isCounty = t => /^[A-Z][A-Z .'-]+$/.test(t)"
        "   && t.length >= 3 && t.length <= 30;"
        " const items = Array.from(document.querySelectorAll('div'))"
        "   .filter(d => isCounty((d.textContent||'').trim())"
        "     && (d.textContent||'').trim().split('\\n').length === 1);"
        " for (const start of items) {"
        "   let el = start;"
        "   while (el && el !== document.body) {"
        "     const cs = getComputedStyle(el);"
        "     if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')"
        "         && el.scrollHeight > el.clientHeight) {"
        "       const before = el.scrollTop;"
        "       el.scrollTop += 200;"
        "       return el.scrollTop !== before;"
        "     }"
        "     el = el.parentElement;"
        "   }"
        " }"
        " return false;"
        "}"
    )
    for attempt in range(80):
        if qlik_frame.get_by_text(target, exact=True).count() > 0:
            try:
                qlik_frame.get_by_text(target, exact=True).first.click(timeout=5_000)
                time.sleep(2.5)
                log.info("county filter applied to %s after %d scroll(s)", target, attempt)
                return True
            except PWTimeout:
                pass
        scrolled = qlik_frame.evaluate(scroll_step_js)
        if not scrolled:
            # Hit the bottom of the list without finding the target.
            log.warning("scrolled to bottom of county list without finding %s", target)
            return False
        time.sleep(0.3)
    log.warning("county %r still not visible after 80 scrolls", target)
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


def _switch_sheet(page, mashup_frame: Frame, button_id: str) -> None:
    """Click a sibling sheet button in the mashup iframe and wait for re-render.

    The mashup's `changeSheet(id, btn)` swaps the qlik-embed's sheet-id which
    causes the inner Qlik iframe URL to change. Patchright's frame tracker
    needs the screenshot-nudge again to notice the new iframe.
    """
    log.info("switching mashup sheet → #%s", button_id)
    try:
        mashup_frame.locator(f"#{button_id}").click(timeout=10_000)
    except PWTimeout as e:
        raise FetchError(f"sheet button #{button_id} not clickable") from e


def _select_chart_sub_tab(qlik_frame: Frame, label: str) -> bool:
    """Click a Qlik chart's sub-tab (e.g. 'by Party and Date'). Returns True
    on success. The tabs render as plain text inside a tab container — clicking
    the matching text label activates it.
    """
    log.info("selecting chart sub-tab: %s", label)
    try:
        qlik_frame.get_by_text(label, exact=True).first.click(timeout=10_000)
        time.sleep(2.5)  # let the chart re-render its bars
        return True
    except PWTimeout:
        log.warning("sub-tab %r not clickable", label)
        return False


def _wait_for_qlik_text(page, marker: str, timeout: float = QLIK_RENDER_TIMEOUT) -> Frame:
    """Poll until SOME qlikcloudgov frame's innerText contains `marker`.

    Used after a sheet switch — the previous frame may still hold the old
    sheet's text briefly. Returns the most recent qlik frame seen so the
    caller can re-extract text from it.
    """
    deadline = time.monotonic() + timeout
    last_qlik_frame: Frame | None = None
    iters = 0
    while time.monotonic() < deadline:
        iters += 1
        if iters % 5 == 0:
            with suppress(Exception):
                page.screenshot(timeout=2_000)
        for fr in _all_frames(page):
            if "qlikcloudgov.com" not in fr.url:
                continue
            last_qlik_frame = fr
            try:
                text = fr.evaluate(
                    "() => document.body && document.body.innerText || ''"
                )
            except Exception:  # noqa: BLE001
                continue
            if marker in text:
                log.info("qlik text marker %r appeared after %d polls", marker, iters)
                return fr
        time.sleep(1.0)
    if last_qlik_frame is not None:
        log.warning(
            "qlik marker %r never appeared in %.0fs (%d polls); "
            "returning latest frame anyway", marker, timeout, iters,
        )
        return last_qlik_frame
    raise FetchError(
        f"No qlikcloudgov.com frame found while waiting for {marker!r}."
    )


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


# Canonical key per Qlik party label. Labels arrive UPPERCASE on the live
# dashboard ("DEMOCRAT", "NON-PARTISAN") but we compare case-insensitively
# and squash dashes/spaces so "NON-PARTISAN", "Non Partisan", and
# "Nonpartisan" all collapse to "non_partisan".
def _party_key(label: str) -> str | None:
    canon = re.sub(r"[\s\-_]+", "", label).upper()
    return {
        "DEMOCRAT": "democrat",
        "REPUBLICAN": "republican",
        "NONPARTISAN": "non_partisan",
    }.get(canon)


def _normalize_date(raw: str) -> str | None:
    """Convert "M/D/YYYY" or "MM/DD/YYYY" to ISO YYYY-MM-DD."""
    parts = raw.strip().split("/")
    if len(parts) != 3:
        return None
    try:
        m, d, y = (int(p) for p in parts)
        return f"{y:04d}-{m:02d}-{d:02d}"
    except ValueError:
        return None


def _parse_by_day_party(text: str) -> list[dict]:
    """Parse the "Early Voting (In Person) → by Party and Date" grouped bar chart.

    Live dashboard text layout (verified 2026-05-03):
      Bar chart \\n
      {date1} \\n {date2} \\n ... \\n {dateN}   # date axis labels, MM/DD/YYYY
      DEMOCRAT \\n REPUBLICAN \\n NON-PARTISAN \\n  # repeats per date
      DEMOCRAT \\n REPUBLICAN \\n               # last date may omit a party
      0 \\n 20 \\n 40 \\n ... \\n {y_max}        # y-axis tick values
      {bar_value_1} \\n ... \\n {bar_value_M}   # M = total party labels above
      *Data as of : ...

    Strategy:
      1. Slice between "Bar chart" and "*Data as of"
      2. Pull all date labels in that slice (chart's x-axis)
      3. Pull party labels (UPPERCASE on the live dashboard)
      4. Group party labels by detecting the cycle restart (each "DEMOCRAT"
         starts a new date group); group sizes can vary per date
      5. The trailing `sum(group_sizes)` numbers in the slice are the bars
    """
    chart_start = text.find("Bar chart")
    if chart_start < 0:
        return []
    chart_end = text.find("*Data as of", chart_start)
    if chart_end < 0:
        chart_end = len(text)
    section = text[chart_start:chart_end]

    date_re = r"\b(\d{1,2}/\d{1,2}/\d{4})\b"
    date_matches = list(re.finditer(date_re, section))
    if len(date_matches) < 2:
        return []
    dates_raw = [m.group(1) for m in date_matches]
    last_date_end = date_matches[-1].end()

    party_re = r"\b(DEMOCRAT|REPUBLICAN|NON[\s\-]?PARTISAN|NONPARTISAN)\b"
    party_matches = [
        m for m in re.finditer(party_re, section, re.IGNORECASE)
        if m.start() >= last_date_end
    ]
    if not party_matches:
        return []

    # Group party labels into per-date buckets. The first label-name in the
    # sequence is the cycle anchor (typically DEMOCRAT on this dashboard).
    anchor = _party_key(party_matches[0].group(1))
    groups: list[list[str]] = [[]]
    for m in party_matches:
        if _party_key(m.group(1)) == anchor and groups[-1]:
            groups.append([])
        groups[-1].append(m.group(1))

    if len(groups) != len(dates_raw):
        log.warning(
            "by_day_party shape mismatch: %d date labels vs %d party groups",
            len(dates_raw), len(groups),
        )
        # Best-effort: pair as many as we have. Trailing dates without
        # groups (or vice versa) are dropped rather than guessed.

    last_party_end = party_matches[-1].end()
    nums_slice = section[last_party_end:]
    nums = re.findall(r"\b(\d[\d,]*)\b", nums_slice)
    n_bars = sum(len(g) for g in groups)
    if len(nums) < n_bars:
        log.warning("expected ≥%d bar numbers, found %d", n_bars, len(nums))
        return []
    bar_values = [_parse_int(n) for n in nums[-n_bars:]]

    out: list[dict] = []
    bar_idx = 0
    for date_raw, party_group in zip(dates_raw, groups, strict=False):
        iso = _normalize_date(date_raw)
        if iso is None:
            bar_idx += len(party_group)
            continue
        rec: dict = {"date": iso, "democrat": 0, "republican": 0, "non_partisan": 0}
        slot_total = 0
        for label in party_group:
            value = bar_values[bar_idx] if bar_idx < len(bar_values) else 0
            bar_idx += 1
            key = _party_key(label)
            if key:
                rec[key] = value
                slot_total += value
        rec["total"] = slot_total
        out.append(rec)
    return out


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


def _scrape_early_voting(
    page, mashup_frame: Frame, *, diag_dir: Path | None = None,
) -> list[dict]:
    """Switch the mashup to Early Voting (In Person) and scrape by_day_party.

    Returns [] on any non-catastrophic failure (no sub-tab, no chart text,
    parser miss). Diagnostic dump is written either way when diag_dir is set,
    so the parser can be tuned against real text.
    """
    _switch_sheet(page, mashup_frame, "EarlyVoting")
    # The qlik-embed swaps sheet-id; the inner iframe re-loads. Wait for a
    # text marker that's specific to the early-voting sheet ("Bar chart" is
    # too generic — present on Total Turnout too). The page header reads
    # "Early Voting" once the new sheet renders.
    qlik_frame = _wait_for_qlik_text(page, "Early Voting")
    time.sleep(3)  # let bars settle

    # Click the "by Party and Date" sub-tab. Order on the dashboard is:
    # by Party | by Party and Date | by Date | by County | Trend Line
    if not _select_chart_sub_tab(qlik_frame, "by Party and Date"):
        log.warning("could not select 'by Party and Date' sub-tab")
        if diag_dir:
            _dump_early_voting_diag(page, qlik_frame, diag_dir, label="no-subtab")
        return []
    time.sleep(2)

    text = qlik_frame.evaluate("() => document.body.innerText")
    if diag_dir:
        diag_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        page.screenshot(
            path=str(diag_dir / f"hub-early-{stamp}.png"), full_page=True,
        )
        (diag_dir / f"hub-early-{stamp}.txt").write_text(text, encoding="utf-8")
        log.info(
            "early-voting diagnostics → %s/hub-early-%s.{png,txt}", diag_dir, stamp,
        )

    parsed = _parse_by_day_party(text)
    log.info("parsed %d days of by_day_party rows", len(parsed))
    return parsed


def _dump_early_voting_diag(
    page, qlik_frame: Frame, diag_dir: Path, *, label: str,
) -> None:
    """Save a screenshot + the early-voting frame's text for parser tuning."""
    diag_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = diag_dir / f"hub-early-{label}-{stamp}"
    with suppress(Exception):
        page.screenshot(path=f"{base}.png", full_page=True)
    with suppress(Exception):
        text = qlik_frame.evaluate("() => document.body.innerText")
        Path(f"{base}.txt").write_text(text, encoding="utf-8")
    log.info("early-voting failure diagnostics saved → %s.{png,txt}", base)


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

            # Poll for the DH.ELECTION2024 iframe instead of assuming it's
            # there after a fixed sleep. In headless Chrome (and sometimes
            # in attached interactive Chrome) this cross-origin S3 iframe
            # appears 5-15s after the load event. Same screenshot-nudge +
            # _all_frames trick as the Qlik wait — page.frames silently
            # caches stale data for cross-origin iframes loaded post-load.
            mashup_frame: Frame | None = None
            mashup_deadline = time.monotonic() + 30.0
            poll = 0
            while time.monotonic() < mashup_deadline:
                poll += 1
                if poll % 5 == 0:
                    with suppress(Exception):
                        page.screenshot(timeout=2_000)
                for fr in _all_frames(page):
                    if "DH.ELECTION2024" in fr.url:
                        mashup_frame = fr
                        break
                if mashup_frame is not None:
                    break
                time.sleep(1.0)
            if mashup_frame is None:
                if diag_dir:
                    _dump_failure(page, diag_dir, "no-mashup-iframe")
                raise FetchError(
                    "DH.ELECTION2024 iframe never loaded within 30s"
                )
            log.info("mashup iframe ready after %d polls", poll)

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
            if county:
                if _select_county(qlik_frame, county):
                    applied_county = county
                else:
                    # Hard fail rather than scrape statewide under a
                    # county-labelled key. Past silent failures here
                    # produced a turnout.json where hub.county was null
                    # but hub.turnout was the statewide number — the
                    # frontend has no way to distinguish that from a
                    # genuine statewide scrape, so we just refuse.
                    if diag_dir:
                        _dump_failure(page, diag_dir, f"county-{county}-not-applied")
                    raise FetchError(
                        f"county filter {county!r} could not be applied "
                        "(filter pane scrolled past the target and the "
                        "scrollable container couldn't be reset). Refusing "
                        "to scrape statewide data under a county key."
                    )

            text = qlik_frame.evaluate("() => document.body.innerText")
            snap = _parse_snapshot(text)
            snap.county = applied_county  # None only when caller passed county=""

            if diag_dir:
                diag_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                page.screenshot(path=str(diag_dir / f"hub-{stamp}.png"), full_page=True)
                (diag_dir / f"hub-{stamp}.txt").write_text(text, encoding="utf-8")
                log.info("diagnostics saved → %s/hub-%s.{png,txt}", diag_dir, stamp)

            # Second pass: switch to the Early Voting (In Person) sheet and
            # scrape the per-day-per-party breakdown. Selections persist on
            # the Qlik app, so the BIBB filter we set on Total Turnout still
            # applies. Best-effort — failures here don't lose the headline.
            try:
                snap.by_day_party = _scrape_early_voting(
                    page, mashup_frame, diag_dir=diag_dir,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("early voting scrape failed (non-fatal): %s", e)

            return snap
        finally:
            with suppress(Exception):
                page.close()
            with suppress(Exception):
                browser.close()
