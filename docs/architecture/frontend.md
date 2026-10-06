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
  (main table, column views, analyst section, Optimize, Refresh). The R
  shortcut is off there too. My Investments reopens on launch if it was last.
- The page renders `GET /api/investments` into `INV.book` with its own
  table (`#inv-tbl`, not `#tbl`/`COLS`: positions have a cash row and, later,
  closed lots). Amounts arrive in `book.ccy` and convert to `FX_QUOTE`.
  `fxSelect` and the startup rate load re-render it.
- The chart's period base indexes come from the server
  (`series.period_start`), the same base as the headline returns, so the two
  can't disagree. It is drawn at its pixel width (no stretched text). A
  Value / Return % switch redraws only the chart; the toolbar and reading are
  fixed-height single lines, so the card never resizes.
- **Owner's rules for this page:** one number per headline cell, with details
  in `RICH_TIPS` hover cards (`inv:*`: plain words, technical name small);
  no dotted underlines or info icons; company names, not tickers; "this
  month", not daily, in the headline (the chart itself is daily).
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
- **The front door (Phases 5–6, owner's W1).** The empty page *is* the
  composer (`invWelcomeHtml` → `invComposerHtml`): one box for typed or
  pasted text, pasted images, and dropped or attached files (screenshots,
  PDF, CSV, Excel), with "add one holding by hand" under it. "+ Add" on a
  full page opens the same composer in `#inv-pop`, "By hand" one click away.
  State is `INV.imp` (`text, files, busy, error, prop, answers`); "Read it"
  posts base64 files to `/api/investments/import`.
- **The review** (`invReviewHtml`) takes the page until Apply or Cancel:
  summary, screenshot thumbnails, at most three one-click questions, and an
  editable table. Purple (`--ai`) marks what the AI guessed, amber what it
  needs from the owner; "From" names the source row. The read and every
  edit run `/import/check` (debounced, newest answer wins) so a sale the
  book can't cover is flagged before Apply, with "Add the N shares as held
  before" or "Leave it out"; Apply waits ("Checking…") while a check is in
  flight. `invRowEntry` is the only place rows become entries: the dry run
  and Apply both send what it builds. Apply sends the rows and the names the
  owner corrected (`aliases`) in one batch.
- **Adding by hand and editing (owner's E2).** `invFormHtml` is a card: the
  company as a header once chosen (initial, name, ticker, today's close via
  `invNowFill`), big number fields with the unit in the label, date chips
  (Today, 1 year ago, the picker as the real `date` field) and a live result
  line (`invResultText`, updated in place on input so typing never loses
  focus). The same card edits an entry in Activity. Other types sit in the
  quiet "Something else" menu.

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
`COLS` (≈line 3714) is the single source of truth for every available
column — `key`, `label`, `w`, `align`, `sortable`, `render(r)`, optional
`heat`, `bg`, `sortValue`, `td_cls`. A `heat.kind === "yo_dyn"` column exposes
a per-view background mode in Customize: **Off / 2C-Quantile / Quantile /
Min-Max** (`getHeatMode`/`cellStyleHeat`). "Quantile" = single-blue quintile
buckets, "2C-Quantile" = orange↔blue diverging quintiles, "Min-Max" =
continuous 10th/90th-clipped blue. `favor:"low"` flips which end is best (e.g.
`analyst_rating`: 1 = Strong Buy reads blue). The old `"percentile"` mode +
`kind:"yo"` fixed-orange ramp were removed; a stored `"percentile"` migrates to
`"quantile"` on read. New modes must also be whitelisted in
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
the MouseEvent as the `name` argument. Legacy names (`IB View`, `Trader View`) are still
accepted and normalised through the alias helpers so saved state migrates
without user intervention. `COLS_BY_KEY` is the lookup table; rendering goes
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
Definitions sheet; `tests/test_xlsx_definitions.py` fails until it has one.

### Metric explanations (hover tips) — one registry per surface
Every number the app shows explains itself on hover; the registries are:

| Surface | Registry | Rendered by |
|---|---|---|
| Holdings table headers | `COL_INFO` (plain text) | shared `[data-tip]` (`_APP_TIP`) |
| Analytics panels (Risk & return, Valuation, Concentration) | `METRIC_INFO` | `statRowHtml` → in-place `.pf-metric-tip` |
| Stock detail modal grids | `DETAIL_METRIC_INFO` | `renderDetailMetricGrid` → in-place `.pf-metric-tip` |
| Optimize side panel | `MPT_METRIC_INFO` (functions: read α / `n_boot` at hover) | `[data-rich-tip="mpt:<key>"]` |
| Optimize → Compute budget "i" | `MPT_BUDGET_INFO` (mirrors `frontier._BUDGETS`) | `[data-rich-tip="mpt-budget"]` |
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
exactly. Sort (`pf_contrib_sort`) and the Table | Chart toggle
(`pf_contrib_view`) live in `CONTRIB_UI` + localStorage; clicks re-render only
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
- **Inline tab rename**: double-click a `.pf-tab-label` →
  `beginTabRename()` swaps the span for an input; Enter commits, Esc
  cancels, blur commits. Calls `/api/portfolio/rename`.
- **Loading chip** (`lc-anchor`, `lc-spin`, `lcHtml`, `lcShow`,
  `lcHide`): a small terminal-flavoured indicator. Braille spinner cycles
  ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ via CSS keyframes on `content`. Optional shimmer bar +
  tabular-numerics counter (e.g. `21·47`). Used in: streaming status,
  startup, analytics empty state, modal detail load, FX hover load,
  Excel export button. CSS in `style.css` (~line 313), JS helpers
  (`lcHtml`/`lcShow`/`lcHide`) in `app.js` (~line 591).
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
  78vw × 76vh over a blurred backdrop. **Top tabs since v1.19** ("S3", modelled
  on the Claude Code settings): one header row holds the title, the sections as
  pills (`.settings-nav-item`, filled accent = selected, no lines between them,
  no group labels — the group is the tab's tooltip), the search and the close;
  the pane is full width at 14px+ type. A search that matches settings inside a
  section shows a count on its tab (`.settings-nav-count`) instead of the old
  sub-lines; Left/Right walk the tabs. The tabs **wrap** to a second line when
  the modal is narrow (a hidden sideways scroller left Logs/About unreachable
  below ~1200px). Its radii are the softer `--r-xl` / `--r-soft` tokens. Follows the
  `openMptOverlay` idiom (the `document.body.style.overflow` lock +
  `dataset.*PrevOverflow` restore) and registers in the global Esc handler.
  See §16 for why Logs exists. Rebuilt in v1.10.1 — five load-bearing rules:
  1. **`SETTINGS_SECTIONS` is the only place a section is declared**
     (`{id, group, label, description, keywords, items, render}`).
     `renderSettingsNav()` rebuilds the nav from it. Add a section there, never
     back in `index.html`. Sections today: General, Column Presets, Models & Data, API keys, Logs, About.
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
     empty query it must **bubble** to the global handler (`app.js` ~line 1999)
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

- **Overlays: `showOverlay()` / `hideOverlay()` are the ONLY way to open and
  close one** (v1.11.0). They own `lockBodyScroll`/`unlockBodyScroll`, a single
  **nesting counter** on `document.body`'s overflow. Never write
  `body.style.overflow`, and never toggle the `show` class by hand — both are
  enforced by `tests/test_frontend_overlays.py`, which also asserts the overlay
  inventory in that test matches every `id="*-bg"` in `index.html`, so a new
  backdrop can't skip the contract.
  Why a counter: overlays nest (About-MPT over MPT, the inline prompt over the
  weights popup) and the global Escape handler (`app.js` ~line 2087) closes
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

- **The limits panel PUSHES the workspace down; it never takes height from it.**
  `.pf-mpt-scroll` (wrapping `.pf-mpt-bounds` + `.pf-mpt-body`) is the single
  scroll column under the controls strip. `mptPinBodyHeight(true)` — called
  **before** `panel.hidden` flips, or it measures the already-pushed height —
  freezes `.pf-mpt-body` at its current height via `--mpt-body-h`, which the CSS
  reads as its `min-height`; `flex-shrink:0` is what makes the body refuse to
  yield. The column then overflows by exactly the panel's height, so the chart,
  slider and legend keep their **exact geometry** and simply move below the fold.
  Measured across an open/close cycle: backing store, CSS box, host height and
  `MPT._proj` all byte-identical; only `scrollHeight` changes.
  Closing must still call `mptRender()` — releasing the pin is a no-op unless the
  window was resized while the panel was up, in which case the body lands at a new
  height and the chart has to be re-measured (verified: 531px pinned during a
  860→1180px resize, correctly re-rendered at 851px on close).
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
  While the panel is open the height is **locked** (`MPT._chartH` +
  `mptBoundsPanelOpen()`) so a reflow of the obstructed box (a window resize)
  cannot re-measure it.
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
  **It is the only box left in the overlay, and that is deliberate** — a
  transient editor dropped on the tool should read as a distinct sheet, whereas
  `.pf-mpt-chartwrap` / `.pf-mpt-side` are the workspace itself and are now
  boxless (transparent, no border; the side panel keeps a single hairline
  `border-left` as a gutter rule). Padding on `.pf-mpt-bounds` is symmetric so
  the head and the last row sit the same distance from the frame.
  **Do NOT wire `mptWireAssetTips` to this grid, and do not give the rows
  `data-mpt-sym`.** That tooltip fires on `mousemove` and each event rebuilds the
  tip's `innerHTML` and calls `placeTip()` (a forced synchronous layout), i.e. a
  parse + reflow per frame while the pointer rests over the list. The stats it
  showed belong to the chart anyway.
  **The grid is not a scroller.** It used to have its own `max-height` *and*
  `overscroll-behavior: contain`, which is what made scrolling feel broken: a
  short inner scroller hits its end after ~100px and then **stops dead** instead
  of chaining to the parent, so getting down a 20-name book took a stack of
  separate gestures. One surface (`.pf-mpt-scroll`), one gesture. `.pf-mpt-bnd-head`
  is correspondingly non-sticky — `.pf-mpt-bounds` is `overflow:hidden`, so it is
  the sticky containing block and never scrolls; sticky there would be inert
  anyway, just with a promoted layer for nothing.

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
- **Chrome vs data colours (v1.19.2, the user's call).** Navy & Denim is for
  the chrome only (buttons, tabs, accents, kickers). **Data colours stay
  classic**: `--pos`/`--neg` are the pre-1.19 green/red (`#1f883d`/`#cf222e`
  light, `#3fb950`/`#f85149` dark) and the heat `blue` in `THEME_COLORS` is
  `#2563eb`/`#60a5fa`, not the accent — the user reads tables by colour pattern.
  Dark mode is **"Ink"** (`#0a0e14`, surfaces `#111823`, borders `#1d2938`):
  near-black with navy-tinted, not gray, surfaces; the 1.19 navy `#08182a` was
  too blue. The desktop splash (`desktop.py` `_BG`/`_ACCENT`) mirrors it.
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

## 11. Quick reference — current line landmarks

The frontend (HTML/CSS/JS) lives in `src/convexity/static/` (it was once
embedded in the monolithic `dashboard.py`, since removed — see §1/§2). Landmarks below are within `src/convexity/static/app.js` unless
noted otherwise. (Approximate. Use `grep -n` to confirm before editing.)

| What | File | Where |
|---|---|---|
| Process-global cache dicts | `cache.py` | ~14–16 |
| Views/weight-presets persistence | `persistence.py` | `save_view` ~109, `save_mpt_run` ~501 |
| Watchlists persistence | `persistence.py` | `load_watchlists` ~379 |
| Google-colon normaliser | `resolver.py` | `_normalize_google_colon` ~153 |
| `resolve_symbol` pipeline | `resolver.py` | ~164 |
| `fetch_one` (per-symbol row) | `fetcher.py` | ~117 |
| `fetch_detail` (modal payload) | `fetcher.py` | ~398 |
| FX layer (spot rates, basket index) | `fx.py` | `fx_rates` ~139, `fx_index_history` ~198 |
| Analytics (`analyze_portfolios_multi`) | `analytics.py` | ~214 |
| `compute_efficient_frontier` | `frontier.py` | ~72 |
| HTTP `Handler` (GET/POST/DELETE) | `server.py` | ~92 |
| `_pick_port` | `server.py` | ~689 |
| `main()` (browser-mode entry) | `server.py` | ~739 |
| Topbar HTML | `static/index.html` | ~13 |
| Loading-chip CSS (`.lc-*`, spinner keyframes) | `static/style.css` | ~313–332 |
| Tab rendering + rename | `static/app.js` | `renderTabs` ~2768, `beginTabRename` ~2814 |
| `exportXlsx` | `static/app.js` | ~3032 |
| Analytics request / render | `static/app.js` | `requestAnalytics` ~3108 |
| Mode pill bar (`renderModeBar`) | `static/app.js` | ~4454 |
| `runPrimary` (Build/Update button router) | `static/app.js` | ~4571 |

Use `grep -n "<symbol>" src/convexity/*.py src/convexity/static/*.{js,html,css}`
to relocate anything not listed above — the package is small enough that
this is faster than trusting a stale line table.
