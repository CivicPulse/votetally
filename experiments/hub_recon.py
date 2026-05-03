"""Recon: what does the GA SoS Election Data Hub page look like to a scraper?

Connects to user-launched Chrome, opens the hub page, dumps:
- iframe URLs (the Qlik dashboard is likely embedded)
- network requests during load (find the data API)
- DOM searches for known visible values
- screenshots of the rendered hub
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from patchright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"
HUB_URL = "https://sos.ga.gov/page/election-data-hub-unofficial-turnout"
OUT_DIR = Path(__file__).parent / "artifacts" / f"hub-recon-{datetime.now():%Y%m%d-%H%M%S}"

KNOWN_VALUES = ["2,235", "2,154", "105,326", "1,319", "BIBB", "Ballots Accepted"]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"writing to {OUT_DIR}")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        context = browser.contexts[0]
        page = context.new_page()

        network: list[dict] = []

        def on_request(req):
            network.append({
                "phase": "request",
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
            })

        def on_response(resp):
            entry = {
                "phase": "response",
                "method": resp.request.method,
                "url": resp.url,
                "status": resp.status,
                "content_type": resp.headers.get("content-type", ""),
            }
            # Capture small JSON bodies for inspection
            if "json" in entry["content_type"].lower() and resp.status == 200:
                try:
                    body = resp.text()
                    if len(body) < 50_000:
                        entry["body"] = body
                except Exception:  # noqa: BLE001
                    pass
            network.append(entry)

        page.on("request", on_request)
        page.on("response", on_response)

        websockets: list[dict] = []

        def on_ws(ws):
            print(f"  websocket: {ws.url}")
            websockets.append({"url": ws.url})
            ws.on("framereceived", lambda payload: websockets.append(
                {"url": ws.url, "dir": "recv", "size": len(payload)}
            ))
            ws.on("framesent", lambda payload: websockets.append(
                {"url": ws.url, "dir": "sent", "size": len(payload)}
            ))

        page.on("websocket", on_ws)

        print(f"navigating to {HUB_URL}")
        page.goto(HUB_URL, wait_until="load", timeout=45_000)
        print("loaded; waiting 4s for static page to settle")
        time.sleep(4)

        # Click "Go Interactive" inside the iframe — that's what triggers the
        # actual Qlik connection. The button id is goInteract per index.html.
        qlik_frame = None
        for fr in page.frames:
            if "DH.ELECTION2024" in fr.url:
                qlik_frame = fr
                break
        if qlik_frame is None:
            print("WARNING: did not find DH.ELECTION2024 iframe")
        else:
            print(f"clicking Go Interactive inside frame {qlik_frame.url}")
            try:
                qlik_frame.locator("#goInteract").click(timeout=5_000)
                print("clicked; polling for Qlik chart elements (up to 45s)")
                # Qlik renders chart objects with class qv-object. Wait for any.
                deadline = time.monotonic() + 45
                qlik_render_frame = None
                while time.monotonic() < deadline:
                    for fr in page.frames:
                        if "qlikcloudgov.com" not in fr.url:
                            continue
                        try:
                            count = fr.locator(".qv-object").count()
                        except Exception:  # noqa: BLE001
                            continue
                        if count > 0:
                            qlik_render_frame = fr
                            print(f"  found {count} qv-objects in {fr.url[:80]}")
                            break
                    if qlik_render_frame:
                        break
                    time.sleep(1)
                if qlik_render_frame is None:
                    print("  no qv-object elements appeared in 45s")
                else:
                    # Wait a bit more for value text to settle
                    time.sleep(5)
                    # Grab the inner HTML of every qv-object for inspection
                    objs = qlik_render_frame.locator(".qv-object").all()
                    summary = []
                    for i, obj in enumerate(objs):
                        try:
                            txt = obj.inner_text(timeout=1_000)[:300]
                        except Exception as e:  # noqa: BLE001
                            txt = f"(error: {e})"
                        summary.append({"i": i, "text": txt})
                    Path(OUT_DIR / "qv-objects.json").write_text(
                        json.dumps(summary, indent=2)
                    )
                    print(f"  dumped {len(summary)} qv-object text blocks")
            except Exception as e:  # noqa: BLE001
                print(f"go-interactive click failed: {e}")

        page.screenshot(path=str(OUT_DIR / "hub.png"), full_page=True)
        Path(OUT_DIR / "hub.html").write_text(page.content(), encoding="utf-8")
        Path(OUT_DIR / "websockets.json").write_text(json.dumps(websockets, indent=2))

        # Dump every frame's content separately so we can find the value selectors
        frame_dir = OUT_DIR / "frames"
        frame_dir.mkdir(exist_ok=True)
        for i, fr in enumerate(page.frames):
            try:
                content = fr.content()
            except Exception as e:  # noqa: BLE001
                content = f"<!-- error: {e} -->"
            url_short = fr.url.split("//")[-1][:60].replace("/", "_")
            (frame_dir / f"{i:02d}-{url_short}.html").write_text(content, encoding="utf-8")
        print(f"  dumped {len(page.frames)} frame contents → frames/")

        frames = [{"url": f.url, "name": f.name} for f in page.frames]
        Path(OUT_DIR / "frames.json").write_text(json.dumps(frames, indent=2))
        print(f"frames ({len(frames)}):")
        for f in frames:
            print(f"  {f['name'] or '(top)'}: {f['url']}")

        # Search every frame's DOM for our known values
        hits: dict[str, list[dict]] = {v: [] for v in KNOWN_VALUES}
        for fr in page.frames:
            try:
                content = fr.content()
            except Exception as e:  # noqa: BLE001
                print(f"  frame content error ({fr.url}): {e}")
                continue
            for v in KNOWN_VALUES:
                if v in content:
                    hits[v].append({"frame_url": fr.url, "len": len(content)})
        Path(OUT_DIR / "value-hits.json").write_text(json.dumps(hits, indent=2))
        print("known-value hits:")
        for v, found in hits.items():
            mark = "✓" if found else "·"
            print(f"  {mark} {v!r}: {len(found)} frame(s)")

        Path(OUT_DIR / "network.json").write_text(json.dumps(network, indent=2))
        print(f"captured {len(network)} network events → network.json")

        page.close()


if __name__ == "__main__":
    main()
