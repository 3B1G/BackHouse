# Market scout (Mk)

A uAgent that checks how our dish prices sit against **relevant** nearby competitors and reports a status per dish:
`below_market`, `at_par` (within ±5% of the median), `above_market` or `insufficient_data` (fewer than 3 prices).
It does **not** decide on a price change. ASI makes that call with the Forecaster's numbers, and CH applies it once
the owner approves.

## Setup

From `nessie_lab/`:

```bash
uv sync --project market_scout          # also installs ../cost_price (editable path dependency)
uv run --project market_scout pytest market_scout/tests -q
```

The scout reads and writes `data/cost_price.db` through the `cost_price` package (build it once with
`uv run --project cost_price cost-price build`, after `generate.py` and `build_book.py`), and loads the shared
`contracts.py` from `nessie_lab/`. Optional keys, in `nessie_lab/.env` or `BackHouse/.env` (see `.env.example`):

- `ASI1_API_KEY`: also read menus with ASI1. Without it, only the rule reader and the seed list are used.
- `APIFY_TOKEN`: also find competitors on Google Maps (`compass/crawler-google-places`). Without it, only the
  competitors already in the database are used.
- `MARKET_SCOUT_SEED`: required for `serve`.
- `MARKET_SCOUT_ALLOWED_SENDERS`: comma-separated agent addresses `serve` takes requests from. Without it, `serve`
  refuses to start unless you pass `--local-only`, which accepts only this machine's local demo client.
- `MARKET_SCOUT_LOCAL_CLIENT_SEED`: the local demo client's seed. Unset = a random seed generated on first use and
  kept in `data/market_scout/local_client_seed` (owner-only permissions on POSIX; the directory has its own
  `.gitignore`). There is no seed in the source any more: a published seed let anyone sign as the allowed client.
- `MARKET_SCOUT_BIND_HOST`: where `serve` and `client` listen (default `127.0.0.1`). uagents 0.26.1 has no host
  option (the `Agent` constructor takes only a port, and `uagents/asgi.py` binds the module constant
  `HOST = "0.0.0.0"`), so the scout sets `uagents.asgi.HOST` before starting. In 0.26.1 that constant is read only
  inside `ASGIServer.serve()` (when it builds the uvicorn config), which is the only server an Agent or Bureau
  starts, so the patch takes effect. `test_effective_bind_host_after_make_agent_is_localhost` runs that `serve()` with
  uvicorn stubbed and checks the host. Re-check this on a uagents upgrade.

## Run

```bash
uv run --project market_scout market-scout run --request-id demo-1            # all dishes, dinner
uv run --project market_scout market-scout run --request-id demo-2 --items "Margherita Pizza" "Lobster Ravioli"
uv run --project market_scout market-scout run --request-id demo-3 --refresh  # re-fetch menus saved more than 6 h ago
uv run --project market_scout market-scout run --request-id demo-4 --as-of 2026-11-01   # our menu on that date
uv run --project market_scout market-scout show data/market_reports/M-<id>.json
uv run --project market_scout market-scout serve --local-only                 # uAgent on 127.0.0.1:8003
uv run --project market_scout market-scout client --target agent1... --request-id chat-1
```

Chat request (JSON in a ChatMessage): `{"request_id": "...", "items": [], "service": "dinner", "as_of": null,
"refresh": false, "par_band": 0.05}`. `request_id` is 1-64 of `A-Z a-z 0-9 . _ -`; at most 50 items of 1-80
characters. Reply: `{"request_id", "report_id", "filename", "status": "ok", "positions": {dish: position},
"upcoming": {dish: {"price_cents", "from", "position"} and/or {"leaving_from"}}}` (`upcoming` lists only dishes
with an approved change after the scan date; `positions` is always the menu on that date). The full
report is at `filename` (`MarketReport` in `src/market_scout/contracts.py`). Its `summaries` are the shared
`MarketSummary` and its `prices` are the shared `CompetitorPrice`. The same sender and `request_id` always get the same
report back.

Other replies: `"status": "error"` (bad request, unknown dish, sender not on the allowlist), `"rate_limited"` (more
than 5 requests from one sender in 10 minutes) and `"busy"` (a scan is already running; it is not queued, ask again).

## How a price gets in

1. **Our menu**: `cost_price.api.menu` on the scan date: `as_of`, or today on this machine's local clock (`date.today()`,
   never UTC: in San Francisco UTC is already tomorrow from 5 pm). That one date is computed once per scan and is the
   report's `menu_as_of` and every `market_summary.menu_as_of`; `cost-price market` / `api.latest_market` show only
   scans whose `menu_as_of` is today (same local default), so a scan with an old or future `as_of` never replaces
   today's comparison. A price change CH applied after the owner's approval is what we compare once it is in force.
   **Upcoming changes**: CH schedules an approved reprice from *tomorrow*, so a scan today still compares the old
   price. Each dish therefore also carries the next valid approved change after the scan date
   (`api.upcoming_changes`, dine-in, this service): `upcoming_price_cents`, `upcoming_from` and `upcoming_position`
   (the new price against the same median), or `leaving_from` for an approved removal. `position` stays today's
   menu. The table prints `approved: $19.50 from 2026-10-25 -> below_market` or `approved: leaving the menu on
   <date>` under the dish. Demo tip: after approving a price, the scan shows it as upcoming; run with
   `--as-of <tomorrow>` to compare it as current. Our location comes from the same database.
2. **Competitors**: the `competitor` table (the 9-restaurant seed list in `cost_price/seed/competitors.json`, gathered
   from their own sites on 2026-10-24, is loaded by `cost-price build`) plus Google Maps through Apify when a token is
   set. A restaurant is kept only if it is Italian or pizza, within 2.5 mi, `$$` or `$$$`, not a chain, open, and has a
   menu page we can fetch (an Apify `menu` link without a scheme, with a bad host such as `http://a..b/menu`, or on
   another port counts as none). Every candidate is in the report with the reason it was kept or dropped; a Google
   Maps row that still breaks something is left out and listed under `limitations`. Relevant Apify finds are added
   to the database (`found_via = apify_google_maps`, one row per menu domain), but they are **unreviewed**: their
   prices never count toward a median (see 6). Apify results are cleaned as they arrive and cached for 24 h in
   `data/market_scout/apify_cache.json`; Apify runs again only when `refresh` is set and the cache is stale, or when
   the cache file is unreadable (bad JSON, wrong types, a time without a zone).
3. **Menu pages**: fetched once and saved under `data/market_pages/` (HTML and text PDFs). Later runs reuse the saved
   copy, so the demo works offline; `refresh` re-fetches only copies older than 6 hours. Delivery apps are never used,
   because their prices are marked up. The fetcher takes http/https on ports 80/443 only, no user:password URLs,
   checks that every address the host resolves to is public and connects to that address, follows at most 3
   redirects by hand (each one re-checked), asks for `Accept-Encoding: identity` and refuses any other
   `Content-Encoding` (the body is read raw with `iter_raw()`, so a gzip bomb is never inflated), stops at 5 MB of
   raw bytes or 20 s for the whole fetch (checked after every read, with 5 s per socket read), and accepts only
   `text/html` or `application/pdf` that starts with `%PDF-` (a PDF over 200 pages is refused before extraction; at
   most 30 pages of the real page tree are read whatever `/Count` says, a page whose drawing operators decode to
   more than 512 KB is skipped (measured without inflating a Flate bomb), 2 MB of operators and 8 s per PDF in all,
   stopping at 200 KB of text; HTML is cut at 2 MB). Addresses that carry a
   private IPv4 (NAT64 `64:ff9b::/96`, 6to4, Teredo, mapped) are checked on the IPv4 inside; IPv4-compatible `::/96`
   and local-use NAT64 `64:ff9b:1::/48` are refused. A whole scan has a 120 s budget, checked before each fetch and
   again after each page is fetched and parsed: once spent, the remaining pages are skipped and listed under
   `limitations`. Parsing is linear in the page size: CSS is split by hand (no regex over a whole `<style>`, at most
   64 KB per block and per page), hide checks see one declaration of at most 200 characters at a time, a `<` that
   never closes becomes text before `html.parser` sees it (it rescans to the end for every unclosed tag), and the
   rule reader takes at most 10 proposals per dish per page (200 per reader).
4. **Reading**: three readers propose prices: the seed observations in the database (each checked on its own
   `source_url`), a conservative rule reader (a line must *be* the dish heading, not a description that mentions
   burrata), and ASI1 when a key is set. Hidden page parts are removed from the text the rule and ASI readers see:
   `hidden`, `aria-hidden="true"`, inline `display:none`, `visibility:hidden`, `font-size:0`, `opacity:0`, offsets
   of -1000px or more off-screen, zero clipping, a `<style>` rule that hides a simple `.class` or `#id` (unless
   another rule shows it again, as tabbed menus do), closed `<details>` apart from the summary, `<template>`,
   `<noscript>`. Same-colour text (white on white) can't be detected reliably; the real defence is the trust rule
   in 6. ASI
   quotes must be at most 160 characters, its price must be the one nearest the dish name in the quote, and its
   match confidence is capped at 0.65 (below the 0.7 needed to count) unless the rule reader found the same price. A
   reader that crashes or returns junk loses only its own proposals for that page.
5. **Verification**: a price is accepted only if its quote is on the saved page and the price is written in the
   quote. Rule and ASI quotes must be in the visible text; hand-collected seed quotes may also sit in a closed menu
   tab (Mona Lisa hides all but the first tab with `display:none`). Everything else goes to `rejected` with the reason.
   A price's `source_url` is where the page really came from after redirects (`final_url`, at most 500 characters).
6. **Analysis**: only **trusted** readings count: the seed list or the rule reader, on a competitor from the
   committed seed list (`found_via = seed`), read on that restaurant's own site: if its page redirected to another
   domain (`domain_of(final_url)` is not the menu page's), the readings are unreviewed, the limitation says
   "redirected off the restaurant's site", seed quotes on it are rejected with that reason, and nothing from it is
   stored. http to https, `www.` and path changes keep the domain and stay trusted. Readings from Apify-found competitors (anyone can list a restaurant on
   Google Maps and publish a page the rule reader would trust) and ASI-only readings are listed in the report under
   `unreviewed` (and `n_unreviewed` per dish) and never move a median. There is no review step yet, so for now they
   are excluded entirely. Of the trusted readings: at most one per restaurant (the best match), match confidence at
   least 0.7, lunch and dinner compared separately (menus with one price for both count as dinner). Prices outside
   0.6x-1.7x of ours are dropped, then outliers more than 3 x 1.4826 x MAD from the median; when the MAD is 0 with
   three or more prices, anything more than 25% from the median is dropped instead. A median needs at least 3
   trusted prices, each from a different competitor. Dropped prices are in `rejected`.
7. **Database**: after each scan, in one transaction (role `scout`): a `verified`/`rejected` check for every seed
   observation tested on a saved page (with the SHA-256 of the saved file and the report id), a new observation plus
   a `verified` check for every accepted rule or ASI price, and a `market_summary` row per dish (read it with
   `cost-price market`). Text from pages, Apify and ASI is NFKC-normalized, stripped of terminal escapes and of
   control, format, surrogate, private-use and unassigned characters, and cut to the schema's caps. Every row is
   validated before the transaction; a row that still fails inside it is rolled back to its own savepoint, skipped,
   and listed under `limitations` ("not stored in cost_price.db"). The report file is written after the database,
   then the receipt. `show` and the table printed by `run` sanitize every string, so a report can't drive the
   terminal.

## Current read (2026-10-24, seed + rule reader, no ASI/Apify)

51 of the 57 seed quotes verify (rechecked after the parsing, redirect and PDF limits above). The 6 that don't are North Beach Restaurant's two-column PDF; its columns interleave
when extracted, so a dish and its price never appear together. The rule reader adds 14 verified prices. Every
competitor here is from the seed list, so the stricter trust rules (seed competitors only, 0.6x-1.7x band, 25%
cut-off at MAD 0) and the wider hidden-text filter leave this table unchanged.

| Dish | Ours | Median | n | Position |
|---|---|---|---|---|
| Margherita Pizza | $18.00 | $21.00 | 7 | below_market (−14%) |
| Funghi Pizza | $22.00 | $26.00 | 3 | below_market (−15%) |
| Diavola Pizza | $23.00 | $25.50 | 4 | below_market (−10%) |
| Caesar Salad | $15.00 | $16.00 | 5 | below_market (−6%) |
| Arancini | $14.00 | $13.00 | 3 | above_market (+8%) |
| Tiramisu | $13.00 | $12.00 | 4 | above_market (+8%) |
| Espresso | $4.50 | $4.00 | 3 | above_market (+12%) |
| Bolognese | $27.00 | – | 2 | insufficient_data (the MAD filter drops Piazza Pellegrini's $24.95 rigatoni) |
| Chicken Parmesan | $29.00 | – | 2 | insufficient_data (the MAD filter drops Mona Lisa's $32) |
| Lobster Ravioli | $28.00 | – | 2 | insufficient_data |

## Limits

- Image-only PDF menus (Ristobar, Little Original Joe's) need OCR and are skipped. JavaScript-only menus need the
  page URL that actually holds the prices (the seed list records it per item as `source_url`).
- Most seed competitors are in North Beach: most Cow Hollow and Russian Hill sites had no readable prices.
- Surcharges (5–6.5% at several places) are not included in menu prices, ours or theirs.
- With only 3 or 4 prices the MAD is small, so the outlier filter is strict: one restaurant a few dollars off the
  other two is dropped (Bolognese, Chicken Parmesan above). That is deliberate; more competitors loosen it.
- The rate limit and the busy flag are in memory: a restarted agent starts with clean counters.
- Apify-found competitors are stored but never counted, and nothing marks them reviewed yet. A review flag belongs in
  `cost_price` (the competitor table and its readers, e.g. `cost-price market`, don't know about review).
- The scan lock is held while pages are fetched. The 120 s scan budget, the 20 s fetch deadline and the 5 s read
  timeout bound it, but one ASI read (up to `ASI_READ_TIMEOUT_SECONDS`) can run past the budget.
- BeautifulSoup is linear but slow: 2 MB of tiny tags takes on the order of 10 s per pass, and a page is parsed twice
  (visible text and text with hidden parts). The scan budget is checked after each page, so a few such pages end the
  scan early rather than stall it, but one page can't be interrupted mid-parse.
