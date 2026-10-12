# My Investments roadmap: the user's real book (issue #1)

Started 2026-10-05. Issue: [ithakis/Convexity#1](https://github.com/ithakis/Convexity/issues/1).

The Portfolio, News and Optimize tabs are a lab for **hypothetical** portfolios:
tickers plus weights, with no quantities, cost or cash. My Investments is the
missing piece: the **real** book, with positions, P&L, performance and alerts.
The owner calls it "the cherry on top, and the cherry is the most important
thing". It should feel as mature as Morgan Stanley, Goldman Sachs or Bloomberg,
and as fresh as TradingView or the Claude and Codex desktop apps.

The work is split into small phases. Each phase runs the same loop: suggest,
prototype, build, verify, review, sign-off. A phase is only closed when the
owner says so.

## Progress

| # | Phase | Status | Branch / PR | Done |
|---|---|---|---|---|
| 1 | Page shell and look | Done (merged) | `feature/my-investments-p1` · [ithakis/Convexity#31](https://github.com/ithakis/Convexity/pull/31) | 2026-10-05 |
| 2 | Ledger and manual entry | Done (merged) | `feature/my-investments-p2` · [ithakis/Convexity#33](https://github.com/ithakis/Convexity/pull/33) | 2026-10-06 |
| 5 | AI import I: text and spreadsheets, and the front door | Done (merged) | `feature/my-investments-p5` (with 6) · [ithakis/Convexity#34](https://github.com/ithakis/Convexity/pull/34) | 2026-10-06 |
| 6 | AI import II: screenshots and PDFs | Done (merged) | `feature/my-investments-p5` (with 5) · [ithakis/Convexity#34](https://github.com/ithakis/Convexity/pull/34) | 2026-10-06 |
| 3 | Performance | Done (merged) | `feature/my-investments-p3` · [ithakis/Convexity#37](https://github.com/ithakis/Convexity/pull/37) | 2026-10-11 |
| 4 | Holdings depth | Done (PR open) | `feature/my-investments-p4` | 2026-10-12 |
| 7 | Attention rail I: risk and events | Not started | | |
| 8 | Attention rail II: plans and price alerts | Not started | | |
| 9 | Notifications | Not started | | |
| → | Thesis and AI advisor (new issue) | Not started | | |

Status values: Not started · In prototype · Building · In review · Done.

**Rows are in run order; the numbers are names.** On 2026-10-06 the owner
moved the AI import (Phases 5 and 6, run together) ahead of Performance:
the composer is the way into the page. Phase 3's decisions were already
taken, and its feedback log keeps them.

## Next-session prompt

Paste this into a new Claude Code session to carry on:

```
Read docs/plans/my-investments-roadmap.md. Continue the My Investments roadmap:
take the first phase in the progress table (run order) that is not Done. That is Phase 7,
Attention rail I: read its section, the Phase 3 and Phase 4 feedback logs (the holdings
table, its columns and the numbers it shows were settled there) and Appendix D, and run it with the phase loop
(suggest → prototype → build → verify → review → sign-off). Start with the first item in its Build list. Locked decisions from Phase 3: Total profit is lifetime;
Return, vs S&P and Your money follow the period pill; the benchmark is the owner's pick
(a fund); the book is valued with as-traded closes; estimated dividends are paid out, never
in Value or Cash. Follow CLAUDE.md. Never use my
real data; run live checks with my API keys as env vars. Ask before any version bump.
```

## The phase loop (every phase)

1. **Pick up.** Read this file, the phase's section and feedback log, and
   CLAUDE.md. Set the phase to "In prototype" in the progress table.
2. **Suggest.** Show 2–3 options as inline chat mockups, each with a
   recommendation and the reason for it, grounded in Appendix D. Let the owner
   choose with multiple-choice questions, and repeat until there is a direction.
   Act as a personal assistant: propose, listen, propose again.
3. **Live prototype.** Build a quick version in the real app. Run it on a temp
   `CONVEXITY_HOME` seeded by `scripts/seed_demo_book.py`, and address it by
   the port its own PID bound. Show screenshots in the light, dark and
   Bloomberg themes. Iterate on feedback; small changes are expected.
4. **Build.** Turn the approved prototype into proper code with tests. Set the
   status to "Building".
5. **Verify.** Run the `verify` skill, ruff, `scripts/check_syntax.py` and
   pytest. Make live calls with the owner's keys wherever NVIDIA NIM or Finnhub
   is used (CLAUDE.md §3). Show proof: screenshots, numbers.
6. **Review.** Set the status to "In review". The owner tries it and small
   changes follow. Before closing, ask explicitly whether everything is good.
7. **Sign-off** (only on the owner's explicit OK):
   - Tick the checklists, add the decisions to the phase's feedback log and the
     lessons to Findings, update the progress table, and keep the next-session
     prompt current.
   - Commit on `feature/my-investments-pN`, cut from `distribution`, and open a
     PR to `distribution`.
   - Reason through the version bump and ask first (CLAUDE.md §15). At a
     bump, rename CHANGELOG's `## Unreleased` to the version header (the
     release-notes slice keys on `## X.Y.Z`).
   - After the merge, give the owner the sync command (CLAUDE.md §3).

## Rules for every phase

- **Never use real data.** Always use a temp `CONVEXITY_HOME`, and never assume
  port 8765: the owner's app often holds it, and `.claude/launch.json` is pinned
  to it. Check which PID owns the port before any write.
- **The repo is public (CLAUDE.md §18).** Demo books use public tickers only.
- **No new outbound hosts.** NVIDIA NIM is the only AI provider (Jev was
  evaluated and declined).
- **UI rules.** At most two levels of box. Data colours stay classic green and
  red. Dark mode is near-black. `--ai` purple marks anything the AI inferred.
- **Before every commit:** `uv run ruff check . && uv run ruff format .`, then
  check_syntax and pytest.
- **Keep the docs current.** Update `docs/architecture/*` and the CLAUDE.md map
  and gotcha index whenever a phase adds a module or a rule.

---

## Phase 1 — Page shell and look

**Goal:** the page exists and its visual language is signed off, before any
real maths is built. The page is filled from the demo book through a stub, so
the owner reacts to something that looks real.

**Depends on:** nothing.

**Prototype questions to explore with the owner:**
- Headline strip: one row of large figures, or a hero value with a secondary
  row? Where do TWR and MWR sit? How is the timing effect explained (tooltip or
  inline)?
- Chart card: height, the Value/Return toggle, how the deposits line and the
  same-deposits-in-the-S&P 500 line are styled.
- Holdings table density compared with the main table; row height; logos;
  whether the totals footer is sticky.
- Rail: width, kicker style, how empty and quiet states look ("All calm").
- Empty state: what a first-time user sees, and the slot for the import.
- Topbar: the label "My Investments", whether to show a count badge, and where
  the active state sits next to Portfolio and News.
- The desktop window at around 1280 px, and the one-column fallback.

**Build:**
- [x] `#inv-btn` "My Investments", first in `#topbar`.
- [x] `showPage(name)` replaces the three hand-copied toggles. It sets
      `body[data-page]`, and `.research-only` hides the main table, the
      column-view bar, the analyst section and `#optimize`. The last page is
      remembered (localStorage `page`).
- [x] `#inv-page` split layout: `.inv-grid` 7fr / 2.2fr, one column below
      1100 px.
- [x] Hero (value and this month), four KPI cells with `RICH_TIPS` hover
      cards (`inv:profit`, `inv:twr`, `inv:vs`, `inv:mwr`), the chart card
      (Value / Return % pill, period pills, daily points, hover reading),
      the holdings table by company name with a cash row (`inv:pos` and
      `inv:posprofit` hovers), and the calm rail. Everything is filled from
      the stub `GET /api/investments/book` (`investments.book_preview`).
- [x] Empty state.
- [x] `scripts/seed_demo_book.py`.

**Verify:**
- [x] Switching across My Investments, Portfolio, News and table-only works,
      and the Portfolio and News state survives a round trip.
- [x] Light, dark and Bloomberg themes; around 1200 and 1440 px, and one
      column.
- [x] Box nesting is at most two levels (sheet, then chart card).
- [x] The full test suite passes, including `tests/test_investments_preview.py`
      (a real book never gets demo numbers; the demo book is coherent).

**Feedback log:**
- 2026-10-05: Q&A settled the decisions in Appendix A. The owner asked for
  this phased, prototype-heavy way of working.
- 2026-10-05: Three page-shell options (A terminal strip, B hero value,
  C side-by-side sheet) were shown as chat mockups. **The owner chose B.**
  - Holdings density: the same as the main table.
  - Calm rail: a short "All calm" line, then upcoming events.
  - **No daily figures** ("not healthy"): show monthly instead. Proposed:
    "This month" replaces "Today" in the headline, a Month %/$ column
    replaces Day, and the chart uses month-end points, with value in the
    display currency on top and monthly % return bars below (S&P 500 ticks).
- 2026-10-05: **Correction from the owner.** "Monthly" applies only to the
  headline field that showed "Today": it becomes "This month". The chart stays
  a line chart like option A (Value in the display currency, or Return %, with
  the S&P 500 for comparison); there is no monthly-bars chart. A new hero
  prototype was shown with two variants: (1) quiet lines, and (2) hero plus a
  four-cell KPI strip with plain-English sublabels. The holdings "Month"
  column is still open; it was shown in the prototype.
- 2026-10-05: **The owner chose hero variant 2** (hero plus a four-cell KPI
  strip) and a **Month** column in holdings. **"Too much info":** cut the
  sublabels ("unrl/rl/div", "your picks vs the market", "timing", "p.a.").
  **Each cell shows a label and one number; details go in a classic hover
  card** that disappears when the pointer leaves (reuse `RICH_TIPS`).
  Labels are plain English ("Total profit", "Return · 1Y", "vs S&P 500",
  "Your money", "/ yr"). Hover cards explain each figure in words, with the
  breakdown table and the technical name (TWR, MWR) in small print at the
  foot. A minimal-hero mockup with hover cards was shown.
- 2026-10-05: **No dotted underlines** on hoverable labels ("ruins the
  aesthetic"). Hover is signalled only by a soft background and a small ⓘ that
  fades in. Hover-card tone: plain words plus a small technical footer
  (confirmed). Shown next: the holdings table (Holding, Value, Weight, Month,
  Profit, Return; ticker hover shows the position, Profit hover shows the
  breakdown including the price-vs-FX split) and the calm rail.
- 2026-10-05: **No ⓘ on hover either**: hover shows only the soft background
  and the card. **Show company names, not tickers** (the ticker goes in the
  name's hover card, and the rail uses names too). **Shares and Avg price are
  visible columns**: Holding, Shares, Avg price, Value, Weight, Month, Profit,
  Return. Avg price is in the share's own currency (pence for LSE). Mockup v4
  shown.
- 2026-10-05: **The owner approved mockup v4: "build it live".** The live
  prototype is running on the demo book: the `#inv-btn` topbar tab,
  `showPage()`, the split layout, hero plus four KPI cells with hover cards
  (`RICH_TIPS` `inv:*`), the Value / Return % chart with period pills and a
  hover readout, the holdings table with names, a cash row, the calm rail and
  the empty state. A stub `GET /api/investments/book` (`investments.py`)
  serves either the demo book or `{empty}`. Checked: all four page states,
  light / dark / Bloomberg, and the full test suite passes. Waiting for the owner's
  review.
- 2026-10-05: **Live review: "looks good, small tweaks".** All three Value-mode
  lines stay.
  (1) The chart uses **daily** points, not weekly (the demo series is now 784
  deterministic weekdays; Phase 3 serves real daily closes).
  (2) The chart must stay **the same size** in Value and Return %. The
  toolbar is now a fixed-height single line, the legend has moved into the
  fixed-height reading under the plot, and a mode switch redraws only the
  chart.
  (3) The Value | Return % switch uses **the same pill toggle as the
  Portfolio tab's Table | Chart** (`.pf-contrib-toggle`).

**Findings:**
- The global `[data-rich-tip]` rule adds a dotted underline; this page turns
  it off (`#inv-page [data-rich-tip]`). The owner rejects both underlines and
  info icons, so a hover cue is a soft background only.
- Reproducing a size jump needs measurement at several widths. The fix that
  holds at every width is fixed-height single-line toolbars plus redrawing
  only the part that changed.
- Narrow windows (found by /verify at ~600 px). A fifth topbar button made
  the labels wrap, so topbar buttons are now `nowrap` and `flex: none`, the
  status text is what shrinks, and below 860 px the topbar wraps to two rows.
  The holdings table scrolls inside `.inv-tbl-wrap`, never the page. A
  container query hides the series names in the chart reading when the card
  is narrow.
- Demo figures must be internally coherent (profit = value − money put in;
  the headline return is read off the chart's own index), or the owner reads
  the mismatch as a bug.
- Phase 2 must replace `book_preview()` behind the same payload keys:
  `ccy`, `headline{value, cash, cash_weight, month_abs, month_pct,
  profit_total, profit_held, profit_sold, dividends, twr{period},
  bench_twr{period}, mwr_ann}`, `positions[]`, `series{dates, value,
  invested, bench_value, twr_index, bench_index, period_start{period}}`,
  `upcoming[]`, `attention[]`. `period_starts()` is the one place the
  period-base rule lives.
- Adversarial review (2026-10-05) fixed: FX switch and startup rates not
  re-rendering the page; JS and Python period bases disagreeing (now the
  server sends indexes); a stretched SVG (now drawn at pixel width); the
  wrong currency in tips and the cash mark; null crashes; unescaped dates;
  R / Refresh acting on the hidden research tab. It also cut about 200 lines.
- The version did not change in Phase 1 (owner's call); 1.20.0 is meant for
  Phase 2, when a real book works.

---

## Phase 2 — Ledger and manual entry

**Goal:** a correct book from hand-entered trades, with undo.

**Depends on:** Phase 1.

**Prototype questions:**
- Quick add versus full transaction form: inline or a popover? Which fields
  are visible by default?
- Ledger list: grouped by month or flat? How editing in place looks.
- Where undo lives (ledger header, toast, or both).
- How an oversell or a broken edit is explained, in plain words.

**Build:**
- [x] `src/convexity/ledger.py`: a pure engine with no I/O.
      - Average cost, one cash pool in `base_ccy`, implied deposits.
      - Split factors with `qty_basis` set to `trade` or `current`.
      - Same-day order: deposit, dividend, sell, buy, fee, withdrawal.
      - `OversellError`. See Appendix B.
- [x] `src/convexity/investments.py`:
      - `state/investments.json` (version, rev, settings, instruments, entries,
        journal of at most 100, redo).
      - Atomic write plus `.bak`; a malformed file is never overwritten (409).
      - `base_rev` conflicts return 409.
      - `ensure_instrument` via `resolver.resolve_symbol`.
- [x] Routes:
      - `GET /api/investments`
      - `POST /api/investments/entries`, `…/entries/update`,
        `…/entries/delete`, `…/undo`, `…/redo` (JSON only)
- [x] UI: quick-add holding, add transaction, editable ledger, Undo/Redo with
      a label and ⌘Z.
- [x] Holdings table core columns: ticker, qty, avg cost, last, day %/$,
      value, weight, unrealised $/%, realised $, total return.
- [x] Lifetime P&L split in the headline: unrealised, realised, dividends.
- [x] `tests/test_ledger.py`: the acceptance test in Appendix B plus its
      companions.
- [x] `tests/test_investments_store.py` and route tests.
- [x] A test that the seeded demo book replays cleanly.
- [x] Rail toggle **Attention | Activity** (`.pf-contrib-toggle`, remembered
      in localStorage `inv_rail`); Activity grouped by month, a row click
      edits in place.
- [x] Add popover, quick add first; **Since is required, prefilled with
      today**; price auto-fills from that day's close.
- [x] Before Phase 3: the return cells show "—" and the chart card one calm
      line; the demo book also goes through the real engine.
- [x] **The book in the Portfolio tab:** a pinned, read-only red pill
      `__book__` labelled "My Investments", "Book" (value) weights by
      default, server guards on the reserved name.

**Verify:**
- [x] The acceptance numbers match.
- [x] With curl on our own port: add, edit, delete, undo, redo; a stale rev
      returns 409; a text/plain POST returns 403.
- [x] In the browser: the empty state, then the first holding, then an edit,
      then undo.

**Feedback log:**
- 2026-10-05: Q&A with mockups. **Add:** a popover from "+ Add", quick add
  first ("Holding": company, shares, avg price, Since), a type row for Buy /
  Sell / Dividend / Cash / Fee / Split, more fields behind a disclosure.
  **Since is required and prefilled with today.** **Undo:** a toast after
  every change, Undo/Redo in the Activity header, and ⌘Z / ⇧⌘Z.
  **Before Phase 3:** real value, month and profit; the return cells and the
  chart show calm gaps, never made-up numbers.
- 2026-10-05: **Ledger placement:** three graphical options shown (toggle
  beside Holdings, section below, drawer). The owner chose the drawer idea,
  refined: **the rail toggles between Attention and Activity**, like Table |
  Chart in the Portfolio tab.
- 2026-10-05: **New feature added to this phase:** the book appears
  automatically in the Portfolio tab as a pill coloured in the theme's red.
  Chosen: a read-only mirror (holdings = constituents, value weights by
  default; Equal / Cap, analytics, News and Optimize still work; no rename,
  delete, edits, presets or "apply"), shipped inside Phase 2.
- Holdings columns stay the Phase 1 sign-off set (Holding, Shares, Avg price,
  Value, Weight, Month, Profit, Return), superseding Appendix A's list.
- 2026-10-06: Built live on the seeded demo book (`ledger.py`,
  `investments.py`, the routes, the rail, the popover, undo and the book
  pill). Checked in light, dark and Bloomberg at 1440 px, plus the empty
  state. The Appendix B acceptance numbers match exactly. One live News
  refresh on the book tab with the owner's keys worked: Finnhub headlines for
  the US names, the News read from NIM, and Book weights. Waiting for the
  owner's review.
- 2026-10-06: Adversarial review (owner's /verify request) fixed 9 backend
  bugs, each with a test:
  - a sell of a tiny fraction of an unheld share crashed;
  - a book file with a bad shape was a 500, not left untouched;
  - booleans, overflowing numbers and junk currency codes were accepted;
  - `source` could mark a real book as demo;
  - changing an entry's company kept the old currency;
  - a pence holding without a Yahoo price showed in pounds;
  - a non-object entry was a 500;
  - the `__book__` guard was too broad (it blocked a preset with that name);
  - holdings with no price dropped out of the value silently.

  Frontend fixes: a faded toast kept its Undo clickable; currency hints were
  unescaped; ⌘Z fired inside the form; book-pill edge cases. FX history
  lookups now use bisect. Then `/verify` on a clean uv-tool install of the
  tree:
  - API: 409, 422, 400s, undo/redo, the origin guard and the reserved name
    all held.
  - UI: quick add in pence (SHEL.L, average 2,605.75p), inline edit, ⌘Z.
  - The Portfolio regression flow (new portfolio, Refresh red then clear) and
    the book tab following a new holding.
  - A live News refresh on the book tab: 4 of 6 holdings read.
  - Reload restored the tab; the logs held no amounts; the desktop window
    reached `loadFinished ok=True`.
- 2026-10-06: **Owner sign-off: "commit, create a PR and merge".** Version
  1.20.0 (owner's call).

**Findings:**
- Yahoo's `auto_adjust=False` only removes the dividend adjustment; closes
  stay split-adjusted. The price auto-fill multiplies the later splits back
  in (NVDA on 1 May 2024 is ~830, not 83). Phase 3's unadjusted valuation
  must do the same.
- Same-day order (sell before buy) means a buy and a sell of the same share
  on one day is refused when nothing was held before. That is Appendix B's
  rule; revisit it if the owner day-trades.
- "This month" values month-start holdings at today's FX until Phase 3
  brings daily FX.
- "This month" can count a dividend twice in a month with an ex-date:
  `_bulk_close` closes are dividend-adjusted. Phase 3's as-traded closes fix
  it.
- The demo book now runs through the real engine. The "Demo data" badge
  shows only while every entry is a demo entry, so it disappears after the
  first entry of your own.

---

## Phase 3 — Performance

**Goal:** honest performance numbers and the chart.

**Depends on:** Phase 2.

**Prototype questions:**
- How TWR, MWR and the timing effect are labelled for a retail user versus a
  quant (the tooltips carry the formulas).
- What the chart shows by default: Value mode or Return mode.
- Period pills and how they relate to the lifetime P&L split, so the two are
  never confused.

**Build:**
- [x] `_bulk_close(adjusted=False)` with dividends cached (splits stay with
      `market_splits`); the default path stays byte-identical.
- [x] `fx.fx_multipliers`.
- [x] Extract `analytics._carino_link`.
- [x] Daily TWR and MWR (XIRR via `brentq`, solved per period length).
- [x] Periods 1M, 3M, YTD, 1Y, 3Y and ALL; under one year, compare the period
      figures rather than annualised ones.
- [x] `drawInvChart`:
      - Value mode: value, net deposits, and the same deposits invested in the
        S&P 500.
      - Return mode: TWR index against SPY total return, with the measure brush.
- [x] FX display currency and the price-versus-FX split.
- [x] Refresh on this page through the `__investments__` sink in `jobs.py`;
      research tabs untouched.
- [x] `RICH_TIPS`: `inv:twr`, `inv:mwr`, `inv:timing` (the Your money card's
      breakdown).
- [x] Estimated dividends (owner's choice, paid out) and the opening-holding
      rule; `opening: true` on imported current holdings.

**Verify:**
- [x] NVDA's 10:1 split (2024-06-10) values a quick-add dated 2023 correctly
      (live: 100 x Yahoo's close on 2023-03-01, 2024-06-07 and 2024-06-11).
- [x] Switching between USD and GBP.
- [x] The S&P line in the chart matches SPY total return over 1Y (live:
      17.2549% both ways).

**Feedback log:**
- 2026-10-06: Chat mockups and questions (asked while this phase was first in line). The owner chose:
  - **Periods: O2.** "Total profit" stays lifetime. Return, vs S&P 500 and
    Your money follow the period pill.
  - **The chart opens in Value mode**, and the last mode is remembered.
  - **Refresh: the topbar Refresh button** (and R) shows on My Investments
    and refreshes the book only.
  - **A quick-added holding's performance starts at that day's market
    value.** The gap from its average price stays in lifetime Total profit
    and is never a one-day return, which keeps the Since tooltip's promise.
- 2026-10-06: Second round of questions (Phase 3 picked up). The owner chose:
  - **Cumulative returns.** Return and vs S&P 500 are cumulative on every
    pill, so the headline equals the end of the Return % chart. Your money
    is "/ yr" from 1Y up and a plain period % under a year. The hover shows
    the other form ("+48.6% in 3 years, or 14.1% a year").
  - **Display currency: each day at its own rate.** The book is computed in
    the display currency, so a GBP view includes what the pound did; the
    S&P line is converted the same way.
  - **Unrecorded dividends are filled in from Yahoo, per payout** (not per
    holding, which double-counts a partly recorded history). A payout with no
    recorded dividend from 5 days before to 45 days after its ex-date is
    estimated as shares held x amount per share, gross. A recorded one
    replaces its estimate.
  - **Estimates count as paid out to the owner:** in Return, vs S&P, Your
    money and Total profit ("Dividends, estimated"), never in Value or Cash.
  - **The timing effect lives in the Your money hover card**; four KPI cells
    stay.
- 2026-10-09: The owner's first real import (an Interactive Brokers positions
  screenshot, installed build) and five issues:
  - **Wrong listings:** read the exchange from the screenshot; use that line
    when Yahoo has it, else the best proxy (same company, same currency
    first), and say which and why. "Invest a lot of effort" in loading
    trades correctly.
  - **History:** each holding's buy date is set in its own row in the first
    import's review, not one date for the whole list.
  - **Optimize on My Investments:** wanted as a rebalancing tool, then
    "forget about the optimize part for now". Not started.
  - Selected text unreadable in light mode; a Settings action to clear the
    book; Gedeon Richter (Budapest) not findable.
- 2026-10-11: Rounds 2 and 3, all done. The owner chose "C · five bands +
  Sharpe" for the Volatility card, total-return + MSCI World as the benchmark
  picker's default, and "opens in My Investments, topbar switches". Asked for:
  tiny Portfolios table fixed, "Portfolio" renamed "Portfolios", a red dashed
  benchmark line, a default currency in Settings, LaTeX hover cards, and
  Rec Δ6M, MSPR and news for European listings (Finnhub's US line, Yahoo
  fallbacks). Then: broker exchange codes in the Add box (`AIR SBF`,
  `IBIS:SAP`), "vs" centred between the toggle and the picker (option A), and
  one period selector everywhere: a big pill with the chosen period as a
  raised pill inside (the owner's own design).
- Parked: Optimize on My Investments; dividends not counted during a history
  stand-in stretch. The split-fetch robustness fix moved to Phase 4's Build.

**Findings:**
- **Replaying the whole ledger in the display currency was wrong for
  cash.** It froze uninvested USD cash at its arrival-day rate, so a GBP view
  ignored what the dollar did to it (a test with half the book in cash
  caught it). Now the base-currency replay gives cash and deposits,
  converted day by day, and a display-currency replay gives each holding's
  cost; the difference is "Currency on cash" in the profit card, so the
  breakdown still adds up.
- Per-payout dividend matching (5 days before to 45 after the ex-date, one
  to one, nearest first) is what makes "fill them in from Yahoo" safe: a
  per-holding switch to total-return prices double-counts a partly recorded
  history.
- A zero day before the first entry lets ALL include the first day's own
  move, and makes the XIRR start explicit (it is shift-invariant).
- The first page load of a session reads years of daily closes (~8 s cold
  for six names); a "Reading your prices…" line replaces the blank page.
  Later loads are cached (GBP after USD: 0.1 s).
- **Adversarial review (owner's /verify request, 2026-10-07)** fixed 7
  issues, each with a test or a live check:
  - a share that pays no dividends re-downloaded years of closes on almost
    every page load (its empty dividend cache expired in 60 s);
  - a display currency with no Yahoo rates showed "your investments can't be
    totalled": it now falls back to the base currency with a warning;
  - the S&P line could 500 when the USD rate was missing;
  - annualising a few days of a big move could overflow;
  - the Portfolio tab's startup load fetched the book without the display
    currency, and a slow load could land after a newer one (a load sequence
    now drops stale answers);
  - on My Investments, attaching to a running research refresh skipped the
    research reload when it finished.
  Removed:
  - `ledger.twr` and `ledger.mwr` (superseded by `twr_index` and
    `period_mwr`);
  - `_on_or_before`, the `_fx_series` hook and the replay's own FX lookup
    (`_make_fx_at` now reads `fx.fx_multipliers`, the one FX path);
  - `_bulk_close_raw`, merged into `_bulk_close` (one download and retry
    path for both modes; the default call is pinned by a test).
  Scale check: a 20-year, 40-share, 800-trade book computes in 0.64 s.
- **The owner's first import (2026-10-09)** booked euro rows shown on Paris
  and Madrid on Xetra (currency alone picked the first euro line) and a
  London row's pence as pounds (no currency on screen: a 100x cost, which
  was the whole "loss" on the page). Plain code now reads the exchange
  beside the ticker and checks units against the market price; the eval gained a synthetic broker positions case (11/11
  live). Asking the model for the exchange made it drop rows that showed none
  (4 → 8 of 10 reads on "typed holdings"), so code reads it instead. On a
  positions table the model took today's price for the average; prompt
  changes either lost the headings or broke the phone list, so a row's own
  arithmetic ((last − average) × shares = its P&L) decides instead.
- **Yahoo's screener has no Gedeon Richter line at all**, and Yahoo renamed
  its Budapest ticker (RICHTER.BD → RICHT.BD) the day of the report, leaving
  one day of history. `symbol_db.yahoo_lookup` (live Lookup) covers names the
  pack lacks; `analytics.extend_history` lends a short line the same
  company's older history. Yahoo's old data needed guards: Frankfurt's Richter
  lines are negative when dividend-adjusted before June 2026, Unilever's
  London line jumps x52 in 1993, and an order-book line moved six times in
  five years.
- The Portfolio tab cut a basket to its youngest line (`dropna(how="any")`);
  it now starts without that holding and reweights the rest. Its analytics
  warnings had never been shown anywhere; they are now under the chart.
- **Adversarial review of the follow-up (owner's /verify, 2026-10-10)**
  fixed, each with a test: a short price fetch wiped the cached dividend
  history; decimal commas made the P&L arithmetic "prove" a 100x price;
  "TSE" (Tokyo on many screens) booked a yen price as Canadian dollars;
  `same_company` took Bank of America for Bank of Ireland; a pence
  conversion left a stale fix; unverified AI picks via Yahoo; "T" matched
  inside "T-Mobile"; real -70% days were masked as glitches; a missing
  ticker cost a second ~7 s of retries; "richter" in a pasted list became
  Richtech Robotics; Settings → Clear stayed stuck after a conflict.
- Two Phase 3 tests compared against "today's" price, which fails on a
  weekend (the last close is Friday's); they use the last trading day now.
- **2026-10-10: the import UI, redesigned with the owner from mockups.**
  Three rounds in chat. Composer: the owner first liked a tall card, then
  chose **the ChatGPT-style pill with starter chips**, minus "Add one by
  hand" ("the pill is adding one by hand") and with a one-line placeholder.
  Review rows: **ticker-first layout C, swapped to name first in bold and
  ticker muted**, the ✦ mark on the left (as in Portfolio search) instead of
  purple, and the exchange clickable to a **simple listing list**. Header:
  **a light navy band with bold labels** ("they get lost"). Date: neither
  mockup; **year buttons, then month, then day**, "super optimized for
  speed of input and very visually inviting", in a compact chip with a
  calendar icon; **no auto-advance** to the next row. Also: the portfolio
  name/cache line hides on My Investments. Built live; the typed-text path
  now finds RICHT.BD from its HUF price (Yahoo Lookup by the first word).
  Live import eval 11/11 (two clean runs; one earlier flake dropped a row).
- **2026-10-10, owner: drop the "Since when" question** ("performance
  should automatically count since the oldest day"; with a date per row it
  made no sense). Undated holdings default to today, shown faintly.
  **/verify with the owner's own positions screenshot (21 rows, kept out of
  the repo):** all 21 read right (quantities, averages, exchanges, DGE in
  pence), every listing within a few % of the screen's last price. Found and
  fixed: every row showed ✦ (the model copied "GLE SBF" as the name), and
  Novartis's Xetra line has no Yahoo data from 2019 to 2025 (now filled from
  NOVN.SW). The review's company cell now keeps ticker and exchange visible
  when space is tight. Live eval 11/11 twice.
- **2026-10-11, owner's review of their real book** (screenshot of the page).
  Picked from mockups: layout A1. Last month under the value; Total profit
  (lifetime), Return with the S&P's beside it, "Change in portfolio value"
  (owner's wording) with the money put in, and a risk cell the owner added
  (volatility, Sharpe, Sortino). "Your money" moved into the Return card (a
  duplicate of Return side by side); "pp" gone. The S&P line starts at your
  value for every pill and hides from its legend; drag-to-measure in Value
  mode too; holdings sort by column or drag into your own order; numbers
  always en-US (owner's choice); the review's € hugs its price.
  **Bug found:** Apply dropped `opening`, so the owner's 21 imported holdings
  (average prices from later buys, dated Jan–Feb 2025) were booked as bought
  at those prices then: ALL TWR −71% (MWR +12.6%). Fixed, and old imports
  read as opening without a rewrite: ALL TWR +49.9%. **Why the Portfolio tab
  differs** (1Y, owner's book): +8.5% actual; +9.2% buy-and-hold total
  return; +12.5% start mix rebalanced daily; +17.1% today's mix rebalanced
  daily (the tab's test: hindsight weights). The owner kept the test, with a
  note giving the actual return beside it.
- **2026-10-11, second round** (installed build). The owner asked for: the
  benchmark line red and dashed, and a choice of benchmark as on Portfolios
  (picked: the Portfolios list plus MSCI World, each priced through a
  total-return fund, kept in `prefs.json`); a default currency in Settings
  (picked: the app opens in it, the topbar switch lasts until the app
  closes); LaTeX in the hover cards with aligned equations; a green to red
  volatility scale (mockup C picked from three: five named bands plus a
  Sharpe line); "Portfolio" renamed "Portfolios". **Bugs found:** the
  Portfolios table shrank to 40% after a render while My Investments showed
  (it measured a hidden table), and the desktop app forgot every
  localStorage choice at each launch (Qt 6's default profile is
  off-the-record; now a persistent profile in the data folder).

---

## Phase 4 — Holdings depth

**Goal:** the holdings table becomes a monitor.

**Depends on:** Phase 3.

**Prototype questions:**
- Which extra columns are on by default, and how the Columns menu looks.
- How risk share and weight are shown side by side (bar pairs?).
- How suggested dividends are presented in the rail.

**Build:**
- [x] **Don't mark the book broken on a split-fetch miss** (found 2026-10-11,
      first, because the table builds on it). `investments._fetch_splits`
      turns a failed Yahoo answer into "no splits" and `market_splits` caches
      that for 10 minutes, so a pre-split `qty_basis: "trade"` entry replays
      at face value, the ledger oversells, and `page()` returns `broken`
      for a book that is fine. Tell "Yahoo failed" (None) from "no splits"
      (empty): keep the last good list per ticker and retry soon; with none,
      show a "split data unavailable, retrying" notice and compute what can
      be computed, never "broken". Test with a stubbed failing fetch.
- [x] Columns:
      - News read and Market read tier (`nsDot`)
      - contribution in pp (Carino-linked, plus a "Cash and fees" bucket)
      - risk share (`mpt.annualized_cov`, Ledoit)
      - dividends and yield on cost
      - analyst upside
- [x] `INV_COLS` registry and Columns menu.
- [x] Collapsible "Closed (n)" section, cash row and totals footer.
- [x] Suggested dividends (Yahoo ex-date × quantity held), added with one
      click and never applied automatically.
- [x] Export: "Positions" and "Ledger" sheets.
- [x] `RICH_TIPS`: `inv:contrib`, `inv:risk_share`, `inv:yoc`.

**Verify:**
- [x] Contributions add up to the TWR and risk shares add up to 1 (tests in
      USD and GBP with fees, a sale, a dividend and a withdrawal; live demo
      book 1Y: +21.0 pts = +21.0%, cash bucket 0.0).
- [ ] Export opens in Excel with the new sheets (opens in openpyxl, live
      GBP export checked; the owner to open it in Excel).
- [x] A simulated Yahoo split outage (NVDA, seeded demo book) leaves NVIDIA
      out with a note, the rest computed; the page healed itself 44 s after
      the outage lifted (120 shares); a restart during the outage used the
      saved list.

**Feedback log:**
- 2026-10-11: Split-fetch miss. The owner chose **"leave it out, retry"**
  over "compute anyway, warn" and "ledger only": with no known split list
  for a holding, totals, chart and returns are computed without it; it stays
  in the table with dashes and a "waiting for Yahoo's split history" note;
  the page retries every minute. No number is ever off by a split factor.
- 2026-10-11: Suggest round (chat mockups, four questions). The owner chose
  every recommendation:
  - **Default columns: today's 8 plus Contribution and Risk**; News read,
    Market read, Dividends, Yield on cost and Analyst upside start off in a
    Columns menu.
  - **Risk share sits with weight as paired bars** in one cell (weight gray,
    risk blue, exact figures on hover).
  - **Suggested dividends: one grouped Attention item** ("4 dividends to add ·
    $214 estimated", Review opens a list with Add per row and Add all as one
    undoable batch). Never applied automatically.
  - **Table foot: Cash, a Total row, then a collapsed "Closed (n)"** with the
    sold-out holdings' realised profit and dividends.
  - Taken as defaults (not asked): Contribution follows the period pill and
    adds up to that period's Return with a "Cash and fees" line; risk share
    uses the last year of daily returns at today's weights.
- 2026-10-12: Owner review of the installed build (real book, six points).
  Chosen from chat mockups:
  - **Holdings headers "kicker"**: 11px caps, tracked, a step brighter than
    muted (`--th-ink`); every label ends on its numbers' right edge, the sort
    arrow hangs in the padding. "Weight · Risk" became **"Weight & Risk"**.
  - **Chart**: more air above and below, **SMA 20/50/200** pills (shared with
    the Portfolio chart), the dashed flat line replaced by a **solid stepped
    "money you put in" line** labelled "Put in €Xk" at its right end (it was
    that line all along, flat because nothing went in or out over 1Y).
  - **Opening fast: show the last page at once, then refresh** ("Updated
    hh:mm · refreshing…"), rather than only fresh numbers.
  - Asked: why the My Investments 1Y return is far below the Portfolios book
    tab's. Both were re-computed from a scratch copy of the owner's book and
    are right (Findings).

**Findings:**
- **Opening My Investments took about 30 s of Yahoo on a cold launch** (a
  book of 20+ holdings, two of them young listings, 2026-10-12), behind
  "Reading your prices". Causes and fixes:
  - the page asked twice at once (`bookLoadWeights` raised `INV.seq`, so the
    first answer was thrown away): both now share `INV.loading`;
  - each young listing's stand-in candidates that Yahoo had no batch data
    for went through `_bulk_close`'s one-by-one retries (up to ~5 s each, 16
    s in all): candidates now use `retry=False`;
  - the stand-in lookup (four Yahoo calls) repeated every launch: found
    stand-ins are kept a week in `state/proxies.json`;
  - closes, stand-ins and the benchmark download in one worker while every
    currency's history comes in parallel (`fx.prefetch_usd_series`, Ticker
    history, which can run beside a download);
  - the server computes the page at startup and keeps it a minute
    (`investments.page`, one computation per currency at a time), and the
    last good page is on disk (`state/book_cache.json`): the window opens
    on it while the fresh one computes.
  Measured: a cold compute about 30 s -> 9 s, identical figures; the desktop
  window opens at +7.5 s on the saved page, fresh numbers follow.
- **My Investments' 1Y return vs the Portfolios book tab's Period return:
  both correct, different questions.** The gap on the owner's real book (a
  year of buy-and-hold, no trades) was taken apart step by step on a scratch
  copy, each step re-computed on the same prices:
  1. the book itself (the shares held, dividends paid out): the base;
  2. **hindsight weights, about +5.6 pts**: the tab starts the year at
     *today's* weights, which already favour the year's winners. Holding
     today's weights from day one instead of what was held then is
     (today's weight - weight then) x the holding's return, summed; one big
     winner that was a small position a year ago and one loser that was a
     big one account for most of it;
  3. **daily rebalancing, about +2.4 pts**: the tab resets to those weights
     every day, selling what rose and buying what fell, which pays in a
     volatile basket; the book never rebalanced;
  4. **dividends reinvested, about +1 pt**: the tab uses dividend-adjusted
     closes (each payout bought back into the same share); the book pays
     them out as cash;
  the steps add up to the tab's figure to 0.01 pt (and the tab's own number
  was reproduced exactly from its saved rows). Of the book's own return, the
  price part is about 12 pts and dividends about 2.3; each holding's
  contribution matched its price change plus dividends within 0.1 pts.
  Side notes: a holding's early prices can come from a stand-in listing, as
  the page warns; and imported entries that are today's average costs dated
  early on skew ALL and the first days, not 1Y.
- **yfinance never raised on a failed split fetch.** `Ticker.splits` reads
  `history(period="max")`, which turns a failed download into an empty frame,
  so `_fetch_splits` returned `[]` ("no splits"), cached for 12 h (not the
  10-minute miss TTL the roadmap assumed: that only caught the rarer None).
  It now reads the history itself and calls an empty one a failure.
- Leaving a holding out removes its cash flows too ("as if never bought"),
  so Value, Total profit and TWR stay one coherent book; the only figure the
  owner would see differ from the broker is Cash, and the Attention note says
  the holding is out. Positions are independent in the ledger and a cash
  shortfall is an implied deposit, so dropping one holding can't break the
  replay of the others.
- Writes touching a pending holding are refused rather than accepted
  unchecked: an accepted edit could break once the splits arrive (a
  trade-basis sale checked against current-basis shares).
- **Contribution needed the ledger's per-holding flows.** `ledger.Day` now
  records the money into each holding per day (buys less sells less its
  dividends, fees left out); with the opening holdings at market value, the
  holdings' day P&L plus a "Cash and fees" remainder is exactly the TWR's
  daily return, so Carino linking adds up to the headline Return to 1e-8.
- Risk share is the same whether cash is in the weights or not (cash has no
  variance and RC is homogeneous of degree 0), so it is safe to show beside
  the portfolio weight; the hover says "cash carries none".
- **Adversarial review (owner's /verify, 2026-10-12)** fixed, each with a
  test:
  - a ticker Yahoo doesn't carry (delisted) sat "pending" forever and
    blocked its own edits: yfinance reports an outage and an unknown ticker
    the same way, so the page's price download decides (prices for the
    benchmark but none for it = not on Yahoo: trades as entered, a warning,
    asked again in 12 h);
  - a day that lost everything (a zero close on a one-holding book) crashed
    the Carino link and would have 500ed the page;
  - the split cache's dict could change size mid-save between a page load
    and the refresh job (now under a lock);
  - one listing younger than a month blanked every holding's risk bar (it is
    now left out alone);
  - "Add all" on more than 500 suggested dividends was refused (limit 5,000);
  - a note starting with "=" became a live formula in the Excel export;
  - yield on cost showed 0% instead of nothing when the cost or rate was
    missing; a sort by a column switched off stayed applied invisibly;
    switching Weight off hid the risk bars while Risk stayed ticked;
  - analyst upside was blank until the Portfolios book tab had loaded once:
    it now falls back to any saved portfolio's cached row.
  Checked clean: bad input on the dividends route (400/409), cross-origin
  and non-JSON posts (403), odd display currencies (contributions add up in
  USD, GBP and JPY), no key in any log or response, a 20-year 40-share
  800-trade book (contributions exact, risk shares sum to 1; the time is in
  Phase 3's price handling), and live: Portfolios refresh with News (Finnhub
  5 calls, NIM 13), Optimize (40,000 portfolios), desktop boot offscreen.
- The demo book has 58 unrecorded payouts, so the review list scrolls
  (340 px). Analyst upside reads the Portfolios tab's cached book rows: it
  is a dash until that tab has loaded the book once.

---

## Phase 5 — AI import I: text and spreadsheets

**Goal:** the "Add or update" composer for natural language, CSV and XLSX.

**Depends on:** Phase 2.

**Prototype questions:**
- Composer placement (always visible, or on a button), and its wording.
- The review table: confirm the agreed prototype on real outputs.
- How snapshot-versus-transactions reconciliation is explained.

**Build:**
- [x] The front door (owner's W1): the composer is the empty page, and
      "+ Add" opens it on a full page, with "By hand" one click away.
- [x] Adding by hand and editing in Activity use the E2 card (owner's
      choice).
- [x] Deterministic pre-parse (CSV and XLSX via pandas or openpyxl), then
      `nemotron-3-super` maps it to a strict JSON schema of ledger operations.
- [x] Every ticker is verified in the symbol pack, the same rule as
      `search.py`. The row's currency picks the listing.
- [x] Review table:
      - a one-line summary
      - purple for inferred cells, amber for missing ones
      - the source shown on each row
      - at most 3 inline one-click questions, each with a stated default
      - a dry run that flags a sale the book can't cover, with a fix
- [x] Apply as one undoable batch; alias memory (e.g. "Shell" maps to SHEL.L).
- [x] Snapshot detection, reconciled against the current book.
- [x] `scripts/eval_import.py` gold set with synthetic statements only, run
      live.

**Verify:**
- [ ] The eval passes live.
- [ ] Broker-style CSVs from at least three formats.
- [ ] Undo removes a whole batch.

**Feedback log:**
- 2026-10-06: **Owner request, made while Phase 3 was next:** make the
  empty state and the Add popover more beautiful, friendly and inviting. Today the empty card has a left-aligned button under centred
  text and a lot of dead space, and the popover covers the page and crowds
  seven type pills into one row.
- 2026-10-06: Chat mockups and questions. The owner first chose:
  - **Empty state: A, "Calm welcome".** No box. A round icon, a headline,
    one line, a centred button, then three quiet cues under a hairline
    (value and profit, vs S&P 500, what needs a look). (Rejected: a faded
    ghost of the page, and the first holding typed into the welcome.)
  - **Add popover: P1, "Holding first".** Titled "Add a holding". The other
    six types sit in a quiet "Something else" menu, so there's no pill row.
    (Rejected: two rows of grouped chips, and a side panel.)
- 2026-10-06: Live prototype of A and P1 (light, dark, Bloomberg).
  **The owner didn't like it much.** They want **AI-assisted input** as the
  way in: add files or paste text (Revolut screenshots, for example) and the
  AI sorts them into historical trades. They also said it is "not that
  inviting to edit" and asked why it got worse than before. Explanation
  given: the composer (Appendix A, "Import UX", approved 2026-10-05) was
  scheduled for Phases 5–6, so Phase 2 built only manual entry, and A/P1
  only polished that manual path. Next: options that make the composer the
  front door, and a question about where the AI import goes in the order.
- 2026-10-06: **The owner chose W1, the composer as the welcome**: one box
  to drop, paste or type into, with "add one holding by hand" as a quiet
  link, and "+ Add" on a full page opens the same composer. **The AI import
  is built now, before Performance**: text, CSV/Excel, screenshots (Revolut)
  and PDFs, so Phases 5 and 6 run together. "Not inviting to edit" means
  **the Add form and editing in Activity**: both get redesigned in this phase.
- 2026-10-06: Three edit feels were shown (E1 a sentence with blanks, E2 a
  live preview card, E3 an entry sheet over the rail). **The owner chose E2
  for both adding by hand and editing**: a company header with today's
  price, big number fields, date chips (Today, 1 year ago, a picked date),
  and the result (value, profit) shown as you type.
- 2026-10-06: **Live prototype on a temp data folder**, with live NIM calls
  on the owner's key. Shown:
  - the composer welcome;
  - a synthetic broker screenshot read into a review table (26 s);
  - the NVIDIA sale flagged before Apply, with the one-click fix;
  - Apply as one undoable batch ("Imported 7 entries · Undo");
  - "+ Add" opening the composer, "By hand" opening the E2 card, and the
    same card for editing in Activity;
  - light, dark and Bloomberg themes.

  Reading model chosen by probe:
  - Screenshots go to `nemotron-3-nano-omni` with thinking off (~7 s, every
    field right at phone resolution).
  - Llama 3.2 90B misread dates and a currency and took 75 s.
  - `nemotron-parse-2.0` was degraded on NVIDIA's side and Kimi K3 timed
    out.
  - Text goes to `nemotron-3-super` under a strict schema. Three
    broker-style CSVs (pence, EUR and commission included) and a holdings
    screenshot also read correctly.
- 2026-10-06: **Owner: "Good, keep going."** The direction is approved.
  Still to finish:
  - PDFs;
  - remembering name corrections (aliases);
  - reconciling a holdings screenshot against an existing book;
  - tests and the live eval.
- 2026-10-06: **PDF library: pypdfium2** (owner's choice). It handles text
  PDFs and scanned ones.
- 2026-10-06: Built and verified, waiting for the owner's review:
  - PDFs, name memory and snapshot reconciliation are in.
  - Live eval: 10 synthetic cases (phone screenshots, three CSV styles, a
    text PDF, a scanned PDF, typed text).
  - The adversarial review found 10 issues, all fixed with tests:
    - unread screenshots weren't shown;
    - one failed scan sank a PDF's text pages;
    - a stray letter in a number cleared its "needed" mark;
    - bad dry-run input was a 500;
    - the upload size cap was missing in the page;
    - drops on the review added hidden files;
    - Yahoo was asked for the same ticker twice per import, with one
      replay per row;
    - two row-to-entry functions had drifted apart;
    - plus two duplications.

**Findings:**
- **The gold images were wrong first, not the model.** Chrome's
  command-line screenshot has a minimum window width, so a fluid phone page
  lost its right column (every amount). The model correctly left them blank.
  The gold pages are now a fixed 393 px.
- **The vision transcript's layout decides accuracy.**
  - Asked loosely, the model sometimes dropped each row's sub-line (type and
    date), and with section headers on lines of their own it sometimes
    dropped those too, and the year with them.
  - Now every row is one line: header | title | sub-line | amount |
    sub-line. That was 5 of 5 complete in samples.
- **The text model sometimes leaves a real entry out "because the ticker
  can't be confirmed"** (about one in eight), despite the prompt. A
  left-out line whose reason is doubt makes code read that block once
  more, naming it.
- **Shrunk screenshots lose digits:** 405.00 read as 405.05. Real phone
  screenshots arrive full size; never downscale before the vision call.
- **A NIM wait needs a deadline.** One eval call sat 46 minutes in the
  shared limiter and circuit breaker. `helpers.Deadline` now bounds every
  stage at 150 s (company search uses it too).
- An imported trade keeps the statement's share count (`trade` basis), so
  the dry run's wording divides by the split factor ("Sells 5 NVIDIA", not
  50).
- **Never put literal example values in a vision prompt.** Given "Buy · 12
  Mar, 15:42" as an example, the model wrote it into a scanned table and
  turned 12.03.2024 into a date with an invented time and no year.
- **The prompt must name the schema's own fields.** It said "amount" where
  the schema has `total`, so dividend cash sometimes landed nowhere.
- Dotted dates are day.month.year. A short block is read twice and the
  fuller reading wins: more rows first, then more cells filled.
- Final state: the live eval passed 30 of 30 across three full runs; the
  test suite has 1150 passing; `/verify` passed on a clean uv-tool install.
- **Second adversarial review (owner's request, after sign-off, before the
  PR): bugs fixed and code removed.**
  - **A long file failed outright.** 90-row chunks overran the 8,000-token
    reply cap (cut-off JSON, 3.5 minutes, then a refusal). Chunks are now
    30 rows and read three at a time: a 100-row sheet reads exactly in 47 s.
  - Excel chunks after the first lost their column names (a "# sheet" line
    headed the block).
  - Vision retries could outlast their deadline.
  - Apply could fire while the dry run was still pending.
  - A refused file's message was cleared by the good file added with it.
  - Clearing a date dropped its amber mark.
  - The one-logo test rejected the gold screenshots (`tests/data/` is now
    exempt).
  - **Removed:**
    - the server's row-to-entry twin (`entry_of`, `attach_problems`): the
      page builds the entries and asks `/import/check` itself;
    - the unused `kind` field;
    - the manual error conversion in the route (`ImportRefused` is a
      `BookError` now);
    - a duplicated no-network replay (`_held_now`);
    - a dead CSS rule and a dead row class.
  - Net: about 30 fewer lines.
  - A rare wobble remains: about 1 read in 20 of short typed text drops an
    entry even with two parallel reads. The review's "Left out" list shows
    it.

---

## Phase 6 — AI import II: screenshots and PDFs

**Goal:** drop or paste anything, anywhere on the page.

**Depends on:** Phase 5.

**Prototype questions:**
- The drop overlay and the paste behaviour.
- Whether a source thumbnail sits next to the review table.

**Build:**
- [x] A drop and paste target covering the whole page (wherever a composer
      shows).
- [x] PDF text extraction (`pypdfium2`, owner's choice). Scanned pages are
      rendered and read like screenshots.
- [x] Images go through the NIM vision model that won a live probe:
      `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`, thinking off.
      `nemotron-parse-2.0` was degraded; Llama 3.2 90B was slower and
      misread fields.
- [x] A notice under every composer that files are read by NVIDIA NIM.
- [x] Size caps, and a clear error for unreadable files.

**Verify:**
- [ ] The eval on synthetic broker screenshots and PDFs, run live.

**Feedback log:**

**Findings:**

---

## Phase 7 — Attention rail I: risk and events

**Goal:** the rail tells the owner what needs attention, calmly.

**Depends on:** Phase 4.

**Prototype questions:**
- Severity styling, and how many items show before "more".
- Where thresholds are edited (in the rail or in Settings).
- What the unread badge looks like on the topbar tab.

**Build:**
- [ ] X-ray rules, all thresholds editable:
      - a single name above N%
      - a sector or currency cluster
      - drawdown from cost or from peak
      - a move of more than 3σ in one day
- [ ] Events: earnings, ex-dividend dates, and News read divergence flags.
- [ ] Severity model, dismiss, unread badge.

**Verify:**
- [ ] Each rule fires on a crafted demo book and stays quiet on a calm one.

**Feedback log:**

**Findings:**

---

## Phase 8 — Attention rail II: plans and price alerts

**Goal:** alerts tied to the owner's own plan, plus plain price alerts.

**Depends on:** Phase 7.

**Prototype questions:**
- Where a holding's plan is edited.
- How a plan alert reads ("hit your 210 target, set on 3 Mar").

**Build:**
- [ ] Plan levels per holding: target, review/stop, maximum weight.
- [ ] Plain price-cross alerts.
- [ ] Dismiss, snooze and dedup.

**Verify:**
- [ ] Crossing, snooze and dedup on the demo book.

**Feedback log:**

**Findings:**

---

## Phase 9 — Notifications

**Goal:** high-severity items and a weekly recap reach the owner while the app
runs.

**Depends on:** Phases 7 and 8.

**Prototype questions:**
- The notification copy.
- The content of the weekly recap.
- Quiet hours.

**Build:**
- [ ] A background scheduler that runs while the app is open:
      - held tickers only, cache-first
      - respects the jobs single-flight rule and the shared rate limiters
- [ ] macOS notifications from the desktop app (`QSystemTrayIcon`, or the
      QtWebEngine notification permission), for high severity only.
- [ ] A weekly recap after the Friday close.
- [ ] A Settings switch.

**Verify:**
- [ ] A notification fires in the desktop app on a crafted alert.
- [ ] The recap renders.

**Feedback log:**

**Findings:**

---

## Next: thesis and AI advisor (new issue, its own roadmap)

The agreed shape is in Appendix C. Open the issue once Phase 4 is done. It
replaces issue #1's Phase 2.

---

## Appendix A — decisions agreed with the owner (Q&A, 2026-10-05)

| Topic | Decision |
|---|---|
| Placement | Its own page, the first topbar button, left of Portfolio. Self-contained: no links into the research tabs, and hypothetical weightings are untouched. |
| Layout | Split: performance and holdings on the left (~70%), the attention rail on the right (~30%). |
| Book | One book, no accounts. A future social "race against friends" will compare TWR %, so the TWR index is stored cleanly. |
| Headline | Value · Today $ and % · P&L (unrealised · realised · dividends) · TWR against the S&P 500 in pp · MWR p.a. with the timing effect (MWR − TWR). |
| Ledger | Buy, sell, dividend, deposit, withdrawal, fee, split. A quick-added holding is one synthetic buy. Every change can be undone. |
| Cost basis | Average cost, which matches what brokers show. |
| Columns | Core: ticker, qty, avg cost, last, day %/$, value, weight, unrealised $/%, realised $, total return. Extras: News and Market read, contribution, risk share, dividends and yield on cost, analyst upside. |
| Import UX | A composer that takes text and any file, then a review step with confidence marks and inline questions, then Apply as one batch. The prototype was approved. |
| Alerts | X-ray rules, events, plan levels and plain price alerts; thesis alerts belong to the advisor issue. In-app, plus macOS notifications for high-severity items and a weekly recap. No email until accounts exist. |
| Providers | NVIDIA NIM only. |

## Appendix B — engine notes (Phases 2–4)

- **Value the book with closes that are not dividend-adjusted.**
  `_bulk_close` uses `auto_adjust=True`. Valuing with it while also booking
  dividends as cash counts every dividend twice. Use `adjusted=False` together
  with Yahoo's dividend and split actions. The SPY benchmark stays total
  return. This goes into the CLAUDE.md gotcha index.
- **Cash** is one pool in `base_ccy`. A trade settles at the trade-date FX
  rate, or at the entry's own `fx_rate` when it has one. If a buy or fee would
  push cash below 0, the shortfall becomes an *implied deposit*. It is derived
  on every replay and never stored. This is what makes a book of quick-adds
  work.
- **Splits:** quantities are kept in today's share count. Each entry records
  `qty_basis`: `trade` (as on the contract note) or `current` (what the broker
  shows today).
- **TWR:** r_t = (V_t + W_t)/(V_{t−1} + D_t) − 1. Inflows count at the start
  of the day, outflows at the end.
- **MWR:** XIRR (`scipy.optimize.brentq`), or None when the cash flows never
  change sign. Under one year, compare period figures, not annualised ones.
- **Contribution:** daily P&L over the start-of-day capital, linked with
  Carino (extracted from `analytics._linked_contribution`), plus a "Cash and
  fees" bucket. It adds up exactly to the TWR.
- **Risk share:** RC_i = w_i(Σw)_i / σ², with Σ from
  `mpt.annualized_cov(…, "ledoit")`.
- **FX split:** the price part is (P·q − C_loc)·FX_now; the FX part is
  C_loc·FX_now − C_disp. Prices go through `helpers.price_in_major`, because
  LSE quotes are in pence.
- **Out of scope:** shorts (an oversell is refused), spin-offs, mergers and
  ticker changes. A holding with no price history is valued at its last trade
  price, with a warning.
- **Logs** record the operation, the entry id and the symbol only, never
  amounts, because users paste logs into public issues.

**Acceptance test** (synthetic AAA in USD; the numbers were re-checked
independently):

| Date | Entries | AAA close |
|---|---|---|
| 2025-01-02 | deposit 10,000; buy 40 @ 100, fee 10 | 100 |
| 2025-04-01 | deposit 5,000; buy 40 @ 125, fee 10 | 125 |
| 2025-07-01 | sell 30 @ 130, fee 10 | 130 |
| 2025-10-01 | dividend 50; fee 20 | 120 |
| 2026-01-02 | withdrawal 1,000 | 140 |

Expected:
- Average cost **112.75**, realised **507.50**, quantity **50**, unrealised
  **1,362.50**.
- Dividends **50**, fees **20**, total P&L **1,900** (= value 15,900 − net
  deposits 14,000).
- **TWR 12.629086%**, **MWR 13.848000%**, timing effect **+1.2189 pp**.
- Contributions add up to the TWR (to 1e-9), and risk shares add up to 1.

Companion tests:
- Trade basis versus current basis across a 2:1 split give the same result.
- GBp with FX: unrealised 975 USD = 650 price + 325 FX.
- A book of quick-adds only: TWR equals the price return.
- An oversell is refused.
- An edit that breaks a later sell is refused.

## Appendix C — agreed shape of the thesis and AI advisor

- **Thesis.** The owner writes anything from one sentence to an essay. The AI
  turns it into a *thesis card* (claim, drivers, KPIs, horizon, "I'm wrong if"
  conditions), which the owner confirms.
- **Investor profile.** Five questions: goal, horizon, the drawdown they can
  stomach, income need, constraints. They set the default maximum weight and
  stop rules.
- **Output.**
  - Per holding: a verdict from a fixed set (intact, watch, challenged,
    broken), up to 3 bullets that each cite evidence, and one bounded
    "consider" action tied to the owner's plan.
  - A weekly 3-point memo.
  - A behavioural coach drawn from the ledger (the disposition effect,
    concentration creep).
- **Quality.**
  - The maths is computed in code and passed in; outputs are typed JSON.
  - Any claim without a cited evidence ID is dropped. Evidence comes from the
    app's own data: fundamentals, analyst figures, the News read lenses, the
    Market read, price and drawdown, weight and risk share.
  - A devil's-advocate pass checks each verdict.
  - A gold-set eval script guards changes, and a track record logs every
    verdict.
- **Framing.** Thesis review and decision support, never "buy X now". This is
  a regulatory flag (SEC/FINRA, FCA, MiFID) to take to legal before v2.0
  markets it as an "advisor".

## Appendix D — research notes (2026-10-05)

- **Price alerts hurt retail investors.** Chen, Liu & Wen, *Alert for Alerts*
  (SSRN 4466498): price-tracking alerts cut returns by about 1% over 6 months,
  through extra trading. Hence plan-based alerts, a calm rail, and only
  high-severity notifications.
- **Feedback on their own behaviour** measurably reduces investors'
  disposition effect (the basis of the behavioural coach).
- **Trading 212** reversed a card-based portfolio redesign within 10 days
  because power users wanted dense lists. It shows MWRR, and its users asked
  for simple and per-holding returns plus monthly and yearly tables, and
  complained when numbers didn't match a hand calculation.
- **Capitally** separates FX from capital gains and benchmarks any subset.
  **Ghostfolio**'s "X-ray" offers static rules with thresholds the user can
  edit.
- **Kubera's AI import** lets you drop a file anywhere, review what it found
  and fix any wrong matches, and remembers those corrections.
- **Agentic UX patterns** (Smashing Magazine, 2026): show what will happen
  before acting, signal confidence, keep an audit trail with undo, and ask when
  unsure. **Microsoft HAX** guidelines: make clear what the system can do and
  how well. **Human-in-the-loop extraction** guides: show the source next to
  each extracted value, and direct attention to the uncertain fields.
- **NVIDIA NIM vision models** available on the owner's key (checked
  2026-10-05): `nemotron-parse-2.0`, `llama-3.2-90b-vision-instruct`,
  `nemotron-3-nano-omni-30b-a3b-reasoning` and `gemma-3-12b-it`.
