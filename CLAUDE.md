# VoteTally

A single-page live tracker of Bibb County, Georgia voter turnout for the active election cycle. Static site at `count.bibbunited.com`, fed by a local Python+Chrome scrape pipeline pushing JSON to Cloudflare R2.

See `README.md` for architecture and pipeline details. The full design brief lives in `.impeccable.md`; the section below is the design contract every Claude session in this repo should honor.

## Design Context

### Users

Bibb County, Georgia neighbors — civic-minded but not data professionals. Mobile-first, casual scroll, often arriving from a shared link. The job to be done: **make today's turnout feel real and present**, attribute it cleanly to the official source, and let them move on.

### Brand Personality

Three words: **alive, civic, sober.** Voice is plain, factual, locally-grounded. Names the county, names the source, names the date. Short sentences. No marketing verbs.

### Aesthetic Direction

**Reference:** NYT election-night live page — editorial typography, big numerals treated like a headline, authoritative because the typesetting treats data like news. Numbers ARE the headline.

**Anti-references:**

- **Generic SaaS dashboard** — no hero-metric template, no identical card grids, no Stripe-clone gradients, no glass.
- **Crypto / finance ticker** — no neon-on-black, no monospace-everywhere, no fake live-tickers, no LED-segment fonts.
- **Government-portal stale** — the page is journalism *about* the GA SoS data, not a clone of the source.

**Theme:** light by day, dark at night, via `prefers-color-scheme`. Light keeps the warm cream canvas (`#fafaf7`). Dark is *editorial late-edition* (warm-neutral background tinted toward brand red), not crypto-terminal black. Both modes share the same typographic system; switching themes should feel like the same paper at a different hour.

**Color strategy:** Restrained — tinted neutrals + one committed accent (Georgia state red, `#a32638`) used on under ~10% of surface area. Do not drift into Committed or Drenched without explicit reason.

**Typography:** System UI / Inter stack with `tabular-nums` and `ss01`. Hierarchy through scale and weight contrast, not color. Hero count uses `clamp(3.5rem, 12vw, 7rem)`.

**Brand mark:** red rounded square with three white vertical bars. Doubles as logo and as a literal tally affordance. Don't replace it casually.

**Motion:** the page must feel alive, but only through *kinetic typesetting*, never ticker-bar pastiche. The total count-ups (ease-out-quart, no bounce) when fresh data arrives. The "updated X ago" line is first-class. Charts ease in from data, never from layout. Never animate `width`/`height`/`top`/`left`.

### Design Principles

1. **The number is the headline.** Type the total like a front-page numeral, not a SaaS KPI.
2. **Freshness is part of the brand.** The "as of" timestamp earns the same care as the count.
3. **One accent, used precisely.** Georgia red appears on the count, the brand mark, and one or two surgical highlights — never on backgrounds, gradients, or chrome.
4. **Editorial, not dashboard.** Spacing, hairline rules, and type hierarchy do the work. Cards used sparingly, never as default containers, never nested.
5. **Mobile-first, glance-optimized.** The headline number must be readable above the fold on a 375px viewport.
6. **Civic before product.** No engagement loops, no email-capture, no related-content carousels. Anything that smells like growth-hacking is wrong here.

### Out-of-scope

- Multi-county comparison, statewide rollups, historical drill-downs.
- User accounts, saved views, sharing widgets beyond OG metadata.
- "Predict the winner" / partisan framing.
