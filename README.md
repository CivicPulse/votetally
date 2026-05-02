# votetally

Bibb County voter turnout tracker for the active GA election.

## Architecture

```
Claude for Chrome  →  ~/Downloads/A-NNNNN.zip
"download-voter-history" shortcut         │
                                          ▼
                              ┌────────────────────────┐
                              │ votetally (Python CLI) │
                              │  parse CSV             │
                              │  filter Bibb           │
                              │  merge into turnout    │
                              └────────────┬───────────┘
                                           ▼
                              ┌────────────────────────┐
                              │ Cloudflare R2 (public) │
                              │  turnout.json          │
                              │  archive/<id>/<ts>.json│
                              └────────────┬───────────┘
                                           ▼
                              ┌────────────────────────┐
                              │ Cloudflare Pages       │
                              │  static site, no Worker│
                              └────────────────────────┘
```

The scraper layer is **local** because the GA SOS site is gated by Google reCAPTCHA v3 Enterprise that blocks all server-side automation we tried (HTTP replay and Firecrawl). A logged-in real Chrome session passes the score check, so we use Claude for Chrome's saved-prompt feature to drive the form. The Python pipeline picks up the resulting zip, processes it, and uploads to R2.

## Setup

```bash
# 1. Install (uv-managed, Python 3.11+)
uv sync

# 2. Configure R2 credentials
cp .env.example .env
$EDITOR .env

# 3. Verify R2 connection
uv run votetally r2-init
```

## Use

```bash
# Process a downloaded zip and print the snapshot (no upload)
uv run votetally process ~/Downloads/A-12599.zip

# Full pipeline: process and push to R2
uv run votetally upload ~/Downloads/A-12599.zip

# Watch ~/Downloads for new election zips and run the pipeline automatically
uv run votetally watch ~/Downloads

# Check the current state in R2
uv run votetally status
```

## Trigger options

The watcher gives you "drop a zip in Downloads → it processes." The Claude for Chrome shortcut produces those zips. To fully automate (every 6h), wire one of:

- **Recommended:** `claude -p "execute the download-voter-history shortcut"` on cron, in parallel with `votetally watch ~/Downloads` running as a systemd user service.
- **Manual cadence:** click the shortcut in Chrome whenever you remember; the watcher takes care of the rest.

## File layout produced by GA SOS

The zip contains a single CSV named `<election-id>.csv` (e.g. `A-12599.csv`) with header row:

```
County Name, Voter Registration Number, Election Date, Election Type,
Party, Ballot Style, Absentee, Provisional, Supplemental
```

`Ballot Style` values seen: `EARLY IN-PERSON`, `ABSENTEE BY MAIL`, `ELECTRONIC BALLOT DELIVERY`. Election-day rows appear after the election date passes.

The website still documents an old fixed-width layout — that is **stale**; the actual file is comma-delimited.

## Deploy the dashboard to Cloudflare Pages

The static site is `site/`. R2 holds the JSON. The frontend fetches
`./turnout.json` (path-relative); the `_redirects` file proxies that
request to the R2 bucket server-side so there is no CORS to configure.

```bash
npx wrangler pages deploy site/ --project-name=votetally --branch=main
```

Live at `https://votetally.pages.dev`. Refreshes automatically whenever
the local pipeline pushes a new `turnout.json` to R2 (cache-control 5min).

### Local preview

`site/turnout.json` is gitignored — the production deploy must not include
it (a static file would shadow the `_redirects` proxy). To preview locally:

```bash
uv run votetally dump-shape > site/turnout.json   # generate sample data
cd site && python3 -m http.server 8765            # serve at :8765
```

## Development

```bash
uv run pytest -v        # 9 tests; the parser test uses ~/Downloads/A-12599.zip as ground truth
uv run ruff check src/  # lint
```
