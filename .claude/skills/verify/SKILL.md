---
name: verify
description: Run Convexity for real (installed package, temp data folder, the user's keys via env when the change calls Finnhub/NIM) and drive the UI, API, CLI, desktop window and installers to verify a change.
---

# Verifying Convexity at runtime

Never touch the user's real data folder, and never print a key. Drive the app
from an install of a clean tree (no legacy key files, no state). When the change
calls Finnhub or NVIDIA NIM, validate it live with the user's keys passed as
env vars (CLAUDE.md §3 "Use and validate the user's real API keys"):

```bash
S=<scratchpad>/v; mkdir -p $S/tree $S/tools $S/bin $S/home $S/run
git archive HEAD | tar -xf - -C $S/tree          # clean tree: no keys, no state
(cd $S/tree && UV_TOOL_DIR=$S/tools UV_TOOL_BIN_DIR=$S/bin uv tool install --python 3.14 ".[desktop]")
cd $S/run && CONVEXITY_HOME=$S/home PYTHONUNBUFFERED=1 $S/bin/convexity > server.log 2>&1 &
lsof -nP -iTCP -sTCP:LISTEN | grep python       # 8765 is usually the user's own app; yours lands on 8766+
```

Always set `CONVEXITY_HOME` — even `convexity --help` is an app launch and runs
the data migration. Kill the server by its python PID (not the bash wrapper);
never `pkill -f convexity`.

Flows worth driving (browser pane on the printed URL):
- Portfolio → + New portfolio ("Untitled 1") → paste `AAPL, MSFT, NVDA, SPY,
  microsoft, JPM` into the search box → Add → chips, red Refresh → Refresh:
  rows, analytics chart, analyst section, red clears. ✕ a chip → red again.
- Reload → the saved tab is restored. Click a row → detail modal; 1M = 30m bars.
- Optimize → Run (frontier + cloud + band); `/api/efficient-frontier` with
  `bounds` and `budget: "light"` for box/infeasible probes.
- News tab and Refresh: with the keys in the env, one refresh of a few names
  checks Finnhub + the News read live ("n/n scored"); without them the news
  phase makes zero calls ("n failed" is expected).
- Settings → About / Models & Data / API keys (save + Remove a fake key; never
  press Test) / Logs.
- Symbol pack + company search: `convexity build-symbols --out <dir> --limit 250`
  (live Yahoo screener, ~2 min; the full build is ~50 min) or a synthetic pack
  (`tests/test_symbol_db.py:pack()`), served like the reference pack below
  (same `CONVEXITY_REFERENCE_URL`, files `symbols-manifest.json` +
  `symbols.ndjson.gz`). Then Find companies: `microsft`, `european banks`,
  `tech with D/E < 0.8 and current ratio > 1`, edit a chip, Add two cards
  (Refresh turns red), a pasted list `AAPL, microsft, ZZZZ`; with the NIM key,
  `best tech companies with D/E < .8 and current ratio > 1` (AI rank chip) and
  `GLP-1 drug makers` (verified picks with why-lines).
- Reference pack, app side: serve a pack directory with
  `python3 -m http.server <port> --bind 127.0.0.1` and start the app with
  `CONVEXITY_REFERENCE_URL=http://127.0.0.1:<port>/<dir>/` → Settings → Models &
  Data → Reference data, the News tab's Market read label, Track record →
  Model (500 names). A synthetic pack is quickest (`reference_pack.gzip_json`
  + a manifest of `sha256`/`bytes`); flip a byte to see a rejected download.
- `convexity build-reference-pack --out <dir> --limit 5` with a fake
  `FINNHUB_API_KEY` and `CONVEXITY_FINNHUB_BASE=http://127.0.0.1:<port>/api/v1/`
  pointing at a small stub that returns synthetic `/company-news` JSON — never
  a real key without asking.
- Migration: `CONVEXITY_LEGACY_ROOT=<dir with a synthetic .convexity_watchlists.json>`.
- Desktop: `QT_QPA_PLATFORM=offscreen CONVEXITY_HOME=... $S/bin/convexity-app`,
  then read `$CONVEXITY_HOME/logs/desktop.log` for `loadFinished ok=True`.
- Installers: `CONVEXITY_SOURCE=$S/tree INSTALL_APPS_DIR=... INSTALL_DESKTOP_DIR=...
  UV_TOOL_DIR=... bash $S/tree/packaging/update.sh < /dev/null`;
  `bash "$S/tree/packaging/Launch Dashboard.command"` (stop it via `.dashboard.pid`).
  Windows installer under pwsh: see docs/architecture/desktop.md (uv shim).
