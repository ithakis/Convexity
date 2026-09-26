# Distribution roadmap: from "clone + conda" to an installable app

Status: **planned** (written 2026-09-26, v1.13.0). One phase = one Claude Code
session = one PR. Do them in order; each phase lists what it depends on.

How to run a phase with Claude Code:

```
Read docs/plans/distribution-roadmap.md and implement Phase N.
Follow CLAUDE.md (§18 security rules). Work on a branch, open a PR,
and tick the phase's checklist in this file as part of the PR.
```

Rules that apply to every phase:
- One PR per phase. CI green before merging. Tag + release per CLAUDE.md §15
  when the phase bumps the version (ask before bumping).
- Update CLAUDE.md in the same PR whenever a phase changes an established
  pattern (paths, env, install, dependency manifests).
- Never commit runtime state, keys, the model's training data or absolute home
  paths (§18).
- After merging: pull the main checkout, restart the app, verify
  `/api/health` reports the new version.

---

## Phase 0 — Safety net (manual, 10 minutes, no PR)

Goal: nothing irreplaceable exists in only one place.

**Done 2026-09-26** — copied to an external drive, SHA-256/byte-verified.

- [x] Copy `~/.convexity/ml_model/` (the deployed `mlsent-v1.1` model, the
      **only** copy) to a second location (external drive / cloud drive).
- [x] Copy the runtime state files (`.convexity_*.json`) and the key files the
      same way.
- [x] Note: `ml/data/` (FNSPID parquet, ~4.8 GB) no longer exists on disk. It
      is rebuildable from the public dataset with `ml/scripts/00→06`; no action
      needed unless you want to retrain soon.

---

## Phase 1 — Housekeeping (small PR, v1.13.1)

Goal: remove stale/public-unfriendly bits before the bigger changes.

**Done 2026-09-26** (v1.13.1) — committed on the `distribution` branch.

- [x] README: remove "opens the browser automatically" (removed in v1.12.3).
- [x] Move the root wishlist file's open items into GitHub Issues (labels
      `feature`, `idea` — issues #1–#8), then `git rm` it and drop its
      references in CLAUDE.md §2/§10. Items already shipped were not filed.
- [x] Rename the market-data API comparison doc → `docs/market-data-apis.md`
      (no inbound links existed).
- [x] Add `.github/dependabot.yml` (pip + github-actions, weekly).

Done when: CI green, no references to the retired wishlist file remain.

---

## Phase 2 — `pyproject.toml` + uv (v1.14.0, the big one — part 1)

Goal: one dependency manifest, a lockfile, and real entry-point commands.
Depends on: Phase 1.

- [ ] Add `pyproject.toml` (PEP 621, build backend `hatchling`):
  - `name = "convexity"`, dynamic version read from `convexity/__init__.py`.
  - `requires-python = ">=3.11"` (verify numba/lightgbm/PySide6 wheels for the
    upper bound and pin it, e.g. `<3.14`).
  - `dependencies` = exactly `envcheck.REQUIRED` (yfinance, pandas, numpy,
    requests, numba, rapidfuzz, openpyxl, lxml, openai, lightgbm, scikit-learn).
  - extras: `desktop = [PySide6>=6.7]`, `dev = [pytest, scipy, ruff, pyflakes]`,
    `train = [duckdb, flaml]` (pyarrow stays out — see CLAUDE.md §4).
  - `[project.scripts] convexity = "convexity.server:main"`
  - `[project.gui-scripts] convexity-app = "convexity.desktop:main"`
    (add a `main()` to `desktop.py` if it has none).
  - Package data: `convexity/static/*`, `convexity/data/*` (lm_lexicon.json).
- [ ] `uv lock` and commit `uv.lock`.
- [ ] `scripts/check_dependency_manifests.py`: check `envcheck.REQUIRED`
      against `pyproject.toml` instead of requirements.txt/environment.yml.
- [ ] CI: `astral-sh/setup-uv` pinned to a commit SHA; jobs use
      `uv sync --locked --extra dev` and `uv run pytest`. Desktop smoke job
      adds `--extra desktop`.
- [ ] Keep `requirements.txt` / `environment.yml` for **one** release with a
      header comment "deprecated, see pyproject.toml" (so existing installs can
      still `update.sh`), then delete in Phase 4.
- [ ] Verify on this Mac: `uv sync --extra desktop && uv run convexity-app`
      opens the app, ML runtime status is OK
      (`uv run python -c "from convexity import ml_sentiment as m; print(m.runtime_status())"`).
- [ ] **Check lightgbm on macOS**: confirm the PyPI wheel works without
      Homebrew `libomp` on a clean machine/user account. If it needs libomp,
      document it and have `install.sh` check for it.

Done when: `uv run pytest` passes locally and in CI; the app runs from
`uv run convexity` and `uv run convexity-app`.

---

## Phase 3 — User data out of the repo folder (v1.14.0 — part 2)

Goal: an installed package never writes next to its own code. Required before
`uv tool install` can work. Depends on: Phase 2.

- [ ] New `convexity/paths.py` — single source of truth:
  - data dir: macOS `~/Library/Application Support/Convexity/`,
    Windows `%APPDATA%\Convexity\`, Linux `$XDG_DATA_HOME/convexity`
    (default `~/.local/share/convexity`). Env override `CONVEXITY_HOME`.
  - subpaths: `state/` (the `.convexity_*.json` files, dot prefix dropped),
    `models/` (replaces `~/.convexity/ml_model/`), `symbol_db.sqlite`,
    `config.json` (API keys), `logs/`.
  - Stdlib only (no `platformdirs` dependency needed).
- [ ] Point every consumer at it: `persistence.py` (`_repo_root()` uses),
      `news_sentiment.py` cache/history files, `symbol_db.py._DB_PATH`,
      `relevance.py`, `ml_sentiment.py` model dir (keep `MLSENT_MODEL_DIR`
      override), `desktop.py` log path, `build_symbol_db.py` default output.
- [ ] Keys: `helpers._load_local_secret` order becomes env var → `config.json`
      in the data dir → legacy key file found by walking up (keep for one
      release).
- [ ] Extend `migrate.py`: on first launch, **copy then verify then remove**
      repo-root state files and `~/.convexity/ml_model/` into the new
      locations. Never overwrite a newer file. Log every move. Add tests in
      `tests/test_migrate.py` (tmp dirs only).
- [ ] Tests must never touch the real data dir: a pytest fixture sets
      `CONVEXITY_HOME` to a tmp path (autouse, in `tests/conftest.py`).
- [ ] Update CLAUDE.md §2 (file layout), §4 (persistence), §18.

Done when: a fresh checkout + `uv run convexity` creates the data dir, your
existing portfolios appear after migration, and the repo folder stays clean
after using the app (`git status` shows nothing new, even untracked).

---

## Phase 4 — Installers on uv + retire conda (v1.14.0 — part 3)

Depends on: Phases 2–3.

- [ ] `install.sh` (macOS/Linux):
  1. install uv if missing (official installer, `curl -LsSf https://astral.sh/uv/install.sh | sh`);
  2. `uv tool install "convexity[desktop] @ git+https://github.com/ithakis/Convexity@vX.Y.Z"`
     (pinned to the latest release tag, not `main`);
  3. build `Convexity.app` whose launcher execs the `convexity-app` command;
     generate `icon.icns` as today (icon is shipped as package data).
- [ ] `install.ps1`: same with the PowerShell uv installer, shortcuts point at
      `convexity-app.exe`. Keep `Assert-Success` after every native call.
- [ ] `update.sh` / `update.ps1` → `uv tool upgrade convexity` (or reinstall at
      the newest tag) + rebuild launcher.
- [ ] `Launch Dashboard.command` → runs `uv run convexity` (dev checkout).
- [ ] Delete `requirements.txt`, `environment.yml`; remove Miniforge code.
- [ ] CI: keep the icon/shortcut smoke jobs; add a job that runs
      `uv tool install .` then `convexity --help` (or a boot-and-health check).
- [ ] CLAUDE.md §3/§14: `pt` env retired; dev workflow is `uv sync` / `uv run`.
- [ ] Manual verification on a clean macOS user account (System Settings →
      Users & Groups → new user) — the only realistic "new user" test.

Done when: one command on a clean account gives a working app.

---

## Phase 5 — Model download on first run (v1.15.0)

Goal: a fresh install has a working Market read. Depends on: Phase 3.

- [ ] Package the model: `ml/scripts/10_export_artifact.py --tarball` →
      `mlsent-v1.1.tar.gz` (the six artifact files).
- [ ] Upload it as an asset to a GitHub Release (e.g. `model-mlsent-v1.1`).
- [ ] `convexity/model_fetch.py`: if `models/mlsent-v1.1/` is missing, download
      the asset from a URL **pinned in code**, verify against a **SHA-256
      pinned in code**, extract safely (reject absolute paths / `..`), then
      reload. Stdlib only. Runs in the background; never blocks startup.
- [ ] Settings → Models & Data: status ("downloading / installed / failed:
      reason") and a "Retry download" button.
- [ ] Failure is visible, not silent (same rule as `_warn_ml_once`).
- [ ] Tests: checksum mismatch rejected, path traversal rejected, offline
      degrades cleanly (local HTTP server in tests, no real network).
- [ ] Document the model-release procedure in CLAUDE.md §4 (retrain → new
      tarball → new release → bump pinned URL + hash).

---

## Phase 6 — API keys in Settings (v1.15.0)

Depends on: Phase 3.

- [ ] Settings → "API keys" section (add it to `SETTINGS_SECTIONS`): Finnhub and
      NVIDIA NIM fields, masked, with "Test" buttons (one cheap call each) and
      links to where to get a free key.
- [ ] `POST /api/keys` writes `config.json` (file mode 0600) — the key is never
      echoed back; `GET` returns booleans only (same as `/api/runtime-status`).
- [ ] Keys reload **without a restart** (today they're read once at import —
      add a reload hook in `news_sentiment` and `finnhub_adapter`).
- [ ] First-run banner: "Add your free API keys to enable News" when missing.
- [ ] Tests: key never appears in any response or log line.

---

## Phase 7 — Repository tidy-up (v1.15.x)

Goal: looks and behaves like a maintained product. Can run any time after
Phase 4.

- [ ] `src/` layout: move `convexity/` → `src/convexity/`; fix pyproject, CI,
      hooks, tests, CLAUDE.md paths.
- [ ] Root contains only: README, LICENSE, CHANGELOG, SECURITY, AGENTS.md,
      CLAUDE.md, pyproject.toml, uv.lock, install.sh, `.github/`, `src/`,
      `tests/`, `docs/`, `ml/`, `scripts/`, `packaging/`, `assets/`.
  - `install.ps1`, `update.*`, `Launch Dashboard.command` → `packaging/`
    (keep `install.sh` at root as the curl target, or add a root stub).
  - `icon.png` → `assets/` (and into package data).
  - `build_symbol_db.py` → `convexity build-symbols` subcommand.
  - `dashboard.py` shim → delete.
- [ ] ruff replaces pyflakes (config in pyproject); make lint blocking; run
      `ruff format` in its own commit.
- [ ] Split CLAUDE.md: keep a short CLAUDE.md (rules, map, gotchas index) and
      move deep sections to `docs/architecture/*.md` (backend, frontend, news,
      mpt, desktop, jobs, security). Nothing is deleted, only moved and linked.
- [ ] README: badges (CI, release, license), screenshot (synthetic portfolio),
      one-line install per OS, "what you need" (free Finnhub + NVIDIA keys),
      uninstall instructions.
- [ ] `.github/ISSUE_TEMPLATE/` (bug, feature).
- [ ] Optional, later: split `static/app.js` (~490 KB) into ES modules, no
      build step.

---

## Phase 8 — Reference pack collector (v1.16.0)

Goal: new and returning users get a calibrated Market read and a meaningful
Track record on day one. Depends on: Phase 5.

Design:
- New **separate public repo** `ithakis/Convexity-data` with a GitHub Actions
  cron (weekdays, after US close). Keys in Actions secrets only.
- Universe: S&P 500 (fixed list committed in that repo).
- Per run: Finnhub news for the universe (rate-limited, ~9 min), the **Market
  read only** (local LightGBM — no LLM, no look-ahead), forward returns from
  yfinance joined once known.
- Publishes to a Hugging Face **dataset** (or a rolling `data-latest` GitHub
  release):
  - `anchor.json` — last 90 days of `market_score` across the universe.
  - `history.parquet` — records shaped like the local sentiment history.
  - `manifest.json` — date, row counts, model version, schema version.
- Published: ticker, date, derived scores, at most a URL. **Never headline or
  article text** (news licensing). Nothing from users is ever uploaded.
- Monitoring: healthchecks.io ping at the end of each run.

Tasks:
- [ ] Collector repo + workflow (reuses `convexity` as a pinned dependency so
      the featurizer is identical — train/serve parity).
- [ ] App: `convexity/reference_pack.py` fetches the pack when older than 24 h,
      validates schema + model version, parses as data only (no pickle).
- [ ] Market read anchor: `reference` when local history < 200, `live` after;
      UI names which one (extends `anchor` values).
- [ ] Track record: add a "Model (500 names)" view from the pack next to "Your
      holdings" (local).
- [ ] Settings toggle to disable the download (privacy: it only reveals that
      the app is running, not what you hold — say so).

---

## Phase 9 — Website (optional)

- Static site on GitHub Pages or Cloudflare Pages; download buttons link to
  `github.com/ithakis/Convexity/releases/latest`; OS-detected install command;
  `/status` page generated from the collector's manifest.
- 2FA on every account involved.

---

## Deliberately out of scope

- Signed/notarized `.app` or PyInstaller bundles (cost, 500 MB+, Gatekeeper).
- Hosting the FNSPID training data (already public upstream; rebuildable).
- Crowdsourcing scores from users.
