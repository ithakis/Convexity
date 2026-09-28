# Convexity

[![CI](https://github.com/ithakis/Convexity/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ithakis/Convexity/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/ithakis/Convexity?sort=semver)](https://github.com/ithakis/Convexity/releases/latest)
[![License: all rights reserved](https://img.shields.io/badge/license-all%20rights%20reserved-lightgrey)](LICENSE)

**Convexity** — a portfolio optimization app built for long-term horizon investing. Runs locally: no cloud accounts, no data leaving your machine.

Built on top of [yfinance](https://github.com/ranaroussi/yfinance) and a tiny stdlib HTTP server. Open it in any browser, paste your tickers, and get a live heat-mapped table in seconds.

![Holdings table](docs/screenshots/table.png)

## A tour

**Portfolio optimization.** Black-Litterman expected returns (market-cap equilibrium blended with analyst price-target views) and a mean-CVaR efficient frontier solved by a custom 8-core interior-point solver. Slide along the frontier, see the tail risk, weights and bootstrap uncertainty band, and apply the result as a weight preset.

![Portfolio optimization](docs/screenshots/optimize.png)

**Portfolio analytics.** Performance against the S&P 500 (plus Nasdaq and a sector-mix overlay), moving averages, drawdown, and a risk & return card with Sharpe, Sortino, Calmar, beta and tracking error against any benchmark.

![Portfolio analytics](docs/screenshots/analytics.png)

**Analyst sentiment.** Weighted consensus rating, rating distribution, price-target upside (mean and median) and the full range of analyst targets for every holding.

![Analyst sentiment](docs/screenshots/analyst.png)

**News & sentiment.** Two independent reads of every holding's headlines: the *News read* (an LLM scoring each headline across financials, outlook, competition, regulation and street view) and the *Market read* (a statistical model of how prices have historically reacted to news like this). Plus movers, what to watch, a market-risk line and a per-headline timeline.

![News & sentiment](docs/screenshots/news.png)

---

## Features

| Column | Description |
|---|---|
| **Price / Market Cap** | Last close price and total market capitalisation |
| **P/S** | Price-to-Sales — heat map anchored at 10× (expensive) |
| **P/E** | Price-to-Earnings — heat map anchored at 40× |
| **% YTD / % 1Y** | Diverging colour scale: green = positive, red = negative |
| **Chart 1Y** | 252-day sparkline, coloured by 1-year return sign |
| **Δ Highs** | Distance from all-time high — bar grows as drawdown deepens |
| **RS Rank 1M** | 12-month relative-strength histogram |
| **20 / 50 / 200 SMA** | Moving-average flags (▲ above, ▼ below) |

**Additional**

- **Streaming progress bar** — rows appear as they load, one at a time
- **Sort** — click any column header or use the ⇅ Sort menu
- **Dark / light mode** — pill toggle, preference saved to `localStorage`
- **Portfolios** — named portfolios as tabs, saved in your data folder, restored on launch
- **Excel export** — every saved portfolio, its analytics and sentiment in one `.xlsx`
- **Column guide** — ⓘ button opens a LaTeX-rendered column reference
- **Click-through detail** — click any row for a full-size 1-year chart and stats

---

## Install

One command installs [uv](https://docs.astral.sh/uv/) if needed, then the
latest release, and adds a launcher. Nothing else to set up.

**macOS / Linux**

```bash
curl -LsSf https://raw.githubusercontent.com/ithakis/Convexity/main/install.sh | bash
```

**Windows** (PowerShell; creates Start Menu + Desktop shortcuts)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/ithakis/Convexity/main/packaging/install.ps1 | iex"
```

Then launch **Convexity** from Launchpad / Spotlight / the Start Menu or your
Desktop (on Linux: run `convexity-app`). It opens in its own window (PySide6 + QtWebEngine). The app is
unsigned: on macOS, right-click → Open the first time; on Windows, SmartScreen
may ask once. Prefer a browser tab? Run `convexity` in a terminal and open the
URL it prints.

**Update:** run the same command again. It installs the newest release and
rebuilds the launcher; your data is never touched.

## What you need

- **Nothing for prices, analytics and the optimizer** — quotes come from
  Yahoo Finance (via yfinance), no account or key needed.
- **Two free API keys for News & sentiment**:
  [Finnhub](https://finnhub.io/register) (company news) and
  [NVIDIA NIM](https://build.nvidia.com/) (the News read). Add them in the app
  under **Settings (gear) → API keys**; each has a Test button and they take
  effect immediately. Everything else works without them.
- **macOS:** the Market read (a LightGBM model) needs Homebrew's `libomp`
  (`brew install libomp`); the installer checks for it and offers to install it.
  The model itself is downloaded once on first launch and checked
  against a pinned SHA-256.

**Your data** (portfolios, caches, API keys in `config.json`) lives in a
per-user folder, never next to the code:

| OS | Data folder |
|---|---|
| macOS | `~/Library/Application Support/Convexity/` |
| Windows | `%APPDATA%\Convexity\` |
| Linux | `~/.local/share/convexity/` (or `$XDG_DATA_HOME/convexity/`) |

## Uninstall

**macOS / Linux**

```bash
uv tool uninstall convexity
rm -rf /Applications/Convexity.app ~/Applications/Convexity.app ~/Desktop/Convexity.app   # macOS launcher + shortcut
rm -rf ~/Library/Application\ Support/Convexity    # optional: your data (Linux: ~/.local/share/convexity)
```

**Windows** (PowerShell)

```powershell
uv tool uninstall convexity
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Convexity.lnk", "$([Environment]::GetFolderPath('Desktop'))\Convexity.lnk", "$env:LOCALAPPDATA\Convexity" -Recurse -ErrorAction SilentlyContinue
Remove-Item "$env:APPDATA\Convexity" -Recurse   # optional: your data
```

uv itself stays installed; remove it with `uv self uninstall` if nothing else
uses it.

## From a checkout (development)

```bash
uv sync --extra dev --extra desktop
CONVEXITY_HOME=$(mktemp -d) uv run convexity-app   # desktop window, throwaway data folder
CONVEXITY_HOME=$(mktemp -d) uv run convexity       # browser mode: prints http://localhost:8765/
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Leave out `CONVEXITY_HOME` to use your real data folder. On macOS you can also
double-click **`packaging/Launch Dashboard.command`**, which runs
`uv run convexity` and prints the URL to open. Engineering notes for
contributors (and AI agents): [CLAUDE.md](CLAUDE.md) and
[docs/architecture/](docs/architecture/).

---

## Usage

1. Click **Portfolio** to open the input box.
2. Paste tickers (or company names — `microsoft`, `BME: SAN` and typos work)
   separated by commas or newlines.
3. Press **Build Dashboard** (or `Cmd/Ctrl + Enter`).
4. Rows stream in as data is fetched — a thin progress bar tracks completion.
   The portfolio is saved automatically and restored on the next launch.
5. **Refresh** (or `R`) updates quotes and news in the background; long-press
   it (or `Shift+R`) to refresh every saved portfolio.

---

## Requirements

Python 3.11–3.14 (the installer fetches 3.11 through uv). Every dependency is
declared in `pyproject.toml` and locked in `uv.lock`; `src/convexity/envcheck.py`
lists the ones the running app checks for at startup.

---

## Architecture

A Python package (`src/convexity/`) serving a vanilla-JS frontend from a
stdlib `ThreadingHTTPServer` on `127.0.0.1` — no build step, no framework.

```
src/convexity/
├── cli.py            `convexity` command: the server, or `convexity build-symbols`
├── server.py         HTTP routes (NDJSON streaming for the table, jobs, the optimizer)
├── desktop.py        the native window (`convexity-app`): same server, in-process
├── fetcher.py        per-symbol yfinance rows (5-worker streaming build)
├── analytics.py      portfolio stats, benchmarks, exposure, analyst consensus
├── frontier.py/mpt.py  Black-Litterman returns + mean-CVaR frontier (numba solver)
├── news_sentiment.py the News read (Finnhub + yfinance headlines, NVIDIA NIM LLM)
├── ml_sentiment.py   the Market read (LightGBM model, downloaded on first run)
├── jobs.py           background refresh jobs (cancellable, survive a reload)
├── persistence.py    portfolios and settings as JSON in the data folder
└── static/           index.html, app.js, style.css
```

Network access is limited to Yahoo Finance, Finnhub, NVIDIA NIM, the KaTeX CDN
(column-guide formulas) and GitHub Releases (the one-time model download). The
full design notes are in [docs/architecture/](docs/architecture/).

---

## Rate limits

Yahoo Finance is an unofficial API. To avoid 429 errors:

- Concurrency is capped at **5 parallel requests**
- Each symbol retries up to **3 times** with exponential back-off
- yfinance 1.0+ uses `curl_cffi` for TLS fingerprinting; no custom session is passed

---

## License

All rights reserved — the source is published for viewing only. No use, copying,
modification, redistribution or commercial use without written permission.
See [LICENSE](LICENSE).
