# Distribution roadmap: from "clone + conda" to an installable app

Status: **Phases 0–4 done** (v1.14.0, 2026-09-27), **Phase 5 done** (v1.14.1); written 2026-09-26, v1.13.0. One phase = one Claude Code
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

**Done 2026-09-26** — committed on the `distribution` branch, no version bump
(1.14.0 is cut when Phase 4 lands; CHANGELOG has an "Unreleased" section).
Findings:
- Upper bound is `<3.15`, not `<3.14`: numba 0.67 / llvmlite 0.49 ship cp314
  wheels, lightgbm is `py3`, PySide6 is abi3. The full suite passes on 3.11
  and 3.14. `.python-version` pins 3.11 for dev and CI.
- `dev` extra omits scipy: it is a runtime dependency (`envcheck.REQUIRED`,
  scipy.sparse in the ML featurizer), so it is already in `dependencies`.
- The manifest check still also checks requirements.txt/environment.yml while
  they exist (update.sh still builds `pt` from them); it drops them
  automatically once Phase 4 deletes the files.
- **libomp: yes, needed.** The PyPI lightgbm wheel loads `@rpath/libomp.dylib`
  from Homebrew/MacPorts rpaths only; sklearn's bundled copy does not satisfy
  it. Tested by pointing the rpaths at a nonexistent path, since this Mac has
  Homebrew libomp installed. Documented in CLAUDE.md §4. Today's conda
  install.sh is unaffected (conda-forge bundles llvm-openmp), so the check
  moves to Phase 4.
- Gotcha hit once: `.venv` files flagged macOS-`hidden` made Qt skip its
  cocoa plugin (CLAUDE.md §3).

- [x] Add `pyproject.toml` (PEP 621, build backend `hatchling`):
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
- [x] `uv lock` and commit `uv.lock`.
- [x] `scripts/check_dependency_manifests.py`: check `envcheck.REQUIRED`
      against `pyproject.toml` instead of requirements.txt/environment.yml.
- [x] CI: `astral-sh/setup-uv` pinned to a commit SHA; jobs use
      `uv sync --locked --extra dev` and `uv run pytest`. Desktop smoke job
      adds `--extra desktop`.
- [x] Keep `requirements.txt` / `environment.yml` for **one** release with a
      header comment "deprecated, see pyproject.toml" (so existing installs can
      still `update.sh`), then delete in Phase 4.
- [x] Verify on this Mac: `uv sync --extra desktop && uv run convexity-app`
      opens the app, ML runtime status is OK
      (`uv run python -c "from convexity import ml_sentiment as m; print(m.runtime_status())"`).
- [x] **Check lightgbm on macOS**: confirm the PyPI wheel works without
      Homebrew `libomp` on a clean machine/user account. If it needs libomp,
      document it and have `install.sh` check for it.

Done when: `uv run pytest` passes locally and in CI; the app runs from
`uv run convexity` and `uv run convexity-app`.

---

## Phase 3 — User data out of the repo folder (v1.14.0 — part 2)

Goal: an installed package never writes next to its own code. Required before
`uv tool install` can work. Depends on: Phase 2.

**Done 2026-09-26/27** — committed on the `distribution` branch, no version bump.
Real data migrated (see the last bullet). Deviations and findings:
- Names (your choice): `state/views.json` etc. — dot **and** `convexity_`
  prefix dropped. The desktop log moved to `<data>/logs/desktop.log` on every
  OS (was `~/Library/Logs/Convexity.log`), so a test launch can never truncate
  a running app's log. `symbol_db.sqlite` moved to the data folder.
- Migration is stricter than "never overwrite a newer file": it never
  overwrites **any** existing destination. Identical ⇒ the old copy is removed
  (a crash between copy and remove); different ⇒ both kept, logged CONFLICT.
  The source is re-hashed right before removal, so a write that lands during
  migration is never lost. Model dirs are staged and renamed in one step.
- **Incident during development (fixed):** migration first ran on *any*
  `import convexity`. `scripts/check_dependency_manifests.py`, run without
  `CONVEXITY_HOME`, migrated the real data while the two `pt` apps (old code)
  were running. Everything was SHA-256 verified; I copied it all back
  (19 files, byte-identical to a snapshot taken minutes earlier) and removed
  the data folder ~3 minutes later; the apps wrote nothing in between. Now only
  an app launch migrates (`convexity._launched_as_app`), covered by a test.
- Gate (not in the plan): with `CONVEXITY_HOME` set, nothing migrates unless
  `CONVEXITY_LEGACY_ROOT` / `CONVEXITY_LEGACY_HOME` name the source. Without it
  a temp-dir dev run would have emptied the real checkout. Added
  `python -m convexity.migrate --dry-run`.
- Keys are **not** migrated into `config.json` (Phase 6 writes it); the old key
  files are never deleted and remain a logged fallback. Same for
  `~/.convexity/ml_model` and a checkout-root `symbol_db.sqlite` while not
  migrated.
- `tests/conftest.py` also sets `PORTFOLIO_SYMBOL_DB`: the symbol-DB fallback
  otherwise opened the checkout's real DB (SQLite touches `-shm` even
  read-only). Tests still *read* `~/.convexity/ml_model` so the model tests run.
- Known bug fixed and verified: a wheel installed with `uv pip install` wrote
  `.convexity_watchlists.json` into `site-packages/convexity/` (reproduced on
  the pre-change build); now it lands in the data folder, and the migration
  rescues the stray file (its "legacy root" is the package dir).
- Verified at runtime: fresh clone + `uv run convexity` with a temp
  `CONVEXITY_HOME` (a saved portfolio lands in `state/`; `git status --ignored`
  gains only `convexity/__pycache__/`); the wheel check; the migration on a
  **copy** of the real state (19 files, 0 SHA-256 mismatches, second run a
  no-op, all 10 portfolios in the UI, model loaded from `models/`);
  `uv run convexity-app` boots, ML available.
- Post-commit runtime verification (2026-09-27), all through real app launches
  on copies of the real data: conflict (dest differs → both kept, app uses the
  data folder), stale temp from a dead run (swept), live SQLite WAL (skipped,
  logged fallback), `kill -9` at 20 points during migration (no loss; two
  kills left a half-copied `symbol_db.sqlite` temp / half-staged model dir,
  both swept and completed on relaunch), two instances launched at once (no
  loss), keys via `config.json` in an installed wheel, `build_symbol_db.py`
  default output, desktop app. Fixed from it: two simultaneous launches
  logged false CONFLICT lines (now judged as identical ⇒ dedup); a malformed
  `config.json` was ignored silently (now logged once); Settings → About still
  listed the old file names (now shows the data folder, from `/api/health`).
- [x] Real data migrated 2026-09-26 21:54 — by the first launch of the new code
      from `Convexity.app`, not by hand. Verified afterwards: all state files
      parse, 10 portfolios, both model versions; backup with a SHA-256 manifest
      in `~/Backups/convexity-data-folder-2026-09-26/`.

- [x] New `convexity/paths.py` — single source of truth:
  - data dir: macOS `~/Library/Application Support/Convexity/`,
    Windows `%APPDATA%\Convexity\`, Linux `$XDG_DATA_HOME/convexity`
    (default `~/.local/share/convexity`). Env override `CONVEXITY_HOME`.
  - subpaths: `state/` (the `.convexity_*.json` files, dot prefix dropped),
    `models/` (replaces `~/.convexity/ml_model/`), `symbol_db.sqlite`,
    `config.json` (API keys), `logs/`.
  - Stdlib only (no `platformdirs` dependency needed).
- [x] Point every consumer at it: `persistence.py` (`_repo_root()` uses),
      `news_sentiment.py` cache/history files, `symbol_db.py._DB_PATH`,
      `relevance.py`, `ml_sentiment.py` model dir (keep `MLSENT_MODEL_DIR`
      override), `desktop.py` log path, `build_symbol_db.py` default output.
      Also `ml/scripts/10_export_artifact.py --deploy` and the key-missing
      messages in the UI.
- [x] Keys: `helpers._load_local_secret` order becomes env var → `config.json`
      in the data dir → legacy key file found by walking up (keep for one
      release).
- [x] Extend `migrate.py`: on first launch, **copy then verify then remove**
      repo-root state files and `~/.convexity/ml_model/` into the new
      locations. Never overwrite a newer file. Log every move. Add tests in
      `tests/test_migrate.py` (tmp dirs only).
- [x] Tests must never touch the real data dir: a pytest fixture sets
      `CONVEXITY_HOME` to a tmp path (autouse, in `tests/conftest.py`).
- [x] Update CLAUDE.md §2 (file layout), §4 (persistence), §18.

Done when: a fresh checkout + `uv run convexity` creates the data dir, your
existing portfolios appear after migration, and the repo folder stays clean
after using the app (`git status` shows nothing new, even untracked).

---

## Phase 4 — Installers on uv + retire conda (v1.14.0 — part 3)

Depends on: Phases 2–3.

**Done 2026-09-27** (v1.14.0) — committed on the `distribution` branch. The
installer only works for everyone once `v1.14.0` is tagged and released: it
installs the latest release and refuses anything older than v1.14.0 (no
pyproject). Deviations and findings:
- **Source archive, not `git+https`.** The installers install from
  `github.com/ithakis/Convexity/archive/refs/tags/<tag>.tar.gz`. A clean Mac
  has no git (`/usr/bin/git` only offers the Xcode tools), and uv needs it for
  `git+` URLs. The tag is the latest GitHub release, looked up at install time,
  so install.sh never needs editing per release. `uv tool upgrade` cannot move
  a tag-pinned URL, so **update = re-run the installer** (`--force`);
  `update.sh`/`update.ps1` are thin wrappers.
- `uv tool install` resolves from pyproject's ranges, **not `uv.lock`**. The
  new CI job exercises exactly that resolution.
- The icon moved into the package (`convexity/assets/icon.png`); at the repo
  root an installed app could not see it and ran iconless.
- **Keys:** an installed package cannot walk up to a checkout's `.finnhub_key`
  / `.nvidia_key`, so an upgrading user would silently lose the News read. When
  the installer runs from a checkout it copies them into `config.json` (0600,
  never overwrites). A curl install has no checkout and nothing to copy.
- Windows shortcuts point straight at the tool's `convexity-app.exe`; the
  conda-era `.vbs` + `conda run` wrapper is gone (it existed for conda's DLL
  search path only).
- CI: rather than keeping icon-only jobs next to new ones, the macOS and
  Windows jobs now run the **whole** installer (uv made that ~2 min instead of
  10-15); `tool-install-smoke` (ubuntu) installs the package and boots it
  outside the checkout. Not run yet — nothing was pushed.
- Verified locally: full suite; a simulated clean account (empty `HOME`,
  `PATH=/usr/bin:/bin`, installer piped as `curl | bash` would) — official uv
  installer, managed Python 3.11 download, envcheck, `.app` with v1.14.0,
  bundle launcher boots the app (`loadFinished ok=True`); the CI health check
  against the installed `convexity`; install from a GitHub-style source
  archive served over HTTP; libomp missing (brew / no brew / no terminal); tag
  guard (v1.13.0 refused, v1.14.0+ accepted); key copy (0600, no overwrite,
  bad JSON untouched); `Launch Dashboard.command` + SIGTERM through `uv run`.
- Your own `/Applications/Convexity.app` was reinstalled from this checkout
  (`CONVEXITY_SOURCE=. ./install.sh`) on 2026-09-27: it now runs the uv tool
  (`~/.local/share/uv/tools/convexity`), keys were copied into `config.json`,
  and an `open -a` launch reported v1.14.0, ML model loaded, both keys set,
  10 portfolios. The `pt` env is still on disk, unused by the app.
- Post-commit verification (same day) found three Windows PowerShell 5.1
  problems in `install.ps1` that pwsh 7 hides — the README one-liner runs 5.1:
  redirected native stderr aborting the install under `Stop`, TLS 1.2 for the
  GitHub API, and `-Source .` producing an empty URL (a `[Uri]` cast
  precedence bug — it would have failed the Windows CI job). Fixed; the CI job
  now runs the installer under 5.1. Details in CLAUDE.md §14.
- Gotcha while testing: a tarball made with macOS `tar` carries `._*`
  AppleDouble entries, so uv saw two top-level entries, did not strip the
  directory and failed with "does not appear to be a Python project" — and
  cached that by URL. Use `COPYFILE_DISABLE=1 tar --no-xattrs` and clear
  `~/.cache/uv/sdists-*/url` when simulating a release archive.

- [x] `install.sh` (macOS/Linux):
  1. install uv if missing (official installer, `curl -LsSf https://astral.sh/uv/install.sh | sh`);
  2. `uv tool install "convexity[desktop] @ <release tag's source archive>"`
     (pinned to the latest release tag, not `main` — see above for why not
     `git+https`);
  3. build `Convexity.app` whose launcher execs the `convexity-app` command;
     generate `icon.icns` as today (icon is shipped as package data).
- [x] `install.sh` (macOS): check for Homebrew `libomp` before installing
      (the PyPI lightgbm wheel needs it; found in Phase 2) and tell the user to
      `brew install libomp`, or offer to run it, when it is missing.
- [x] `install.ps1`: same with the PowerShell uv installer, shortcuts point at
      `convexity-app.exe`. Keep `Assert-Success` after every native call.
- [x] `update.sh` / `update.ps1` → reinstall at the newest tag + rebuild
      launcher (they re-run the installer).
- [x] `Launch Dashboard.command` → runs `uv run convexity` (dev checkout).
- [x] Delete `requirements.txt`, `environment.yml`; remove Miniforge code.
- [x] CI: keep the icon/shortcut smoke jobs (now inside full-installer jobs);
      add a job that runs `uv tool install .` then a boot-and-health check.
- [x] CLAUDE.md §3/§14: `pt` env retired; dev workflow is `uv sync` / `uv run`.
- [ ] Manual verification on a clean macOS user account (System Settings →
      Users & Groups → new user) — the only realistic "new user" test.
      **Left for you** (creating a macOS user is a system-settings change);
      do it after v1.14.0 is released, with the one-line command from the
      README. The empty-`HOME` simulation above is the closest stand-in.

Done when: one command on a clean account gives a working app.

---

## Phase 5 — Model download on first run (v1.14.1)

Goal: a fresh install has a working Market read. Depends on: Phase 3.

**Done 2026-09-27** (v1.14.1 — your call, rather than the 1.15.0 planned
here) — committed on the `distribution` branch. The asset is published as the
release `model-mlsent-v1.1` (targeting `main`, marked not-latest so the
installer's `releases/latest` lookup still finds the app release). Downloaded
back from GitHub: same SHA-256, and a real `python -m convexity.model_fetch`
into a temp data folder installed it through GitHub's CDN redirect, byte-identical.
Deviations and findings:
- The tarball is built **deterministically** (Python `tarfile`: sorted names,
  mtime 0, uid 0, gzip mtime 0) by `model_fetch.pack()`, shared by the export
  script and the tests. Packing the same six files twice gives the same
  SHA-256, so the pinned hash can be re-derived from the model at any time.
  macOS `tar` would have added `._*` entries, which the extractor rejects.
- Built from a copy of the deployed model (1,411,751 bytes, sha256
  `9e05d4af…fecf46d`); all six files checked for home paths / usernames first.
  The installed files are byte-identical to the source.
- Stricter than "reject absolute / `..`": an allowlist — only the six artifact
  files under `mlsent-v1.1/`, no links, no devices, no extra files or dirs, all
  six required, size caps on download and extraction. Extraction goes to a
  staging dir and is moved into place with one `os.rename`.
- `CONVEXITY_MODEL_URL` overrides the URL only (loopback http allowed, for
  testing); the SHA-256 has no override. `CONVEXITY_MODEL_DOWNLOAD=0` disables
  the automatic start (tests set it); Retry ignores it.
- Race found in the first e2e run: with a fast server the boot warm-up and the
  post-download reload both loaded the model. `reload()` now takes the model
  lock first and keeps an already-loaded model, and the warm-up skips its load
  while a download is running (no misleading "missing artifact" line at boot).
- Settings: while downloading, the pill reads "Downloading" and the pane polls
  every 1.5 s (timer cleared with the log poller's rule); Retry appears whenever
  no model is on disk and nothing is running. A missing-model reason in the
  Track record names the download failure.
- Verified end to end (fresh `CONVEXITY_HOME`, local HTTP server, real app
  boot, headless Chrome at DPR 1): happy path (boot banner before the download
  finished, then "Market read available"); offline (3 attempts, one FAILED
  line, nothing left behind, Failed + Retry in Settings, Retry → progress →
  Running); tampered tarball (checksum mismatch, discarded, Retry with the good
  file → Running). Path traversal and the other unsafe archives are unit tests
  (they need a matching hash).
- Gap found by `/verify` after the release and fixed: "present" meant "the
  folder exists", so an emptied or half-copied `models/mlsent-v1.1/` blocked
  the download, the Retry button and gave a raw `FileNotFoundError`. Present
  now means all six files; an incomplete folder is moved aside (kept, never
  deleted, only once a verified download is ready) and replaced. Verified in
  the running app: half-empty folder at boot → set aside with its file intact,
  one GET, Market read running; empty folder with auto-download off →
  "(incomplete)" + Retry in Settings → click → installed.
- Gotcha hit again: iCloud re-flagged ~17.7k `.venv` files `hidden`
  (`No module named 'convexity'`); `chflags -R nohidden .venv` before runs.

- [x] Package the model: `ml/scripts/10_export_artifact.py --tarball` →
      `mlsent-v1.1.tar.gz` (the six artifact files). Also `--from <dir>` to pack
      an existing bundle.
- [x] Upload it as an asset to a GitHub Release (`model-mlsent-v1.1`).
- [x] `convexity/model_fetch.py`: if `models/mlsent-v1.1/` is missing, download
      the asset from a URL **pinned in code**, verify against a **SHA-256
      pinned in code**, extract safely (reject absolute paths / `..`), then
      reload. Stdlib only. Runs in the background; never blocks startup.
- [x] Settings → Models & Data: status ("downloading / installed / failed:
      reason") and a "Retry download" button.
- [x] Failure is visible, not silent (same rule as `_warn_ml_once`).
- [x] Tests: checksum mismatch rejected, path traversal rejected, offline
      degrades cleanly (local HTTP server in tests, no real network).
      `tests/test_model_fetch.py`, 33 tests.
- [x] Document the model-release procedure in CLAUDE.md §4 (retrain → new
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
