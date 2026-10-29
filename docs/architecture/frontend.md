# Frontend

[← CLAUDE.md](../../CLAUDE.md) · part of the engineering notes; `§N` references name the original CLAUDE.md sections (map in CLAUDE.md).

Sections: §5 (frontend), §8 (CSS / theming), §11 (line landmarks).

## 5. Frontend (`src/convexity/static/`)

Real static files served by `server.py` — `index.html`, `app.js`,
`style.css`. No frameworks, no build step. KaTeX is the only external
dependency (loaded from CDN, used only for column-guide formulas).

### Pages and My Investments (`showPage`, `INV`)
- `showPage(name)` is the only page switch: `"investments"` (`#inv-page`),
  `"portfolio"`, `"news"` or `"table"`. It sets the active topbar tab and
  `body[data-page]`; on `investments` CSS hides every `.research-only` block
  (main table, column views, analyst section, Optimize). Refresh and R stay:
  there they refresh the book only (jobs.md, the `__investments__` sink), with
  their own tooltip; the research tab's red "needs refresh" cue is muted on
  this page. My Investments reopens on launch if it was last.
- The page renders `GET /api/investments` into `INV.book` with its own
  table (`#inv-tbl`, not `#tbl`/`COLS`: positions have a cash row and, later,
  closed lots). The page asks for `?ccy=FX_QUOTE` (and every POST sends
  `ccy`): the server computes the whole book in the display currency, each day
  at its own rate, so `fxSelect` on this page reloads it rather than
  rescaling. `book.ccy` equals `FX_QUOTE`, so `fxConvert` is the identity.
  The first load of a session reads years of prices ("Reading your prices…").
- The chart's period base indexes come from the server
  (`series.period_start`), the same base as the headline returns, so the two
  can't disagree. It is drawn at its pixel width (no stretched text). A
  Value / Return % switch redraws only the chart; the toolbar and reading are
  fixed-height single lines, so the card never resizes. It opens in Value and
  remembers the mode (`inv_mode`) and the pill (`inv_period`). Both modes
  have the drag-to-measure brush (`attachRangeBrush`, the Portfolio charts'
  own; owner, 2026-10-11), always measuring the TWR and SPY indexes, so money
  put in between two days never reads as a gain. Days are evenly spaced, so
  the brush maps pixels to days through `xInv`. The benchmark entry in the reading
  is a switch that hides or shows its line in both modes (`inv_bench_off`;
  a hidden line leaves the y-range and the brush). The value-mode benchmark line
  is the server's own per pill (`bench_value[period]`), starting on your
  value. A missing benchmark drops its line, never the chart.
- **The benchmark (owner, 2026-10-11).** "vs [S&P 500 ▾]" beside the Value |
  Return % switch is the Portfolios tab's popover (`benchMenuHtml`, which
  `benchSelectHtml` now wraps, and `wireBenchSelect`) over
  `INV.book.benchmarks`. A pick POSTs `{inv_benchmark}` to `/api/prefs` and
  reloads the book; it is never kept in the browser. Every label reads
  `invBench()` (the KPI sub-line, the legend, the brush, `inv:twr`,
  `inv:risk`). The line is red and dashed (`.inv-l-bench`, `var(--neg)`), the
  money put in stays a gray dash.
- **Performance: the top of the page (owner's layout A1, 2026-10-11).** The
  value, then last month's change on its own line ("▼ $2,326 (−1.0%) past
  month", the 1M window). Four KPIs: **Total profit** (lifetime, "not
  counting money you put in"), **Return · period** (TWR, with the S&P 500's
  return under it), **Change in portfolio value · period** (with the money
  put in or taken out under it) and **Volatility · period** (Sharpe and
  Sortino under it). There is no "vs S&P" cell and no "pp": the owner found
  Return and Your money side by side a duplicate, so Your money (MWR, "/ yr"
  from a one-year span up, `invMwr`) and the timing effect live in the
  Return card (`inv:twr`). Cards: `inv:profit`, `inv:twr`, `inv:change`,
  `inv:risk`. A book younger than the pill reads "since you started".
  Estimated dividends and the currency on cash are rows in the profit card.
- **The cards' formulas are LaTeX** (owner, 2026-10-11): `invTip(title,
  body, rows, tech, tex)` puts `tex` in a `$$\begin{aligned}…\end{aligned}$$`
  `.mt-formula` block that `_RICH_TIP`'s KaTeX pass typesets (and caches),
  with the owner's own figures in it: `texMoney` (money as `\text{}`, the
  sign in math so it is a real minus, `$` escaped) and `texPct`. `tech` is
  one plain muted line saying what the symbols mean. In a JS template
  literal every TeX backslash is doubled (`\\;`); a single one is silently
  eaten (`\;` became ";"). The Volatility card (mockup C) adds five fixed
  bands (`INV_VOL_BANDS`: <6, 6–12, 12–20, 20–30, 30%+) with yours lit, the
  benchmark's volatility in its band, and Sharpe marked on a red to green
  line from 0 to 2+.
- **The Attention rail (Phase 7, owner's layout B, 2026-10-13).**
  `invAttentionHtml` renders `INV.book.attention`: "Needs a look" (red group
  label, always shown), then "For your information" (info items, the book's
  warnings and Phase 4's dividends to add; folded after `INV_ATT_FOLD` = 3),
  then Upcoming (earnings and ex-dividend dates in the next N days). No boxes:
  kicker, group labels, hairlines. ✕ dismisses (`invAttPost`, never
  `invPost`: it must not bump `INV.seq`); "Change limit" (on hover) and the
  kicker's gear open Settings -> Attention (`renderSettingsAttention`, a row
  per rule: number and switch, saved on change). The topbar badge
  (`invBadge`): a count of unread items on `#inv-btn`, red when one is
  "Needs a look", gray otherwise, none while the page shows; read from
  `/api/investments/attention` 1.5 s after launch, and marked seen when a
  fresh page renders on My Investments (`invAttSeen`: only what is on
  screen, not behind Activity or folded under "n more"); leaving the page
  badges what wasn't read. An answer with `page: false` updates the rules
  only, never blanks the rail. The divergence item carries the ✦ in `--ai`.
- **Holdings order (owner, 2026-10-11).** A header click sorts (`INV_COLS`:
  biggest first, names A to Z), a second flips it, a third returns to "your
  order", where rows drag by a grip shown on hover (HTML5 drag on the `tr`,
  only while no column sorts). Both are per browser (`inv_sort`,
  `inv_order`); a holding missing from the saved order goes to the end in
  the server's order (by value). Cash stays last.
- **Holdings columns (Phase 4).** `INV_COLS` is a registry of objects
  (`k`, `label`, `get` for sorting, `dir`, `on` by default, `menu` wording,
  `cell`). On by default (owner, 2026-10-11): today's eight plus
  Contribution and Risk; News read, Market read, Dividends, Yield on cost
  and Analyst Δ start off. The Columns button beside "+ Add" opens a
  checklist (`invColsMenu`, kept per browser in `inv_cols`). `risk` is
  `merged`: it draws risk share as a blue bar under the gray weight bar in
  the Weight cell (`invWeightCell`, scaled to the biggest of either; risk
  blue is a fixed data colour, never the theme accent), exact figures in
  `inv:risk_share`. Contribution follows the period pill (`invContrib`),
  shown as points; its colour uses the rounded figure (`invPtsCls`).
  The `<tfoot>` holds Cash (with the "Cash and fees" contribution), a bold
  Total (value, 100%, Total profit, the period's Return as the
  contributions' sum) and a collapsed "Closed (n)" (`inv_closed`), so drag
  and drop only ever sees the holdings in `<tbody>`.
- **Headers and chart (owner, 2026-10-12).** Holdings headers are the
  "kicker" style (11px caps, `--th-ink`, a step brighter than `--muted` per
  theme); the header `button` must inherit `text-transform` and
  `letter-spacing` (a button resets both), and the sort arrow is absolute in
  the cell's right padding so every label ends on its numbers' right edge.
  The chart has more air (T 16, B 28, readout margin), SMA 20/50/200 pills
  (`invSmaPillsHtml`, the shared `CHART_SMA`; the average runs on the TWR
  index over the whole history, scaled back to value in Value mode so a
  deposit never jumps it), and "Money you put in" as a solid step line with
  a "Put in €Xk" label in a right gutter (R 84 when the card is 560px+).
  The value axis never goes below 0.
- **Opening fast.** `invLoad` paints `?cached=1` (the last good page, "Updated
  hh:mm · refreshing…", `.inv-stale`) and swaps in the fresh one; if the
  fresh one fails the note says "couldn't refresh" and the old figures stay
  labelled. All callers share `INV.loading`; `bookLoadWeights` awaits it
  instead of sending its own request (the two used to race at launch).
- **Suggested dividends (Phase 4).** One Attention item ("N dividends to
  add · $X estimated", `invDivsHtml`); Review lists them newest first with
  Add and Add all, both through `POST /api/investments/dividends` (one
  journal item). They are never added on their own.
- **Numbers are en-US everywhere** (`NUM_LOCALE`, owner 2026-10-11:
  "$238.488" read as $238 on a Mac set to a dot-grouping region). Every
  `toLocaleString` on a number passes it; dates keep their own formatters.
- **The Portfolios tab's book view** says what it is (the tab was renamed
  from "Portfolio" on 2026-10-11): under the stats,
  `bookTestNote()` reads "Today's holdings at today's weights, held through
  the whole period", with the actual return from `INV.book` for the same
  pill. The test is daily rebalanced at today's weights, so it has hindsight;
  on the owner's book (1Y, 2026-10-11) it read +17.1% against +8.5% actual:
  +9.2% buy-and-hold total return, +12.5% at the start mix rebalanced daily,
  +17.1% at today's mix (the owner kept the test, with this note).
- **Owner's rules for this page:** one number per headline cell, with details
  in `RICH_TIPS` hover cards (`inv:*`: plain words, technical name small);
  no dotted underlines or info icons; company names, not tickers; "past
  month", not daily, under the value (the chart itself is daily).
- **The ledger (Phase 2).** The rail toggles **Attention | Activity** (the
  `.pf-contrib-toggle` pill, remembered in localStorage `inv_rail`). Activity
  lists the entries newest first, grouped by month; a row click edits it in
  place, with Delete. "+ Add" (Holdings kicker, Activity header, empty state)
  opens `#inv-pop`, one form for every type (`invFormHtml`): quick add
  ("Holding": shares, avg price, Since, which is required and defaults to
  today) is a `buy` with `qty_basis: "current"`. The company field uses
  `/api/investments/lookup`; picking one fetches that day's close, which
  fills a buy or sell price and keeps following the date until the user types
  their own. Prices show in the share's quote unit (pence for LSE) and travel
  in the major unit. Every change POSTs with `INV.book.rev` and renders the
  payload it gets back; a refusal shows the server's message under the form.
  Undo: a toast with an Undo button (`toast(msg, {label, fn})`), Undo/Redo in
  the Activity header (the hover names the step), ⌘Z / ⇧⌘Z on the page when
  no field has focus.
- **The front door (Phases 5–6, owner's W1; restyled 2026-10-10).** The
  empty page *is* the composer (`invWelcomeHtml` → `invComposerHtml`, all
  inside `.inv-comp-wrap`, which `invComposerRefresh` redraws): a chat-style
  pill, the owner's pick from mockups ("like ChatGPT, keep the theme"). Files
  sit as chips above the text; the text grows with what's typed
  (`invCompFit`, capped at 220px, no drag handle); the clip and a round send
  button sit inside; Enter sends, Shift+Enter is a new line. Under it two
  starter chips ("Upload a screenshot", "Broker statement") open the file
  picker with their own `accept`. No "by hand" link on the welcome: typing
  in the pill *is* adding by hand (owner). "+ Add" on a full page opens the
  same pill, compact and without starters, in `#inv-pop`, "By hand" one
  click away. State is `INV.imp` (`text, files, busy, error, prop,
  answers`); sending posts base64 files to `/api/investments/import`. The
  topbar's portfolio name/cache line is hidden on this page
  (`body[data-page="investments"] .topbar .status`).
- **The review** (`invReviewHtml`) takes the page until Apply or Cancel:
  summary, screenshot thumbnails, at most three one-click questions, and an
  editable table, **one line per row** (owner's pick, 2026-10-10): the
  header is a light navy band (`--rev-head-bg`); the company is its name in
  bold, the ticker muted, and the exchange a link (`.inv-rev-ex`) that opens
  the same company's other listings with their currency
  (`listing_ccy`, `alts[].ccy` from the importer). A mark column on the left
  is the only AI colour: ✦ when the AI guessed something (its tooltip says
  what, and the source row), ⚠ amber when the row needs the owner; no purple
  text anywhere. The Type column appears only when an import has more than
  holdings. The read and every
  edit run `/import/check` (debounced, newest answer wins) so a sale the
  book can't cover is flagged before Apply, with "Add the N shares as held
  before" or "Leave it out"; Apply waits ("Checking…") while a check is in
  flight. `invRowEntry` is the only place rows become entries: the dry run
  and Apply both send what it builds; `invRowSet` is the one place a row is
  edited (inputs and menus). Apply sends the rows and the names the owner
  corrected (`aliases`) in one batch. A price or amount is one field with
  its currency sign (`.inv-rev-money`, a `label` that carries the border,
  hover and focus), the input bare and sized to its text (`invFieldCh`), so
  it reads "€48.46" like the rest of the app (owner, 2026-10-11: the sign sat
  far left of a fixed-width box). Each holding row sends `opening: true`;
  the server keeps it (`investments._clean`).
  **Every row has its own buy date** (owner, 2026-10-09: "rather than just
  edit for the whole portfolio"); the "Since when" question is gone
  (2026-10-10): performance counts from the oldest date entered. A holding
  the source gave no date starts today (`r.date_default`), shown muted
  (`.inv-rev-date.q`) until the owner picks its own date. A stand-in's note
  ("Yahoo has no Budapest line…, priced from …") is the one extra line a
  row can have, with the check/oversell notes; "On Xetra, as shown" is not
  shown, the exchange link already says it.
  **The review's menus are drawn by the app** (`invMenuOpen`/`invMenuClose`,
  one `.inv-menu` at a time, closed by a click outside, Esc, scroll or a
  second click): native `<select>` and `<input type=date>` were painted
  with black highlights by the desktop shell. The date picker
  (`invDatePick`) goes year → month → day, built for speed (owner: "super
  optimized for speed of input"): 12 years per page, future months and days
  disabled, "Don't know the day: use the 1st", Today and "Same as above";
  every pick closes it (the owner chose no auto-advance). **While a menu is open, `invRender` defers** (the
  background `/import/check` would otherwise redraw the picker away under
  the cursor); closing the menu runs the deferred redraw, and a row click
  re-finds its button after it.
- **Adding by hand and editing (owner's E2).** `invFormHtml` is a card: the
  company as a header once chosen (initial, name, ticker, today's close via
  `invNowFill`), big number fields with the unit in the label, date chips
  (Today, 1 year ago, the picker as the real `date` field) and a live result
  line (`invResultText`, updated in place on input so typing never loses
  focus). The same card edits an entry in Activity. Other types sit in the
  quiet "Something else" menu.

### Notifications and the weekly recap (Phase 9)
- `notifyPoll()` reads `/api/notifications?since=` every minute while the
  page is visible (the first read only takes the cursor: no backlog). On
  new items it reloads the book on My Investments (or refreshes the badge
  elsewhere); in browser mode only (`native: false`) it shows a toast
  (`#notify-toasts`, max three, 20 s). `window.invFocusItem(key)` is the
  click-through (the desktop app calls it through `runJavaScript`):
  `showPage` via `#inv-btn`, wait for `INV.loading`, unfold "n more" when
  the item is folded, scroll to `[data-att-key]` and flash it; `__rail__`
  is the rail's top, `__recap__` opens the recap (`#inv-recap-bg`, through
  `showOverlay`). The rail shows "Your week to …" until it is opened
  (`recapSeen`, localStorage: a per-viewer convenience) or a week passes.
  Settings → Notifications reads and writes `/api/prefs`, never localStorage.

### The book in the Portfolio tab (`BOOK_KEY = "__book__"`)
- When the book holds anything, `renderTabs()` pins a red pill "My
  Investments" first (`.pf-tab-book`, colour `--book`, the theme's red). It is
  a **read-only mirror**: constituents are the open positions
  (`/api/investments/symbols` at startup, then `bookTabSync()` after every
  change on the page), and its default weight mode is `book` (today's market
  values, cash excluded), with Equal and Cap still available. Analytics, News
  and Optimize run on it; rename, delete, constituent edits, company search,
  presets, MPT save/apply and saved MPT runs are gated (`viewIsBook`), and the
  server refuses the name on every portfolio write route except the cached
  rows (`Handler._writes_book_name`). A changed book rebuilds the rows on
  activation instead of turning Refresh red. The xlsx export skips it.

### State (`STATE`, `DATA`, `VIEWS`, `WATCHLISTS`)
- `DATA` — currently-rendered rows
- `VIEWS` — server-side view metadata (mirrors `/api/views` response)
- `WATCHLISTS` — `{name: entries_string}`
- `STATE.activeView` — current portfolio name, or `AD_HOC_KEY`
  (`"__current__"`) for the unsaved tab
- `STATE.mode` — `"equal" | "cap" | "custom" | "preset:<name>"`. `"custom"`
  is the legacy ad-hoc path (anonymous, Apply-without-Save). Named modes
  use the `preset:` prefix and resolve via `STATE.weightPresets`.
- `STATE.customWeights` — `{symbol: fraction}` after the user applies the
  weights popup without saving (ad-hoc Custom only)
- `STATE.weightPresets` — `[{name, weights, saved_at}]` for the active
  portfolio. Loaded by `loadPresetsForView()` when the tab changes; lives
  inside the view JSON on disk.
- `STATE.period` — `"1M" | "3M" | "6M" | "YTD" | "1Y" | "3Y" | "5Y" | "MAX"`
- `STATE.analyticsByTab` — `{tabName: {cacheKey: result}}` — keeps each
  tab's analytics warm so switching tabs is instant. Cache key is
  `${mode}|${period}|${fxQuote}` and `mode` may be `"preset:<name>"`.
- `STATE.fitColumns` — boolean toggle for the optional "Fit to screen"
  table mode. When enabled, the frontend scales column widths, font size,
  and chart cells down just enough to keep the active view inside the
  current table width. Preference lives in `localStorage.fit_columns`.
- `MPT` (separate top-level) — overlay state: `{result, selectedIdx,
  hoverIdx, view, runs, busy}`. `result` is the latest
  `/api/efficient-frontier` payload; `selectedIdx` is the frontier index
  controlled by the slider.
- `SORT` — `{key, dir}` for the row table

### Column registry (`COLS` + `BUILTIN_VIEWS`)
`COLS` is the single source of truth for every available
column — `key`, `label`, `w`, `align`, `sortable`, `render(r)`, optional
`heat`, `bg`, `sortValue`, `td_cls`. A `heat.kind === "yo_dyn"` column exposes
a per-view background mode in Customize: **Off / 2C-Quantile / Quantile /
Min-Max** (`getHeatMode`/`cellStyleHeat`). "Quantile" = single-blue quintile
buckets, "2C-Quantile" = orange↔blue diverging quintiles, "Min-Max" =
continuous 10th/90th-clipped blue. `favor:"low"` flips which end is best (e.g.
`analyst_rating`: 1 = Strong Buy reads blue). The old `"percentile"` mode +
`kind:"yo"` fixed-orange ramp were removed; a stored `"percentile"` is rewritten to
`"quantile"` by `persistence._sanitize_heat`. New modes must also be whitelisted in
`persistence._HEAT_MODES`. `BUILTIN_VIEWS` (just below COLS)
maps each preset (`Default`, `Fundamentals`, `Momentum`) to an ordered
list of column keys.

**The column bar is one flat row of chips (v1.11.1).** `renderColumnViewBar()`
emits the three built-ins, a `.cv-seg-div` hairline, then every custom preset
(`customViewNames()`, **creation order** so a new one lands at the end) as a
`.cv-custom-chip` in `var(--pos)`. The `Custom ▾` dropdown is gone; deleting a
preset lives in Settings → Column Presets (`deleteCustomView`), so the bar holds
no destructive control. The two green rules must stay **below**
`.cv-seg button.active` in `style.css` — the first is the same (0,2,1)
specificity and source order is what breaks the tie.

**Built-ins are edited in place and saved instantly**, so the amber pill is
purely informational and the two predicates deliberately differ:
`builtinIsModified()` (differs from factory — drives Settings' Revert, always)
vs `builtinShowsDirtyPill()` (that, minus an `acked` flag — drives the pill).
**Save = acknowledge**, not "write": `ackViewOverride` → `POST
/api/column-views/builtin-ack` only sets `acked`. Any later edit must re-arm the
pill, which is free in `upsert_column_view` (it rebuilds the entry) but has to be
done **by hand** in `set_builtin_view_heat` (it mutates one). An override
carrying only `acked` is dropped on read, preserving the
empty-override-disappears invariant. Reset/Revert is still
`DELETE /api/column-views/<name>`; `resetViewOverride(name)` takes an optional
name so Settings can revert a preset without switching to it.
**Wire these through arrow functions** — `onclick = resetViewOverride` passes
the MouseEvent as the `name` argument. The pre-1.4.5 names `IB View` / `Trader View` are no longer mapped. `COLS_BY_KEY` is the lookup table; rendering goes
through `getActiveColumns()` which resolves the active view from
`STATE.activeViewName` / `STATE.customViews` / `STATE.activeColumnOverride`
(set when the user drags headers on a built-in preset — kept
in-memory until they Save-as-new or Reset). User-defined views persist
to `<data>/state/column_views.json` via `/api/column-views`.

**To add a new column**: append an entry to `COLS`, add a `COL_INFO`
tooltip, and (if it belongs in a preset) include its key in
`BUILTIN_VIEWS`. The XLSX export auto-discovers row-payload keys via
`HOLDINGS_PRIMARY_COLS` + the extras pass — add it to that list (or to
`HOLDINGS_SKIP_EXTRAS` if it's duplicated elsewhere, e.g. analyst
columns). An exported column also needs a `COLUMN_DEFS` entry (and an
`EXTRA_LABELS` header if it is an extra) for its Excel header comment and the
Definitions sheet; `tests/test_xlsx_export.py` fails until it has one.

### Metric explanations (hover tips) — one registry per surface
Every number the app shows explains itself on hover; the registries are:

| Surface | Registry | Rendered by |
|---|---|---|
| Holdings table headers | `COL_INFO` (plain text) | shared `[data-tip]` (`_APP_TIP`) |
| Analytics panels (Risk & return, Valuation, Concentration) | `METRIC_INFO` | `statRowHtml` → in-place `.pf-metric-tip` |
| Stock detail modal grids | `DETAIL_METRIC_INFO` | `renderDetailMetricGrid` → in-place `.pf-metric-tip` |
| Optimize side panel | `MPT_METRIC_INFO` (functions: read α / `n_boot` at hover) | `[data-rich-tip="mpt:<key>"]` |
| Optimize Guide → Compute budget card | `MPT_BUDGET_INFO` (mirrors `frontier._BUDGETS`) via `RICH_TIPS["mpt-budget"]` | `#pf-mpt-info-budget` (filled at load; the chip itself has a one-line `data-tip`) |
| Contribution table headers | `CONTRIB_INFO` | `[data-rich-tip="contrib:<key>"]` |
| Excel export | `xlsx_export.COLUMN_DEFS` / `METRIC_DEFS` (plain-text mirror) | header comments + Definitions sheet |

`.pf-metric-tip` entries are `{formula (KaTeX), desc, range}` through
`metricTipHtml()`. The in-place card is absolutely positioned inside its host,
so a scrolling ancestor clips it; hosts inside the Optimize overlay (side panel
is `overflow-y: auto`) and anywhere else that clips use **`[data-rich-tip]`**
instead: `_RICH_TIP` renders `RICH_TIPS[key]()` into one body-level fixed box
placed by `placeTip()`, KaTeX cached per HTML string. KaTeX fonts load on the
first formula shown, so the very first hover can paint the formula a beat
late. `tests/test_frontend_tips.py` checks every `COLS` key has a `COL_INFO`,
every detail-grid label a `DETAIL_METRIC_INFO`, every `data-rich-tip` key a
provider, and that the budget tip's numbers match `frontier.py`.

### Contribution to return (`renderContribHtml`)
Rows are `analytics.contribution` (Carino-linked, sums to the period return)
plus two client-side columns: **W×R** = weight × the holding's own period
return (pp) and **Compounding** = contribution − W×R, so each row reconciles
exactly. Sort (`pf_contrib_sort`) is a localStorage convenience; the Table |
Chart toggle is `pf_contrib_view` in prefs.json (chart by default, the
owner's call 2026-10-19), read through `CONTRIB_UI.view`; clicks re-render only
`#pf-contrib-body`. The chart is an HTML/CSS horizontal waterfall
(`renderContribChartHtml`) in contribution order, ending in a Total bar.

### Portfolio panel and constituents
`#input-panel` is a transparent wrapper: the `.pf-tabs` pills sit on the page,
and so does the open portfolio (`.pf-body`, spacing only since v1.19).
**At most two levels of box (the user's rule, v1.19).** The page used to nest
four: gray `.pf-body` → Analytics box → `.pf-card` → content. Now the
"Constituents" and "Portfolio analytics" headings are the HOLDINGS-style
`.section-anchor` kicker rule, and `.pf-analytics` is the ONE sheet; inside it
the `.pf-card`s are borderless cells separated by hairlines (each cell draws its
top rule, the left column the vertical one; `nth-child` rules in style.css,
collapsed under 900px). Don't give a cell a fill or border again — add a
hairline. Since v1.19.2 the sheet is the News cards' gray (`--bg-subtle`) and
the dividers are inset `::before`/`::after` hairlines that stop short of the
sheet's edge; the plot sits in `.pf-chart-card` (white `--bg-canvas`, rounded),
which is the second and last box level — nothing goes inside it. Bar tracks on
the sheet use `--bg-canvas` so they still show on the gray. Section kickers are
11px/800 with 2px rounded rules. A scripted audit (bordered box ≥160×60 inside another) found no
nesting anywhere in Portfolio or News after the change. There is no text editor: the constituents are the entries string
`ENTRIES` (what `/api/watchlists` stores), shown as `.pf-chip`s by
`renderConstituents()` — the row's name when a `DATA` row matches the entry,
else a name remembered from a search card (`ENTRY_NAMES`), else the entry.
Every edit goes through `commitEntries(next)`: it saves the watchlist at once
(the server marks the view stale), and nothing is built. `refreshNeeded()`
— entries set and the view missing, empty, stale or saved from other entries —
drives `#refresh.needs-refresh` (red) via `syncRefreshNeeded()`, called from
`renderEditorMeta()`. Refresh runs the background job, which saves the view;
`rfFinish` then re-reads the active view's rows (the live patch only upserts,
so removed constituents would otherwise stay) and the red clears. Switching to
a stale tab does not rebuild it; only rows saved before a new column existed
still rebuild by themselves. The unsaved tab (`AD_HOC_KEY`) is labelled
`untitledName()` ("Untitled N") and is saved under that name by its first
edit, because the server keeps no empty watchlists; removing the last chip is
refused. The Build / Update Portfolio / Save as new buttons, the textarea and
the analytics stale banner were removed in this change.

**Field + tags, and the fold (owner, 2026-10-18).**
- **Search box.** `#co-form` is a filled field at rest (`--field-bg`, no
  accent outline) and draws its accent ring on focus only.
- **Chips.** They are borderless fills (`--chip-bg`) with the ticker as a
  recessed badge, so they read apart from the canvas.
- **Tokens.** Both are tokens in all three themes; Bloomberg's are neutral,
  not its selection blue.
- **The fold.** The "Portfolio Constituents" kicker is a button
  (`#pf-chips-toggle`) that folds the chips away.
  - Folded, it keeps the count and shows one mono line of tickers
    (`#pf-chips-sum`).
  - One global choice, `pf_chips_collapsed` in `prefs.json`, so the desktop
    app keeps it. An empty list never folds.

### σ/μ in the stock window (app.js "σ/μ: the real-world price distribution")

Owner-approved design (mockup v7, 2026-10-17). Two toolbar toggles after
Volume, `DETAIL.showSigma` / `DETAIL.showMu` (1.29): σ on at every open, μ on
unless the owner turned it off (prefs `dist_mu`, `fcMuDefault`, saved by the
toggle). σ+μ = the options' σ around the model price (CAPM + the analysts'
α, `capm.py`); σ = pure CAPM; μ = the model path alone, no randomness;
neither = the old chart. While either is
on, `renderChart` ends the history at ~58% of the plot (`padR`) and spans
the **linear** y axis over the window's history ∪ the end curve's 5%..95%
(`fcYRange`, rule "B · Balanced", owner 2026-10-18: it was 3–97%, which on 12M
set both limits so 1Y and 5Y looked identical). The axis was log until
2026-10-20; the owner read its 9 → 28 → 89 → 276 → 859 ticks as broken, so it
is linear like any price chart, with round 1/2/5 × 10^k ticks in the display
currency, and the densities are drawn **per unit price** (`sim.pp` = p / S;
the payload's curves are per log price), which shows their real right skew.
There is 5% padding plus `FC_LANE` px of headroom for the E/D flags. A change of domain
eases over ~280 ms (`fcTweenDomain`, final-state redraws, a slow frame snaps).
The density's mass beyond the frame is labelled ("▲ 4% above",
`fcDraw`). And
**One load** (2026-10-19): `openModal` starts `/api/detail` and the
options together (`ensureDensity()` keys on `DETAIL.row`, and `DETAIL.seq`
drops answers for a window that moved on; `closeModal` bumps it too, or a
load finishing after Esc rendered into the hidden window) and draws once
behind one "fetching detail and options" chip, waiting for the options at
most 8 s from opening; slower ones arrive on their own. The two horizons are
fetched one after the other (the shown one first), never together: the
server's chain cache is unlocked, so parallel requests downloaded every
expiry twice. `ensureDensity()` returns a promise that settles when the
window can draw (the first horizon, or both when it falls back to 3M). **Axis labels are in the
display currency**: everything is plotted in the listing's own currency, and
`fmtTickVal` converts the ticks and today's tag like every label (NVDA's axis
read $214.1 beside a EUR header and EUR targets, so "Low €191.24" looked level
with a price it was above). The analysts' low..high is inside the y-domain
when shown, the marks' labels are laid out inside the plot (pushed
down from the top, then up from the bottom), and the chart redraws on window
resize (a timer, not rAF, which stalls in a background window). Hovering a
readout cell lights its part of the fan and the density (`DETAIL.fcBand`:
`up`, `mean`, `50`, `90`, `20`, `cons`, `p20`); there is no (i) badge, the whole
cell is the target (owner, 2026-10-20).

**One model price, one Street reference** (1.29, owner's picks: option A,
option 3). The mean is the app's model (`capm.py`): μ = rf_local + β·ERP + α,
the curve is the options' shape tilted once to S0·e^(μT), and the payload's
`model` block carries both μ on and μ off, so the toggle never refetches.
`fcMode` picks `curves.model` / `curves.market` (`rn` under Q); `fcModelAt`
gives μ and the model price with the server's formula, so the pill, the cell
and the grid agree to the cent (the cell shows the model price itself, never
the discretised curve's mean, which differs in the 4th digit). The path ends
on one white **"Model price" pill** right of the spine ("· CAPM" with μ off,
"Forward" under Q). The readout cell is named by the expiry's date
("Model price · 17 Sep 27"): the grid's "Model 12M" is the same μ over exactly
one year, and both numbers are in its tip with the decomposition
(`rf + β×ERP + α = μ`, KaTeX). The Street's 12-month mean target (`fcMarks`:
mean, n, low/high, in the window's currency) is a **reference only**: on 12M a
hollow amber diamond at its own date with "Street target (12M) $X · not a
forecast" (a canvas-coloured halo: it sits over the paths), the
Pr[S ≥ Street] cell, and on 3M the inset. Gone in 1.29: the trust control,
the per-firm mixture, the solid "At expiry" diamond, the low/high bar, the 3M
pace line and the μ-only firm lines. Beta replaced the Dividend Yield cell
(value, proxy and window; "prior" when it is the fallback); the yield is in
the footnote. On 3M the year is on a true clock in an inset over the history
(`mountFcInset` / `fcDrawInset`, its own canvas `#m-fc-in`): the model's
path S0·e^(μt) through the main chart's model price to 12 months, the Street's
pace to its target (hollow diamond), and the 12M payload's curve (same μ) for
the 90% band. It sits in the free corner the price crosses least. The dashed
line is the **mean path** `sim.meanPath` (the paths' average per step, pinned
to end on the curve's mean along the variance clock). The hover's dates count back from the expiry
(`fcDateAt(days, d)`): `d.days` runs from the options' session, not today.
`mountForecast` lays a canvas (`#m-fc-cv`, `pointer-events: none`) and a hit
layer (`#m-fc-hit`) over the future zone and density; the history keeps its
own crosshair. Rules that matter (owner feedback 2026-10-17 folded in):

- **Today's price is a gauge**: an SVG dashed level across the whole plot and
  a filled tag on the left axis (`yTagSvg`, the tick under it hidden); the
  future-zone hover moves a second, accent tag and level (`#m-ytag-hov`). There
  is no right-hand tag gutter (`FC_ZONE.tagW` 12) and no Replay button.
- The horizon pill reads `3M | 12M`; the expiry is the axis label under the
  density, **12M** · 17 Oct 27 (`fcFmtShort`).
- **E/D flags** (`renderEventMarkers`, `openEventCard`): a layer over the
  finished chart from `/api/events`, so its late arrival never restarts the
  animation. E lane at the top, D just under; faint dashed rules; E green for
  a beat, red for a miss, D accent. Flags under 16 px from the previous one in
  their lane are skipped. The card measures reactions on `DETAIL.data.history`
  (daily, dividend-adjusted, dates via `evDay`: +12 h, UTC date, so a
  European midnight bar keeps its day): first session, 5 sessions, S&P 500 the
  same session and the excess; a D's price move is the total return minus
  amount / previous close (≈, the closes are adjusted).

- **Paths are bridges, not GBM** (`fcSimulate`): ends are stratified inverse-CDF
  draws from the drawn curve, so the end marginal is exact; the clock includes
  the options' earnings jumps. Seeded by symbol/horizon/settings, so a redraw
  is the same picture. Touch chances use the Brownian-bridge crossing
  correction between steps.
- **Animate only a new picture** (`fcPlay` keys on symbol, horizon, mode and
  settings): 750 ms paths then 300 ms ease-out density, no histogram stage;
  a resize or range tab redraws the final state; reduced motion skips it.
- **The hover box sits left of the density** (and left of the analyst chips
  on the curve) and snaps to the low, mean and high targets at the expiry.
- 12M is the default; when the 12M chain fails the gate and 3M passes, the
  window switches to 3M once (`DETAIL.fcAuto`) and the footnote says why.
- Readout labels name the horizon and the threshold (owner, 2026-10-18):
  `Pr[S_12M > €35]` (S0), `Pr[S_12M ≥ €40]` with "mean target" or "+20%" in
  the value ("Street target" since 1.29). The threshold is in the display currency, from
  `fmtMoneyThreshold`: whole units from 10 up, two significant figures
  below.
- Readouts replace `#m-range-info` in `#m-fc-ro`: Pr[S_T > S_0] (KaTeX
  labels: style only `.m-fc-grid > div > span`, never every span, or KaTeX's
  own spans stack), the model price (or the forward under Q), median and
  ranges, "Falls 20% or more" (by the end · at some point), Pr[S_T ≥ the
  Street's 12-month target] (or ≥ 1.2 S_0), Beta and Hist. / Imp. Volatility
  (payload `vol`); each cell's `data-tip` explains it and its `data-tip-tex`
  leads the tip with the formula. The curve's own σ and the dividend yield are
  in the footnote. Its **"Model & assumptions"** link opens Settings → Model
  price (`renderSettingsModel`: the formula, why the home index / BSWA / 0.9
  shrink with the backtest's numbers, why α is small, κ, what it does not
  claim, the open window's own numbers, and the parameters). Every model and
  distribution save goes through `wireModelPrefs`, which clears `DETAIL.fc`
  and re-prices the grid (`requestModelPrices(true)`).
- **The model price card** (2026-10-28, the owner's pick after four rounds of
  first-time-user critique): hovering the window's "Model price" readout opens
  `RICH_TIPS["model-price"]` → `modelCardHtml(d)`, not a text tip. Top: both
  equations filled in (P_T = S0·e^(μT), then μ coloured by input), a sentence
  and one bar building μ in % and in money; then a 2×2 of input cards (r_f,
  β, ERP, α), each a chart plus Why / Used / What if rows; footer: the
  display currency and "not a forecast". One colour per input, `--mp-*`
  (fill) and `--mp-*-ink` (text) in all three themes; KaTeX `\color` needs a
  literal, so `mpColor()` reads the ink at hover time. The parts are rounded
  so the equation adds up as written; the price uses the unrounded μ. A
  negative part (a negative beta) stacks left of zero with the net μ marked.
  The charts read `MODEL_AUX`, filled once per window by `loadModelAux` when
  a density lands: `/api/risk-free-history` (the US bill only; other
  currencies show the rate chain with the used step lit), `/api/erp-history`
  and `/api/beta-scatter` (`capm.scatter`: the last 250 pairs the beta was
  measured on, weeks for a weekly beta). A chart whose data has not arrived
  is left out; the numbers never depend on it. Money goes through `fmtMoney`,
  so prices are in the display currency, and the footer says so when it is
  not the listing's.
- **The model price card's motion** (2026-10-29, the owner's picks from
  chat demos): `TIP_MOTION["model-price"]` = `mpOpen` / `mpClose`, a hook in
  `_RICH_TIP` any rich tip can use. The card unfolds from the edge next to
  the price (down when it sits below it) and folds back into it; the bar
  assembles while the price counts up; then r_f draws its year to a
  rippling dot, β's days burst out of the origin in six layers (six
  animations, not one per day), ERP's marker slides in from 3% and snaps,
  and α's curve blooms. The hooks are `mpa-*` classes and `data-*` in the
  SVG builders. Web Animations with fill "backwards", so a finished card is
  exactly the static markup (selection, re-render and close never meet a
  half-animated state); `hide()` waits for the fold, a `show()` meanwhile
  cancels it, and a 400 ms timer hides it anyway on a page that isn't
  drawing (a minimised window never finishes an animation).
  `prefers-reduced-motion` keeps the plain fade.
- **The grid's Beta / Model 12M / Model μ** come from `POST /api/model-prices`
  (`requestModelPrices`, called with every analytics request, keyed on the
  rows' symbols and prices): never on the streaming path, merged into DATA,
  then `render()`. A saved row's figures are from the day it was saved, so a
  new price re-prices them all. Requests overlap (tab switches, the warm-up, a
  refresh), so there is no "newest wins": each answer merges into the rows it
  was priced for (same symbol, same price); a refresh's swapped-in row keeps
  the previous numbers until re-priced (`upsertDataRow`), and finishing a
  refresh of the My Investments mirror re-prices it (`rfFinish`). A built-in
  override whose columns equal a retired factory list reads as factory
  (`persistence._RETIRED_FACTORY_COLUMNS`), so new preset columns appear for
  an owner who only changed heat.
- **Every source says what it is** (`renderFcReadouts`, keyed on the payload's
  `source`, `quality.smile` and `fx`): a surface fit names its quotes over
  all expiries; an FX layer is "+ FX" in the σ label and the pair, vol and ρ
  in the footnote; the history estimate says why options were not used
  (`fallback`), its β, proxy, weeks and rate, and has no
  risk-neutral curve (`fcMode` falls back to the market's μ under Q). The
  footnote's curve σ is the drawn curve's own width (`var_x / T`), so it
  includes FX.
- The header line is `modalMetaHtml`: industry (sector on hover) · City
  (Exchange) from `EXCHANGE_PLACES` by Yahoo code · currency. The detail
  sections keep their metric tips without the dotted underline.
- Colours: data stay `--pos`/`--neg`/accent; analysts use `--amber`
  (`--amber-rgb`, `--on-amber`) defined in all three themes.

### Company search (`#co-search`, app.js "Company search")
One box above the constituents posts to `/api/search` (backend.md §6). An
"✦ AI" badge (`#co-ai`) sits left of the text when the model read the query;
the chips row starts with the plain kind ("Screen ·"). The ranking's reason
and the $1B floor are the rank chip's tooltip, never extra lines. Cards show
the name, then the ticker and exchange in gray, then market cap, 1Y and each
criterion. **Show more (N)** opens `#co-full-bg`: every result as one table
(`FULL`, `coFullOpen/coFullLoad/coFullRender`), loaded 25 rows at a time as it
scrolls (`{query, offset, limit}`), sortable by any column over the rows
loaded; selection is `CO.picked`, keyed by the result's position in the
server's order, so cards and table share it.
The response's `query` is kept verbatim in `CO.query`: the chips render it
(purple = chosen by the AI), a chip's × or its inline editor changes it and
posts `{query}` back, which the server validates and runs without the LLM.
Cards (top 5) are selectable; "Also listed on" swaps a card to another
listing of the same company. **Add** passes the picked tickers to
`commitEntries` (Refresh turns red; their rows load on the next refresh). A
pasted list (`kind: "list"`) comes back with every new card preselected.
**Clear** (`.co-clear`, first in the header row; muted text that becomes a
pill on hover) runs `coClear()` and empties the box.
Tickers already in the portfolio show as "In this portfolio" and cannot be
picked.

### Key UI behaviours added in Passes A/B/C
- **Overlapping builds**: `build()` (now only the new-column rebuild on a
  tab switch) can still overlap a refresh job or another tab's build. `BUILD_GEN` means only the newest build writes `DATA`, saves and
  requests analytics; a superseded one cancels its reader and leaves
  progress/disabled state alone. `BUILD_STREAMING` keeps refresh-job row
  frames out while a build repopulates `DATA`. Both write through
  `upsertDataRow()`, never `push()`. Before this, overlapping writers left
  symbols in `DATA` twice and the build persisted them (§4 "One row per
  symbol").
- **In-place tab rename** (2026-10-19): double-click a `.pf-tab-label`, or
  right-click the tab → Rename (`tabMenuOpen`, an `invMenuOpen` menu), and the
  label itself becomes editable (`contenteditable="plaintext-only"`, no input
  box); Enter or a click away commits, Esc restores; a taken name shakes and
  reverts. Two rules make it survive: a click on the already-open tab does not
  re-run `activateTab` (its awaited redraw used to land between the two clicks
  of the double-click), and `renderTabs()` defers while `TAB_RENAME` is set,
  redrawing on finish. The unsaved "Untitled N" tab renames too: the name is
  held in `STATE.adhocName` and used by its first save (`untitledName`), or
  saved at once when it is the open tab and already has companies (`ENTRIES`
  are the open tab's). Names clash case-blind, "My Investments" included;
  Enter during IME composition doesn't commit; ⌘-Tab away keeps editing; a
  click that ends the edit is not lost to the redraw (it waits for pointerup).
  The rename hint shows on the open tab only; every label has its full name
  as a native title.
- **Constraints popover** (`tradingPopOpen`, the portfolio chart): an
  `invMenuOpen` panel, right-aligned under its button (`invMenuPlace(..,
  right)`), at most the window's width. `invMenuOpen` treats a redrawn button
  with the same id as the same anchor (the analytics redraw replaces
  `#pf-trading` while the panel stays open; the render hands the panel the new
  button and re-places it). `tradingControlsRender` builds the pills once and
  wires its own clicks; `tradingControlsSync` marks the setting without a
  rebuild (a rebuild under the pointer swallowed the click). The bps field
  commits as you type (350 ms), blank or junk reverts; `tradingSet` debounces
  `requestAnalytics` (250 ms). `savePrefs` saves one change at a time, in
  order, and keeps queued changes over an older answer.
- **Loading chip** (`lc-anchor`, `lc-spin`, `lcHtml`, `lcShow`,
  `lcHide`): a small terminal-flavoured indicator. Braille spinner cycles
  ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ via CSS keyframes on `content`. Optional shimmer bar +
  tabular-numerics counter (e.g. `21·47`). Used in: streaming status,
  startup, analytics empty state, modal detail load, FX hover load,
  Excel export button. CSS (`.lc-*`) in `style.css`, JS helpers
  (`lcHtml`/`lcShow`/`lcHide`) in `app.js`.
- **Export → Excel** (`exportXlsx`): client just hits
  `/api/export-xlsx`; server handles everything. Button shows the
  loading chip during the ~40s cold-cache export. The maintenance
  contract for "what goes in the file" lives both in the inline HTML
  comment next to the button AND in `xlsx_export.py`'s module docstring.
- **Topbar + column-bar hover copy**: actionable hover text on
  `Portfolio`, `Refresh`, `Export`, `Sort`, and `Fit to screen` uses the
  custom `data-tip` pseudo-element pattern, not native `title`, so the
  help text is consistently visible in-browser.
- **Settings overlay** (`#settings-btn` gear, `openSettings`/`closeSettings`):
  78vw × 76vh over a blurred backdrop. **Left sidebar since v1.28.6** (modelled
  on Claude desktop; replaced the v1.19 top tabs): `aside.settings-side` holds
  the search and the grouped section list (`SETTINGS_GROUP_ORDER` = Settings /
  Data & models / Help, each row an inline-SVG outline icon from
  `SETTINGS_ICONS` + label); the selected row is a neutral fill with `--text`,
  not the accent pill. The sidebar is `--surface-sunk` with no border-right —
  the surface step is the divider. The close button floats top-right of the
  pane (`.settings-modal > .settings-close`); there is no header bar. A search
  that matches settings inside a section shows a count on its row
  (`.settings-nav-count`); Up/Down walk the rows. The list scrolls inside the
  sidebar, so nothing becomes unreachable on a short window.
  Its radii are the softer `--r-xl` / `--r-soft` tokens. Follows the
  `openMptOverlay` idiom (the `document.body.style.overflow` lock +
  `dataset.*PrevOverflow` restore) and registers in the global Esc handler.
  See §16 for why Logs exists. Rebuilt in v1.10.1 — five load-bearing rules:
  1. **`SETTINGS_SECTIONS` is the only place a section is declared**
     (`{id, group, label, description, keywords, items, render}`; give a new
     section an icon in `SETTINGS_ICONS`, or it renders with a blank slot).
     `renderSettingsNav()` rebuilds the nav from it. Add a section there, never
     back in `index.html`. Sections today: General, Column Presets, My Investments, Attention, Models & Data, API keys, Logs, Guide, About.
     **Guide** (2026-10-13) is the old topbar "i" overlay, merged into Settings at
     the owner's request: its markup is `<template id="guide-tpl">` in
     `index.html`, `renderSettingsGuide` clones it and runs KaTeX, and
     `guideItems()` makes one search item per card (searching "RS Rank" lands on
     that card). Its cards are hairline rows there (`.settings-guide`), not
     cards in the modal. The MPT info modal still uses the shared `.info-*` CSS.
     **My Investments → Clear my investments** asks once inline ("Clear N
     entries" / Cancel, no native dialog), then switches to the page with an
     Undo toast; the server keeps a dated copy (backend.md).
  2. **The search `<input>` stays static in `index.html`.** Only
     `#settings-nav-list` is re-rendered; an input inside that subtree would be
     destroyed mid-keystroke, blurring the field. For the same reason typing
     re-renders the **nav only** — re-rendering the pane per keystroke would
     tear down `#log-body` and restart the log poller ~10×/second.
  3. **`renderSettingsPane()` calls `stopLogPolling()` unconditionally as its
     first statement**; only `renderSettingsLogs` restarts it. A new section can
     therefore never leak a 1.5s timer against `/api/logs`.
  4. **Nav clicks are delegated** on `#settings-nav-list` — per-node handlers
     would be bound to detached elements after the first search.
  5. **Esc**: the search field clears a non-empty query and stops there; on an
     empty query it must **bubble** to the global handler (the Escape handler in `app.js`)
     which closes seven overlays in one pass. Never `preventDefault` it.
  Mirrored controls (theme, Fit to screen) follow a strict
  **single-mutator / single-painter** rule: `setTheme()` and
  `toggleFitColumns()` remain the only mutators; `syncThemeControls()` /
  `syncFitControls()` paint *both* the topbar and the settings widgets. The
  settings controls must never write `STATE.fitColumns` or the theme directly.
  **Gotcha:** the theme segmented control uses `data-theme-opt`, NOT
  `data-theme` — `style.css` themes via unscoped `[data-theme="dark"]` /
  `[data-theme="bloomberg"]` attribute selectors, so a button carrying
  `data-theme="bloomberg"` silently adopts the whole Bloomberg palette while
  sitting inside a dark-theme page. Verified in all three themes.
- **Topbar single-class overrides lose the cascade.** `.topbar button` is
  (0,1,1) and sets `font-size: 12.5px`; a bare `.gear-btn` / `.info-btn` rule is
  (0,1,0) and is silently ignored. The gear's declared `font-size: 15px` never
  applied for that reason and it rendered at 12.5px until v1.10.1, which added
  `.topbar button.gear-btn { font-size: 20px }`. Measure computed styles in the
  browser rather than trusting a declaration.
  **This has now bitten twice — assume it, don't rediscover it.** The refresh
  status chip's cancel button was the second case (fixed v1.11.1): `.rf-chip-x`
  (0,1,0) lost *every* declaration to `.topbar button`, including its own
  `border: none; background: transparent`, so it rendered as a bordered 30px
  `--r-md` square with the canvas background inside a 999px pill. It is now
  `.topbar .rf-chip-x` (0,2,0), draws an **inline SVG** mark rather than a `×`
  glyph (whose size and baseline vary by system font), and hovers to
  `rgba(var(--neg-rgb), 0.16)` instead of a solid red fill. Any new control
  placed inside the topbar needs the `.topbar` prefix on its rules.
  A cheap way to iterate on one of these without touching the app: build a
  standalone lab page that inlines the real `:root`/`[data-theme]` variable
  blocks plus the competing `.topbar button` rule, render the candidates side by
  side in all three themes at 1× and 3×, and open it in the browser pane. Write
  the losing/shipping variant at its **real** specificity or the lab will
  "fix" the bug for you and prove nothing.
- **Terminology (v1.12): the engines are the "News read" and the "Market
  read"** in all UI copy. Engine names (LLM, statistical model) appear only in
  the Methodology and Settings → Models & Data. The middle News tier is
  labelled "Mixed" (`neutral` in data), the middle Market tier "No edge"
  (`no_edge`). The shared vocabulary (`NS_COLORS` from the theme's `--ns-*`
  variables, `NS_LABELS`, `NS_LENSES`, `nsTierDot`, `nsPill`, `nsScale`) sits
  in one block near the top of `app.js`; the `--ns-*` colours are defined per
  theme in `style.css` and must stay in all three.
- **NS column + hover card.** `nsDot(s, sym)` renders two dots, **Market read
  first, then News read**; faded = stale, hollow = no read. There is no
  per-dot tooltip: one delegated handler opens a single `.ns-hc-tip` card
  (`nsHoverCardHtml`, positioned by `placeTip`) with a lens row per lens (score
  cells + the fact, "No news" when empty), two-pass agreement, the Market
  tiles (expected move, percentile + what it is ranked against, tier), the
  divergence sentence, the stale notice and both ages. Escape and scroll hide it.
- **Timeline and Flash Tape show only headlines the News read read and found
  to be about a holding** (`nsReadAbout`). Unread headlines (outside the 15 per
  ticker) and "not about it" ones are half the feed on a mega-cap; the tape's
  "Not about it" chip still shows the latter. One dot per headline (the News
  score); tier chips filter on it, lens chips replace the old event chips.
- **Flash Tape full-screen** (`openTapeFullscreen`, 99vw × 99vh): clicking the
  `#ns-tape-card` body opens it; `e.target.closest("a, select, button, input,
  label")` guards the links and filter controls. `renderNsTape({fullscreen})`
  is ONE function serving both surfaces — the inline panel keeps its 120-row cap
  and single-line ellipsised headlines, the overlay lifts the cap to 1000 and
  adds a numeric score column plus a 2-line-clamped summary. Filter changes in
  either view re-render the other so the two never diverge.
- **Track record and Methodology.** "Model Diagnostics" is now the Track
  record (`toggleTrackRecord` / `renderTrackRecord`): two verdict cards, a
  long-short chart (`svgLine`), per-lens hit rates, consistency, and a
  collapsed "For quants" block — all from `/api/news-diagnostics`. The
  Methodology modal (`openMethodology`) is two side-by-side explainers with
  frozen validation numbers and KaTeX formulas in collapsed `<details>`; live
  numbers belong in the Track record, not there.

- **Windows have no outer border (v1.28.6, the user's "Frame B").** Every
  overlay surface is listed ONCE in the grouped rule at the end of `style.css`:
  `background: --surface-raised` (one step lighter than the page in dark and
  Bloomberg), `border: 0`, `box-shadow: --shadow-window`, and `--bg-canvas`
  re-pointed at `--surface-raised` so sticky headers and inputs that paint
  "canvas" don't read as darker strips. It sits last in the file on purpose:
  several per-window rules higher up have the same specificity. **A new overlay
  surface goes in that list.** Inner hairlines (header/footer dividers) stay.
  Small dropdowns (`.inv-menu`, `.ct-menu`, `.bench-menu`) keep their hairline —
  they open over the undimmed page, where a shadow alone doesn't hold the edge;
  the window-sized Constraints popover (`.inv-menu.tr-pop`) is in the list.
  `--surface-raised` / `--surface-sunk` / `--shadow-window` are literal per
  theme (never `var(--bg-canvas)` — that would be a cycle inside a window).
- **Weights popup (v1.28.6, "V1 · plain number").** Slim slider with the accent
  fill driven by a `--p` var (`syncWeightsInputs`), the weight printed as text
  that only becomes a field on hover/focus (`type="text" inputmode="decimal"`,
  `parseWeightPct` accepts `29,66`), inline-SVG ghost locks, no per-company
  colours (tried and rejected). Behaviour is the original auto-rebalance, with
  one fix: a row is capped at `1 − lockedTotal`, so it can no longer push the
  total past 100%. Click-focus selects on the field's own **mouseup** — the
  browser places the caret on mouseup, after focus and after any rAF, so a
  focus-time `select()` was undone and "20" typed over 9.28 became "209.28".
- **Overlays: `showOverlay()` / `hideOverlay()` are the ONLY way to open and
  close one** (v1.11.0). They own `lockBodyScroll`/`unlockBodyScroll`, a single
  **nesting counter** on `document.body`'s overflow. Never write
  `body.style.overflow`, and never toggle the `show` class by hand — both are
  enforced by `tests/test_frontend_overlays.py`, which also asserts the overlay
  inventory in that test matches every `id="*-bg"` in `index.html`, so a new
  backdrop can't skip the contract.
  Why a counter: overlays nest (About-MPT over MPT, the inline prompt over the
  weights popup) and the global Escape handler closes
  seven of them in one **unconditional** pass. The old design had three
  overlays each stashing `dataset.<name>PrevOverflow` and the other nine
  locking nothing at all — so the detail modal scroll-chained to the page, and
  a nested pair could restore each other's value. Both helpers no-op when the
  overlay is already in the requested state, which is what makes the
  seven-closer pass safe.
  Second half of the fix is CSS: the grouped `overscroll-behavior: contain`
  rule in `style.css` (just above `/* ===== Detail modal ===== */`). **Every
  new overlay scroll container goes in that list.** Both halves are needed —
  the body lock alone still lets the trackpad's elastic bounce chain out, and
  containment alone does nothing for a gesture starting on the backdrop.
  Scroll *position* needs no saving: `overflow:hidden` on `<body>` freezes the
  viewport where it is (it propagates because `html` is `overflow: visible`).
  The `padding-right` compensation replaces the scrollbar's width, without
  which the page visibly jumps ~15px wider the instant anything opens.

- **Detail-modal header shows the ACTIVE RANGE's return**, not `pct_1d`
  (v1.11.0). `modalPriceBlockHtml()` / `renderModalPriceBlock()` own
  `#m-price-block`; a drag in progress (the brush's `onUpdate(sel)`) wins over
  the range tab, and the `.p-range` badge names whichever period is being reported. It
  also reads price from `DETAIL.data` once the detail payload lands — the
  skeleton is never re-rendered after the fetch resolves, so the old code kept
  the table row's cached price for the modal's whole lifetime. It computes from
  **`activeChartSeries().full`**, not `d.history`: on an intraday range those
  two disagree about where the window starts, and a header contradicting the
  summary line right beneath it is worse than no header.

- **Chart granularity ladder** (v1.11.0). Everything was hardcoded
  `interval="1d"`, so 1M was ~21 points on an 800px-wide chart.

  | Range | Source | Fetch | Displayed |
  |---|---|---|---|
  | 1M | `GET /api/history` | `30m` / `60d` | last 1M (~280 of 780 bars) |
  | 3M | `GET /api/history` | `1h` / `1y` | last 3M (~435 of 1749) |
  | 6M | *same cached payload as 3M* | — | last 6M (~870) |
  | YTD · 1Y · 5Y · MAX | the daily `period="max"` payload from `/api/detail` | — | client slice, thinned to ≤1500 |

  **The fetch window is deliberately far wider than the display window, and
  that is load-bearing, not waste** — an SMA 200 over 30m bars needs 200 bars of
  warm-up, so computing on exactly the visible window leaves the line empty on
  every short range. It also means a symbol costs at most **two** intraday
  fetches no matter how the user tabs around. `fetcher._RANGE_INTRADAY` is the
  table; `fetcher._INTERVAL_MAX_DAYS` holds Yahoo's own caps (`1m ≤ 7d`,
  `2–90m ≤ 60d`, `1h ≤ 730d`). Exceeding a cap returns an **empty frame, not an
  error**, which is why `_period_days()` returns 10 000 for anything it can't
  parse — guessing small would let an over-cap request through and blank the
  chart instead of falling back. `range_history()` returns `fallback: true` for
  the daily ranges, for a cap violation, and for listings with no intraday data;
  the client then just draws the daily series and the legend says "daily".
  Benchmarks are fetched at the **same interval** (the client passes `bench=`,
  since it already knows the sector ETF) or the overlay renders as a staircase
  against a smooth line. Modal open stays instant: intraday is fetched lazily
  only when 1M/3M/6M is picked, with a loading chip on the range tab.

- **Both charts share their interaction code**: `attachChartHover()`
  (crosshair + tooltip) and `attachRangeBrush()` (drag-to-measure), each fed
  `lines: [{label, pts, dot}]` with `lines[0]` the main series. The measure is
  **TradingView-style and transient** (the user's call): while the button
  is held a band and a floating `.chart-measure` badge show every line's return
  over the span; both vanish on release. There is deliberately no persisted
  selection state, no Escape handler and nothing to `destroy()` — every
  listener sits on the chart's own overlay element, rebuilt each render. (The
  earlier persistent design needed an AbortController, a capture-phase Escape
  handler with a `SCROLL_LOCK` guard, and teardown in `closeModal()`; all of
  that went with it.) The `.sel`/`.pf-sel` rect carries its own `y`/`height` in
  the markup. `setPointerCapture` stays in try/catch — it throws for a dead
  pointer id.

- **Benchmark picker.** "vs ▾" in the Risk & Return header is a small custom
  dropdown (`benchSelectHtml` / `wireBenchSelect`: Arrow keys, Esc, outside
  click). `STATE.bench` (localStorage `pf_bench`) drives the "/ x" column and
  Beta/R²/TE (the portfolio's `rel` against it); `activeBench(a)` falls back to
  SPY. The chart's purple line stays the S&P 500 on purpose (`BENCH_LINES`). The Nasdaq / Sector-mix
  pills add extra lines (`pfBenchLines`, deduped), and their period returns
  are listed *under* Risk & Return, not in the chart legend. The first overlay
  pill (`#pf-show-bench`) is relabelled with the chosen benchmark on render.

- **`thinPoints(pts, maxN)` must pin BOTH endpoints.** A 45-year MAX window is
  ~11.5k daily closes. Last-in-bucket downsampling naturally starts at index
  `ceil(step)-1`, which silently moved AAPL's MAX start date forward ~7 trading
  days and changed the reported return from +339417% to +316048%. The window the
  header and chart report must be the window the user asked for.

- **SMAs (20/50/200)** are off by default and share **one object**, `CHART_SMA`
  — `DETAIL.sma` and `STATE.pfSma` are both that same reference, persisted to
  `localStorage.chart_sma`. A shared *key* was not enough: two independent
  copies read at different times silently clobbered each other (enable SMA 200
  in the modal, then click a portfolio pill, and the pill's stale page-load
  copy was written back over it). Toggling from the modal calls
  `syncSmaPills()` so the pills' `data-on` repaints too.
  `smaSeries()` takes the **full** series and the caller slices the result —
  never the other way round. Legends name the bar frequency ("SMA 50 · 30m
  bars"), since the same period means something very different on 30m vs daily
  bars. The portfolio chart's SMAs are computed **server-side** (`series.sma`,
  see §4 "One wide fetch") since the client only ever has the period slice.
  At the true start of a series `smaSeries` averages what exists so far.
  `SMA_COLORS` in `app.js` and `.swatch.sma*` in `style.css` must stay in sync.
- **Chart types, TradingView-style (v1.19).** `CHART_TYPE` (localStorage
  `chart_type`, default **HLC area** — the user's pick) is shared by the stock
  modal and the portfolio chart, chosen from a pill + rounded menu
  (`chartTypeSelectHtml` / `wireChartTypeDd`; Arrow keys, Esc closes the menu
  before the modal, Tab out closes it; a native `<select>` popup can't be themed): HLC area, Candles, Hollow candles, OHLC bars, Area,
  Line. Drawing goes through `buildBars` (joins `history` + `ohlc` + `volume`
  on timestamp) → `aggregateBars` (first open / max high / min low / last close
  / summed volume — candles are bucketed to ~5px, so 5Y goes weekly like
  TradingView) → `priceLayerSvg` / `volumePaneSvg`. **The bars are paint only:**
  hover dots, drag-measure, the header and every return still run on the exact
  close series, so switching type can never change a number. Volume is its own
  pane, each bar coloured by close ≥ open. Intraday ranges use an **ordinal**
  x-axis (one slot per bar, nights and weekends skipped); it hands hover/brush
  an inverse `geom.xInv`, which both helpers prefer over the linear time
  inverse; overlays (S&P, sector ETF) are first put on the stock's own bar
  times (`alignOnto`), or a non-US listing's SPY bars stack on its last slot of
  each day. Hover reads the bar CONTAINING the time (`barAtTime`, a lower-bound
  search on bucket ends), and a bucketed bar shows its date span. The chart SVG
  carries literal theme colours, so `setTheme` repaints both charts. Saved
  analytics without `series.ohlc` (pre-v1.19) are backfilled once by
  `requestAnalytics`. The portfolio's bars are approximate (backend.md "Bars
  for the chart types"); the high line's colour is `--hlc-hi`, defined in all
  three themes.
- **Fit to screen toggle** (`#cv-fit-toggle`): optional table compaction
  mode for dense presets. `applyTableFitMode()` computes a scale from the
  active columns' declared widths versus `.table-wrap` width and applies
  it through the `--table-scale` CSS variable. The active view should
  remain readable, but the explicit goal is "keep the current preset on
  screen before falling back to horizontal overflow."
  **A hidden table is never measured** (owner, 2026-10-11: "the table gets
  really small"): a `render()` while My Investments shows (a resize, a
  refresh) read a 0px wrap, floored the width to 320px and the scale to 0.4.
  `applyTableFitMode` now keeps the last good scale when the wrap has no
  content width (`tableContentWidth`; `clientWidth` alone still counts the
  padding) and sets `_fitDeferred`, and `showPage()` renders again when the
  table shows.
- **Default currency (owner, 2026-10-11).** Settings → General picks it
  (`benchMenuHtml` over `FX_SUPPORTED`), saved as `default_ccy` in
  `prefs.json`. The boot waits for `/api/prefs` (`BOOT_PREFS`, at most
  1.5 s) before `fxInit`, so the first load is already in that currency.
  The topbar switch is kept in **sessionStorage** (`fx_quote`): it lasts
  until the app or tab closes, and a new one opens in the default.

- **The optimizer is one toolbar and a Constraints sheet** (owner's pick
  "A · Inspector", 2026-10-19). The toolbar holds lookback, CVaR level and
  covariance as segmented pills and analyst trust / compute budget as chips;
  every control explains itself on hover (`data-tip`; the budget's table is in the Guide),
  never an (i) beside it. The **Constraints** chip (`#pf-mpt-cons-sum`,
  `mptConsSummary`) opens `#pf-mpt-cons-bg`, a sheet over the workspace (z 95,
  like the guide; Esc closes it first) holding cash vs fully invested, the
  shared rebalancing/cost controls (`tradingControlsRender`, the same as the
  chart's panel), the risk-free rate and the per-position limits grid. The ids
  inside are the ones `mptGetParams` reads. Because the sheet floats, the chart
  never changes size when it opens: the old in-flow limits panel, its
  push-down pinning (`mptPinBodyHeight`, `--mpt-body-h`) and the height lock
  are gone. The side panel leads with four tiles (return net of costs, 30-day
  CVaR, max drawdown, trading cost a year) and keeps its rich tips without the
  dotted underline (`.pf-mpt-bg [data-rich-tip]`); weight bars are pills.
  The "Return, net" tile shows `ret_net ?? ret` (this portfolio's own
  backtested cost); the chart, slider readout and best-point pick keep `ret`,
  the figure the frontier was ranked on.
- **Nothing re-runs by itself; a changed input marks the run stale** (owner,
  2026-10-19; the CVaR pills' old auto-rerun is gone). `mptParamsDiffer()`
  compares `mptGetParams()` with `MPT.result.params` at each control's
  precision (rf 0.01%, trust/CVaR whole %, so "0.90" vs 0.9 is equal) and
  `mptStaleUpdate()` dims the chart and panel (`.pf-mpt-bg.is-stale`, still
  interactive), puts a dot and "Run to update" on Run and writes what differs
  under the tiles. It runs from delegated click/input/change listeners on the
  optimizer and its sheet, from `mptRenderSide()`, and from `mptConsSummary()`,
  which `tradingSet` calls on every change of the shared rebalancing/cost.
  **Loading a saved run never changes the shared trading setting**: a run made
  under another one shows "This run used Weekly · 25 bps … Use these", and only
  that click calls `tradingSet`. Segmented pills are set through `mptSegSet`
  (numeric compare, radiogroup + roving ←/→).
- **A run in flight keeps the previous result.** `mptStreamFrontier` replaces
  `MPT.result` mid-stream, so `mptRun` keeps `prev` and puts it back (chart,
  slider, panel) on Cancel or an error; meanwhile the side panel is
  `.is-busy` (dimmed, inert) and Use/Save are disabled, as they are before any
  run. Run shows the stream's progress ("Running 42%"); ⌘/Ctrl+Enter runs.
  Opening the optimizer on another portfolio forgets the shown run
  (`mptResetResult`). The Constraints sheet takes focus on open, traps Tab and
  returns focus to its chip; Esc in the open Budget menu stops there.
- **Light-theme navy and one pill look.** `--mpt-navy` (#002B51 on `:root`,
  the accent in dark/Bloomberg) colours the title, Run and the active pills.
  The toolbar trays, the sheet's trays and `.tr-seg` share one rule (search
  "ONE segmented-pill look" in style.css); it lists `.tr-pop .tr-seg button`
  because `.inv-menu button { font: inherit }` comes later. Below 980px the
  body is one column with an explicit chart row (`minmax(360px, 60vh)`): a
  content-height row left the flex:1/min-height:0 chart at 0px.
- **The canvases CROP, they never rescale** — the second line of defence, and
  still load-bearing. They used to be `inset:0; width:100%; height:100%`, but
  their backing stores are only resized inside `mptSizeCanvases()`. The limits
  panel used to be a flow sibling in the `flex-column` modal, stole ~450px from
  `.pf-mpt-chart`, and nothing re-measured — so the browser rescaled a stale
  bitmap and the entire plot visibly squashed; `MPT._proj` also kept the old
  `cssH`, so hover/click hit-testing silently drifted off the frontier.
  The contract: `mptSizeCanvases` publishes the height it actually drew at
  to `--mpt-chart-h`, the canvases read **that** rather than `100%`, and
  `.pf-mpt-chart` is `min-height:0; overflow:hidden`. A shorter parent therefore
  clips the canvas instead of stretching it, and it returns intact.
  **`mptSettleChartHeight()` must be called after anything that paints the side
  panel** — `mptRenderSide()` writes the legend *below* the chart, shrinking the
  host ~22px after `mptRenderChart()` already locked it, which left the x-axis
  label (drawn at `cssH − 6`) clipped. It is called from `mptRender()` **and**
  from the streaming `done` handler, which finalises via
  `mptRenderChart({fixedProj})` and so never reaches `mptRender()` — that is the
  path every completed run takes, so missing it there fixes nothing.
  **Do not "improve" this with a ResizeObserver.** RO delivery is tied to the
  frame lifecycle and is throttled or dropped outright in a backgrounded window
  — measured here as *zero* callbacks for a real 1185→735px change — so the
  correction would fail exactly when the user tabs away and back. The settle
  pass is synchronous and deterministic instead.

- **`textOnHeat` measures contrast; it does not guess a threshold**.
  It used to flip to white above a fixed `|t|` (0.55 light / 0.65 dark). That was
  wrong: on the light theme's green ramp `--text` beats white at *every*
  saturation (4.14:1 vs 3.82:1 even at full tint), so the rule went white
  precisely where dark text was still winning 6-8:1 and a mid-range cell (a +20%
  upside) rendered white-on-light-green at **2.2:1**. It now reproduces the
  background `colorDiverging` will paint and keeps whichever of `--text` / white
  / near-black actually measures best (`relLuminance` + `contrastRatio`, ~10
  float ops per cell). Near-black is a candidate because a saturated tint on
  dark/bloomberg is a *bright* green/red where both white and the near-white
  `--text` fail — the same problem `--on-accent` solves with `#050505` (§8).
  Two consequences: **`THEME_COLORS` now carries a `text` triple per theme and
  it must stay in sync with `--text` in style.css**, and the `0.9` mix factor is
  duplicated from `colorDiverging` — change one, change both. Measured floor
  across the analyst table went 2.2 → 5.28 (light) / 4.7 (dark) / 4.04 (bbg).

- **Per-position limits grid is a spreadsheet, not a form.** `.pf-mpt-bnd`
  inputs are borderless/transparent with the spinners suppressed; `:focus` draws
  an inset accent outline (Excel active-cell) and `.pf-mpt-bnd-row:focus-within`
  tints the row — `:focus-within` so keyboard tabbing highlights without any JS.
  `:not(:placeholder-shown)` colours a *set* constraint in `--accent`, which is
  why the `0` / `100` placeholders are load-bearing, not decoration. Rows carry
  `logoImg(sym)` + the company name pulled from `DATA`. Values and their column
  headers are both centred, and `align-self:stretch` makes the input fill the
  row's **full** height (the row is `align-items:center`, which otherwise leaves a
  dead strip above and below) — verified by hit-testing a 96×23 cell on a 6×4 grid,
  96/96 points resolve to the input, so a click anywhere in the column lands in the
  number.
  Focusing a cell **selects its value** (`focusin` on the grid + a rAF-deferred
  `select()`), so typing over `25` yields `4`, not `254`. Three non-obvious bits:
  `focusin` rather than `click` covers keyboard Tab and does not re-fire inside an
  already-focused cell (which would wipe a deliberate caret placement mid-edit);
  the rAF is required because the browser sets the caret from the click position
  *after* focus and would undo a synchronous `select()`; and on `type="number"`
  **`selectionStart` reads `null`** — that is an API limitation, not a failure, so
  assert the behaviour by typing over the value, not by reading the selection.
  It lives in the Constraints sheet; `.pf-mpt-chartwrap` / `.pf-mpt-side` are
  the workspace itself and stay boxless (the side panel keeps a single hairline
  `border-left` as a gutter rule).
  **Do NOT wire `mptWireAssetTips` to this grid, and do not give the rows
  `data-mpt-sym`.** That tooltip fires on `mousemove` and each event rebuilds the
  tip's `innerHTML` and calls `placeTip()` (a forced synchronous layout), i.e. a
  parse + reflow per frame while the pointer rests over the list. The stats it
  showed belong to the chart anyway.
  **The grid is not a scroller.** It used to have its own `max-height` *and*
  `overscroll-behavior: contain`, which is what made scrolling feel broken: a
  short inner scroller hits its end after ~100px and then **stops dead** instead
  of chaining to the parent, so getting down a 20-name book took a stack of
  separate gestures. One surface (`.pf-mpt-cons-body`), one gesture.

- **Analyst consensus table** (`renderAnalystDashboard`): Weight uses the blue
  quintile ramp, and Upside **and Upside (median)** share the diverging ramp
  anchored at 30 — all via `cellStyleHeat` with a synthetic column literal, so
  they read identically to the main grid's heat columns. The two upside columns
  must keep the **same ramp and the same anchor**: they are the same quantity on
  the same scale, and reading them as a pair is the point of the median column —
  a visibly weaker median tint means the mean is being dragged up by one high
  outlier. `setTheme()` re-renders this table, because
  `render()` only rebuilds the main grid and the tints are baked into inline
  styles at build time. **There is no true q25/q75 of analyst targets** — Yahoo
  publishes only low/mean/median/high and no per-analyst data exists in the feed
  — so nothing quartile-shaped is fabricated. The median and the low/high band
  are reported **as upside, not as target price** (`Upside (median)` and
  `Upside range`, both immediately right of `Upside`): upside is the decision
  variable — a target of 768 means nothing until you know the price it is
  measured against — and three upside figures side by side make the median-vs-mean
  skew and the width of the band readable in one scan. Raw target prices stay in
  the hover. `Upside range` sorts on `(high−low)/mean`, the same dispersion
  `frontier.py` uses for BL view confidence. `quickAnalystPreview()` nulls the
  trio (the streaming row payload has only the mean) so the optimistic paint
  shows `—` and fills in. **Do not add them to `fetch_one`** — that is the hot
  loop; they come from `analytics._analyst_for`.
- **`_analyst_for`'s target trio needs its retry** (`analytics.py`). It runs on 8
  pool threads, and Yahoo answers a burst of `.info` calls by handing some of them
  an **empty dict** rather than an error — a throttle, not "this name publishes no
  range". Verified directly: names that came back thin returned a full
  low/median/high on a sequential call moments later. Without the retry the
  biggest holdings rendered `—` in exactly the two columns above, and the 300 s
  negative cache meant the next refresh usually failed the same way. Two jittered
  escalating retries, **only** on the all-None path: one is not enough, because
  the retries themselves collide. Live coverage went 10/15 → 15/15.

## 8. CSS / theming patterns

- CSS variables (`--accent`, `--bg-canvas`, `--text`, `--muted`,
  `--border`, `--pos`, `--neg`) defined in `:root` and overridden under
  `[data-theme="dark"]`.
- **Palette "Navy & Denim" (v1.19)**, chosen by the user from five options
  inspired by Morgan Stanley / Goldman Sachs: accent `#187aba` (MS blue) on
  deep-navy ink `#0b2239`, blue-tinted neutrals instead of GitHub grays, hover
  `#e8f2fa` (was a yellow). Dark mode (since v1.19.2 "Ink", below) uses accent
  `#4ba3e3` and a **dark** `--on-accent` (`#04121f`) because white on that blue
  is only ~2.8:1. `THEME_COLORS` in app.js mirrors these values — change both.
  Use `rgba(var(--accent-rgb), a)`, never a literal blue.
- **One period selector (2026-10-11, the owner's pick).** Every chart period
  control is the same pill: a tinted track with the chosen period as a raised
  pill inside, the shape of the Value / Return % switch (`.pf-contrib-toggle`).
  One rule in style.css styles `.pf-period-tabs` (Portfolios chart, My
  Investments), `.m-range-tabs` (detail modal), `.ns-tl-periods` (News
  timeline) and `.ns-lb-periods` (News window). A new period control joins
  that rule, never gets its own look. Non-period switches (Optimize lookback,
  Track record source) keep their own styles.
- **Chrome vs data colours (v1.19.2, the user's call).** Navy & Denim is for
  the chrome only (buttons, tabs, accents, kickers). **Data colours stay
  classic**: `--pos`/`--neg` are the pre-1.19 green/red (`#1f883d`/`#cf222e`
  light, `#3fb950`/`#f85149` dark) and the heat `blue` in `THEME_COLORS` is
  `#2563eb`/`#60a5fa`, not the accent — the user reads tables by colour pattern.
  Dark mode is **"Ink"** (`#0a0e14`, surfaces `#111823`, borders `#1d2938`):
  near-black with navy-tinted, not gray, surfaces; the 1.19 navy `#08182a` was
  too blue. The desktop splash (`desktop.py` `_BG`/`_ACCENT`) mirrors it.
- **The shared tooltip is a card** (`.app-tip`, owner 2026-10-20): `--tip-bg`
  / `--tip-border` / `--tip-shadow` per theme, a 10 px radius (rounder than the
  `--r-*` scale on purpose: it floats), 13 px system font. A `data-tip-tex`
  attribute beside `data-tip` leads the tip with a KaTeX-typeset formula
  (`.app-tip-h`); without KaTeX the TeX source shows as text.
- **Selected text has its own tokens** (`--sel-bg` / `--sel-fg` per theme,
  one `::selection` rule): without them the desktop shell's palette showed
  light-mode selections as near-black text on dark gray (owner, 2026-10-09).
- **Corner-radius scale** (`--r-lg` / `--r-md` / `--r-sm` / `--r-xs` in the base
  `:root`, currently the "Sharp" 6/4/2/1 px tier). Every non-circular
  `border-radius` reads a token, so app-wide roundness tunes from these four
  values alone; pills/circles (`999px` / `50%`) stay literal on purpose.
- **Three themes: `light`, `dark`, `bloomberg`** (a Bloomberg-terminal
  palette added in v1.5.3). Because the whole app is variable-driven, a
  theme is a `[data-theme="…"]` block plus a matching `THEME_COLORS.<name>`
  RGB-triplet entry in `app.js` (the latter feeds the JS-computed
  heatmap/spark/RS-bar/delta-bar colours). To add a fourth theme, copy those
  two blocks. **Bloomberg's colour hierarchy is the point — don't flatten
  it**: `--text` is WHITE (data values), `--muted` is AMBER (labels/headers/
  secondary), `--hover` is the terminal's dark selection blue, borders are
  neutral gray. The first cut made body text amber too and the user rejected
  it ("everything is the same color"). A short fidelity-override block right
  under the variable block additionally paints table `th`, ticker `.sym`
  cells amber ("amber = editable" is the terminal's own convention). `--on-accent` is
  the text/thumb colour placed *on* an `--accent` fill (white in light/dark,
  near-black in Bloomberg so text stays legible on the bright orange); any
  new accent-filled control must use `color: var(--on-accent)`, never a
  hardcoded `#fff`. Two JS branches that ask "is this a dark canvas?" use the
  `isDarkTheme(t)` helper (true for `dark` **and** `bloomberg`) rather than
  `=== "dark"`.
- **Theme switch interaction** (`setupThemeSwitch` in `app.js`): a short
  click toggles light↔dark (Bloomberg counts as non-light, so a click exits
  it to light); a **long-press (≥500ms)** on the switch activates the hidden
  Bloomberg theme (pointer events cover mouse+touch; the terminating click is
  swallowed via a `longFired` flag). Choice persists in `localStorage.theme`
  and is restored by `readTheme()` (which accepts all of `THEME_NAMES`).
- Tooltips use TWO patterns:
  - **Pseudo-element `::after`** on `[data-tip]` — fast, declarative.
    Used for header tooltips and overlay-pill info icons.
  - **JS-rendered `.pf-metric-tip` div** — heavier but supports rich
    content (LaTeX formulas, etc.). Used for analytics metric labels.
- Hover-tooltip cursor is `cursor: default` — explicitly NOT `cursor:
  help`. The user dislikes the question-mark cursor.

## 11. Finding things

There is no line-number table on purpose: it went stale within a release. Search
for the symbol instead:

```bash
grep -n "<symbol>" src/convexity/*.py src/convexity/static/*.{js,html,css}
```

Anchors worth knowing (all greppable): `COLS` / `BUILTIN_VIEWS` (column registry,
top of `app.js`); `renderTabs`, `beginTabRename`, `tabMenuOpen` (tabs); `requestAnalytics`,
`renderModeBar` (analytics); `exportXlsx`; `lcHtml` / `lcShow` / `lcHide` (loading
chip, CSS `.lc-*`); `showOverlay` / `hideOverlay` and the global Escape handler;
`Handler` and `_pick_port` in `server.py`; `fetch_one`, `fetch_detail` in
`fetcher.py`; `resolve_symbol` in `resolver.py`; `analyze_portfolios_multi` in
`analytics.py`; `compute_efficient_frontier_stream` in `frontier.py`; `fx_rates`,
`fx_index_history` in `fx.py`; `save_view`, `save_mpt_run` in `persistence.py`.
