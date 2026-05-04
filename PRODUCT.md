# VoteTally — Design Context

A live tracker of Georgia voter turnout for the active election cycle (currently the 2026 General Primary). The flagship page tracks Bibb County at `count.kerryhatcher.com`; sibling pages exist for neighboring counties at `/<county>/` (currently only `/crawford/`). Each page is a single-county tracker — same design, same JSON shape, separate R2 object per county. Static site fed by a local Python+Chrome scrape pipeline pushing per-county JSON to Cloudflare R2.

**Register:** brand. The page IS the product — there is no app surface behind it. Design choices should be committed and identity-strong, not generic-product chrome.

**Architecture:** *single-county per page, multi-instance.* Adding a county adds a sibling page (`/<county>/`) and a sibling JSON object (`turnout-<county>.json`); it never adds a county selector, comparison view, or shared dashboard. Each page belongs to its county, full stop. This keeps "the number is the headline" working for every county and avoids drifting into a multi-county product.

## Design Context

### Users

Bibb County, Georgia neighbors — civic-minded but not data professionals. They land on the page during early-voting season to glance at a number: *how many of us have voted so far?* Mobile-first, casual scroll, often arriving from a shared link or text message. They are not staying long. They are not comparing tabs. The page either gives them a confident, alive number in the first second or it has failed.

The job to be done: **make today's turnout feel real and present**, attribute it cleanly to the official source, and let them move on.

### Brand Personality

Three words: **alive, civic, sober.**

- *Alive* — the count is happening right now; the page should feel that way. Freshness is part of the brand.
- *Civic* — this is Bibb County's number, not a product's KPI. It belongs to the place.
- *Sober* — no theatrics, no partisan loading, no engagement bait. Trustworthy because it doesn't try too hard.

Voice: plain, factual, locally-grounded. Names the county, names the source, names the date. Short sentences. No marketing verbs.

### Aesthetic Direction

**Reference:** sister site to [bibbunited.com](https://www.bibbunited.com/) — same county, same dual-color civic identity (navy + crimson), same Barlow Condensed display + Source Serif body system, same light "Community" / dark "Urgent" mode pattern. count.kerryhatcher.com should read as a child of that family: editorial typography, big condensed numerals treated like a headline, authoritative because the typesetting treats the data like news. Numbers ARE the headline.

**Anti-references** (do not let the design drift into these):

- **Generic SaaS dashboard** — no hero-metric template (giant number / small label / 4-up supporting stats / gradient accent), no identical card grids, no Stripe-clone gradients, no glass.
- **Crypto / finance ticker** — no neon-on-black, no monospace-everywhere terminal pastiche, no fake live-tickers, no Bloomberg-orange.
- **Government-portal stale** — implied, not stated by the user, but worth naming. The data is from the GA SoS; the page should read as journalism *about* that data, not as a clone of the source.

**Theme:** light by day, dark at night, via `prefers-color-scheme`. A `?theme=light|dark` URL flag overrides the OS preference for visual testing.

- **Light ("Community"):** white canvas (`#ffffff`), slate ink (`#111827`), navy (`#1b2a4a`) for civic-identity moments (kickers, secondary chrome), crimson (`#dc2626`, Tailwind red-600) for the count and section-heading rules. Mirrors bibbunited.com defaults exactly.
- **Dark ("Urgent"):** slate-900 canvas (`#0f172a`), slate-800 surfaces (`#1e293b`), slate-50 ink (`#f8fafc`), red-500 lifted accent (`#ef4444`). Same canvas family bibbunited.com uses for its `[data-mode="urgent"]` state. Not warm-neutral, not "editorial late-edition" — clean cool slate, deliberately echoing the sister site.
- Both modes share the same typographic system and the same `data-theme` swap mechanism (mirrors bibbunited's `data-mode`). Switching themes should feel like the same civic site under a different headline pressure.

**Color strategy:** Two-color brand identity per the bibbunited family — **navy (`#1b2a4a`)** as the civic anchor, **crimson (`#dc2626` light / `#ef4444` dark)** as the live-data accent. Both are surgical, not drenched: navy lands on kickers, headers, and structural moments; crimson lands on the count, the brand mark, and section-heading underlines. Neither color appears as a background or gradient. The accent surface area still stays under ~15% combined.

**Typography:** **Barlow Condensed** (display, 600/700, uppercase for h1/h2 and the kicker) for headings and the hero numeral; **Source Serif 4** (body, 400/600/700 + italic) for body copy and captions. Loaded from Google Fonts with `display=swap`. `tabular-nums` (via `font-feature-settings: "tnum"`) is preserved on the hero `.total` so the count-up animation doesn't reflow digit widths. Hero scale stays at `clamp(3.5rem, 12vw, 7rem)`.

**Brand mark:** rounded red square with three white vertical bars (favicon). Doubles as logo and as a literal tally / bar-chart affordance. Don't replace it casually.

**Motion (this is where urgency lives):** the page must feel alive, but only through *kinetic typesetting*, never through ticker-bar pastiche. Specifically:

- The total count animates from prior value to new value when fresh data arrives (count-up, ease-out-quart, no bounce).
- The "updated X minutes ago" line is a first-class element, not a footer afterthought — it's the proof that the number is live.
- Charts ease in from data, never from layout. Never animate `width`/`height`/`top`/`left`.
- No marquees. No fake live-tickers. No LED-segment fonts. No pulsing dots that aren't tied to real status changes.

### Design Principles

1. **The number is the headline.** Type the total like a late-edition front-page numeral in Barlow Condensed, not like a SaaS KPI. Everything else on the page is subordinate to it.

2. **Freshness is part of the brand.** The "as of" timestamp earns the same care as the count itself. If the page can't prove it's live, it has failed at urgency. Stale data must read as stale, never as current.

3. **Two colors, used precisely.** Navy is the civic anchor (kickers, structural chrome); crimson is the live-data accent (the count, the brand mark, section-heading underlines, the freshness badge, the chart's primary series). Neither color appears on backgrounds, gradients, or decorative chrome. Inherits bibbunited.com's restraint: two committed colors do more than a full palette.

4. **Editorial, not dashboard.** Spacing, hairline rules, and typographic hierarchy do the work. Cards are used sparingly and only when they're the best affordance — never as a default container, never nested, never decorated with side-stripe borders or gradient accents.

5. **Mobile-first, glance-optimized.** Most visits are one-handed phone scrolls from a text link. The headline number must be readable and complete above the fold on a 375px viewport. Charts may scroll; the number may not.

6. **Civic before product.** No analytics-driven engagement loops, no email-capture, no related-content carousels. The page belongs to the county. Anything that smells like growth-hacking is wrong here.

### Out-of-scope (so it stays that way)

- Multi-county comparison, statewide rollups, historical drill-downs. Those are different products. Sibling county pages (Bibb, Crawford, …) are separate instances of the same single-county tracker — never a unified view.
- Cross-links between sibling county pages. Each page is isolated; a Crawford visitor who wants Bibb has to know the URL. This is deliberate — adding a county switcher would whisper "multi-county product" and undermine the editorial single-county posture.
- User accounts, saved views, sharing widgets beyond OG metadata. Already shareable; resist adding more.
- "Predict the winner" / partisan framing. Turnout is non-partisan by construction; the page must stay that way.
