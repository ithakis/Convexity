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
| 1 | Page shell and look | Done (PR open) | `feature/my-investments-p1` | 2026-10-05 |
| 2 | Ledger and manual entry | Not started | | |
| 3 | Performance | Not started | | |
| 4 | Holdings depth | Not started | | |
| 5 | AI import I: text and spreadsheets | Not started | | |
| 6 | AI import II: screenshots and PDFs | Not started | | |
| 7 | Attention rail I: risk and events | Not started | | |
| 8 | Attention rail II: plans and price alerts | Not started | | |
| 9 | Notifications | Not started | | |
| → | Thesis and AI advisor (new issue) | Not started | | |

Status values: Not started · In prototype · Building · In review · Done.

## Next-session prompt

Paste this into a new Claude Code session to carry on:

```
Read docs/plans/my-investments-roadmap.md. Continue the My Investments roadmap:
take the first phase in the progress table that is not Done, read its section and
the feedback log, and run it with the phase loop (suggest → prototype → build →
verify → review → sign-off). Follow CLAUDE.md. Never use my real data.
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
   - Reason through the version bump and ask first (CLAUDE.md §15).
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
- [x] 988 tests pass, including `tests/test_investments_preview.py` (a real
      book never gets demo numbers; the demo book is coherent).

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
  light / dark / Bloomberg, and 983 tests passing. Waiting for the owner's
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
  `headline{value, cash, month_abs, month_pct, profit_total, profit_held,
  profit_sold, dividends, twr{period}, bench_twr{period}, mwr_ann}`,
  `positions[]`, `series{dates, value, invested, bench_value, twr_index,
  bench_index}`, `upcoming[]`, `attention[]`.
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
- [ ] `src/convexity/ledger.py`: a pure engine with no I/O.
      - Average cost, one cash pool in `base_ccy`, implied deposits.
      - Split factors with `qty_basis` set to `trade` or `current`.
      - Same-day order: deposit, dividend, sell, buy, fee, withdrawal.
      - `OversellError`. See Appendix B.
- [ ] `src/convexity/investments.py`:
      - `state/investments.json` (version, rev, settings, instruments, entries,
        journal of at most 100, redo).
      - Atomic write plus `.bak`; a malformed file is never overwritten (409).
      - `base_rev` conflicts return 409.
      - `ensure_instrument` via `resolver.resolve_symbol`.
- [ ] Routes:
      - `GET /api/investments`
      - `POST /api/investments/entries`, `…/entries/update`,
        `…/entries/delete`, `…/undo`, `…/redo` (JSON only)
- [ ] UI: quick-add holding, add transaction, editable ledger, Undo/Redo with
      a label and ⌘Z.
- [ ] Holdings table core columns: ticker, qty, avg cost, last, day %/$,
      value, weight, unrealised $/%, realised $, total return.
- [ ] Lifetime P&L split in the headline: unrealised, realised, dividends.
- [ ] `tests/test_ledger.py`: the acceptance test in Appendix B plus its
      companions.
- [ ] `tests/test_investments_store.py` and route tests.
- [ ] A test that the seeded demo book replays cleanly.

**Verify:**
- [ ] The acceptance numbers match.
- [ ] With curl on our own port: add, edit, delete, undo, redo; a stale rev
      returns 409; a text/plain POST returns 403.
- [ ] In the browser: the empty state, then the first holding, then an edit,
      then undo.

**Feedback log:**

**Findings:**

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
- [ ] `_bulk_close(adjusted=False)` with dividends and splits cached; the
      default path stays byte-identical.
- [ ] `fx.fx_multipliers`.
- [ ] Extract `analytics._carino_link`.
- [ ] Daily TWR and MWR (XIRR via `brentq`).
- [ ] Periods 1M, 3M, YTD, 1Y, 3Y and ALL; under one year, compare the period
      figures rather than annualised ones.
- [ ] `drawInvChart`:
      - Value mode: value, net deposits, and the same deposits invested in the
        S&P 500.
      - Return mode: TWR index against SPY total return, with the measure brush.
- [ ] FX display currency and the price-versus-FX split.
- [ ] Refresh on this page through the `__investments__` sink in `jobs.py`;
      research tabs untouched.
- [ ] `RICH_TIPS`: `inv:twr`, `inv:mwr`, `inv:timing`.

**Verify:**
- [ ] NVDA's 10:1 split (2024-06-10) values a quick-add dated 2023 correctly.
- [ ] Switching between USD and GBP.
- [ ] The S&P line in the chart matches SPY total return over 1Y.

**Feedback log:**

**Findings:**

---

## Phase 4 — Holdings depth

**Goal:** the holdings table becomes a monitor.

**Depends on:** Phase 3.

**Prototype questions:**
- Which extra columns are on by default, and how the Columns menu looks.
- How risk share and weight are shown side by side (bar pairs?).
- How suggested dividends are presented in the rail.

**Build:**
- [ ] Columns:
      - News read and Market read tier (`nsDot`)
      - contribution in pp (Carino-linked, plus a "Cash and fees" bucket)
      - risk share (`mpt.annualized_cov`, Ledoit)
      - dividends and yield on cost
      - analyst upside
- [ ] `INV_COLS` registry and Columns menu.
- [ ] Collapsible "Closed (n)" section, cash row and totals footer.
- [ ] Suggested dividends (Yahoo ex-date × quantity held), added with one
      click and never applied automatically.
- [ ] Export: "Positions" and "Ledger" sheets.
- [ ] `RICH_TIPS`: `inv:contrib`, `inv:risk_share`, `inv:yoc`.

**Verify:**
- [ ] Contributions add up to the TWR and risk shares add up to 1.
- [ ] Export opens in Excel with the new sheets.

**Feedback log:**

**Findings:**

---

## Phase 5 — AI import I: text and spreadsheets

**Goal:** the "Add or update" composer for natural language, CSV and XLSX.

**Depends on:** Phase 2.

**Prototype questions:**
- Composer placement (always visible, or on a button), and its wording.
- The review table: confirm the agreed prototype on real outputs.
- How snapshot-versus-transactions reconciliation is explained.

**Build:**
- [ ] Deterministic pre-parse (CSV and XLSX via pandas or openpyxl), then
      `nemotron-3-super` maps it to a strict JSON schema of ledger operations.
- [ ] Every ticker is verified through `resolver.resolve_symbol` and the symbol
      pack, the same rule as `search.py`.
- [ ] Review table:
      - a one-line summary
      - purple for inferred cells, amber for missing ones
      - the source shown on each row
      - at most 3 inline one-click questions, each with a stated default
- [ ] Apply as one undoable batch; alias memory (e.g. "Shell" maps to SHEL.L).
- [ ] Snapshot detection, reconciled against the current book.
- [ ] `scripts/eval_import.py` gold set with synthetic statements only, run
      live.

**Verify:**
- [ ] The eval passes live.
- [ ] Broker-style CSVs from at least three formats.
- [ ] Undo removes a whole batch.

**Feedback log:**

**Findings:**

---

## Phase 6 — AI import II: screenshots and PDFs

**Goal:** drop or paste anything, anywhere on the page.

**Depends on:** Phase 5.

**Prototype questions:**
- The drop overlay and the paste behaviour.
- Whether a source thumbnail sits next to the review table.

**Build:**
- [ ] A drop and paste target covering the whole page.
- [ ] PDF text extraction.
- [ ] Images go through the NIM vision model that wins a live eval among
      `nvidia/nemotron-parse-2.0`, `meta/llama-3.2-90b-vision-instruct` and
      `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`.
- [ ] A notice on first use that files are read by NVIDIA NIM.
- [ ] Size caps, and a clear error for unreadable files.

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
