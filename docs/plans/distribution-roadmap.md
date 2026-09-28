# Distribution roadmap: from "clone + conda" to an installable app

Status: **Phases 0–7 done** on the local `distribution` branch (code at
1.15.0, 2026-09-28); written 2026-09-26, v1.13.0. One phase = one Claude Code
session. Do them in order; each phase lists what it depends on.

**Release plan (decided 2026-09-28): one big v2.0.0.** Nothing after v1.13.0
has been pushed or released — 1.14.x/1.15.0 exist only as commits on the local
`distribution` branch. The phases below, then the v2 features (to be listed
by the user), all land on `distribution`; at the end: one version bump to
2.0.0, the 1.14–1.15 CHANGELOG sections folded into a single 2.0.0 section
(no tag ever pointed at them), one PR to `main`, full CI, tag + GitHub release
per CLAUDE.md §15. Until then: **no push, no PR, no tags, no version bumps.**
One repository only: everything — app, installers, the reference-pack
workflow (Phase 8) and the website (Phase 9) — lives in `ithakis/Convexity`.
Consequence to keep in mind: CI's macOS/Windows installer jobs only run on
GitHub, so each phase runs their local equivalents (CLAUDE.md §13) instead.

How to run a phase with Claude Code:

```
Read docs/plans/distribution-roadmap.md and implement Phase N.
Follow CLAUDE.md (§18 security rules). Commit to the current branch
(distribution): no new branch, no push, no PR, no version bump. Tick the
phase's checklist in this file and add your findings.
```

Rules that apply to every phase:
- Commit to `distribution`; run the full local gate (pytest, check_syntax,
  `uv lock --check`, smoke_test_server, local install smoke) before each
  commit. The version stays at 1.15.0 until the v2.0.0 release (ask before
  bumping, CLAUDE.md §15); release notes go under "Unreleased" in CHANGELOG.md.
- Update CLAUDE.md in the same PR whenever a phase changes an established
  pattern (paths, env, install, dependency manifests).
- Never commit runtime state, keys, the model's training data or absolute home
  paths (§18).
- After the v2.0.0 merge: pull the main checkout, reinstall the app, verify
  `/api/health` reports 2.0.0.

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
- Follow-ups fixed 2026-09-27 (after Phase 5): migration conflicts are shown
  (banner + Settings → About, via `/api/health` `migration_conflicts`), the
  port probe matches the server's bind so a restart keeps 8765, and the dev
  venv in iCloud-synced `~/Documents` is `.venv.nosync` behind a `.venv`
  symlink (root cause and test in CLAUDE.md §3).
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

Depends on: Phase 3. Start from `distribution` at v1.14.2 or later — it
already has the pieces below; build on them rather than re-creating them:
- Key order is fixed in `helpers._load_local_secret`: env var → `config.json`
  (`finnhub_api_key`, `nvidia_api_key`; `helpers._CONFIG_KEYS`) → legacy
  `.finnhub_key` / `.nvidia_key` (logged fallback). `POST /api/keys` writes
  those same two names, via `paths.config_file()`; don't invent a new file or
  new key names.
- A malformed `config.json` is logged once (`helpers._CONFIG_WARNED`). The
  no-restart reload must reset that flag, or a fixed file stays "ignored" in
  the log.
- Banners follow one pattern: a field on `/api/health`, a `sync…Banner()` in
  `app.js` built on `.env-banner`, and a **Details** button that opens the
  right Settings section. See `syncMigrationBanner` (1.14.2) and
  `syncLlmBanner`. The first-run keys banner should be the same shape and go
  to the new API-keys section.
- Settings → About already lists `config.json` under "Where your data lives".
- Test runs: `tests/conftest.py` gives every test its own `CONVEXITY_HOME`, so
  a test can write `config.json` freely. Manual runs: `CONVEXITY_HOME=$(mktemp -d)`,
  never the real data folder (CLAUDE.md §3).

- [x] Settings → "API keys" section (add it to `SETTINGS_SECTIONS`): Finnhub and
      NVIDIA NIM fields, masked, with "Test" buttons (one cheap call each) and
      links to where to get a free key.
- [x] `POST /api/keys` writes `config.json` (file mode 0600) — the key is never
      echoed back; `GET` returns booleans only (same as `/api/runtime-status`).
- [x] Keys reload **without a restart** (today they're read once at import —
      add a reload hook in `news_sentiment` and `finnhub_adapter`).
- [x] First-run banner: "Add your free API keys to enable News" when missing.
- [x] Tests: key never appears in any response or log line.

Findings (2026-09-28, implemented on `distribution` after v1.14.2):
- New `convexity/keys.py` owns the write/clear/reload/test logic; the server
  routes are thin. `helpers._resolve_secret` now returns `(value, source)` so
  Settings can say *where* a key comes from (env / config.json / legacy file)
  without ever returning it; `_load_local_secret` is unchanged for callers.
- `POST /api/keys` takes an explicit `action: "set"|"clear"` — with a
  "key present means set, empty means clear" shape, an accidental empty submit
  would have deleted a working key.
- A malformed `config.json` is refused with 409 and left byte-identical; while it
  is broken the reader ignores it, so both keys show "Not set" alongside a red
  "fix or delete <path>" line. Saving again after the fix works (the one-time
  warnings are re-armed by every reload).
- Reload has three parts beyond re-reading the globals, each found by reading
  the call paths: the lazy OpenAI client had captured the old key; a persisted
  "key rejected" LLM status is *permanent* and would have kept short-circuiting
  (and kept its banner up) until the next refresh; and it is persisted, so it
  is re-saved after clearing or a restart would bring it back.
- `/v1/models` on NIM is not auth-gated, so it cannot validate a key; the Test
  button sends a `max_tokens: 1` completion instead. Finnhub's test sends the
  key as `X-Finnhub-Token`, not `?token=`, so it can't surface in a URL.
- Test buttons go through the shared limiters but never wait on them: a full
  minute reports "rate-limited" instead of freezing the click for up to 60 s.
- With the NVIDIA key missing, the LLM banner stands down in favour of the keys
  banner (one cause, one banner, and it leads to where the key is entered).
  The keys banner's dismissal is remembered in localStorage until both keys are
  set.
- The legacy walk-up fallback is untouched (one-release rule). It matters for
  testing: the dev checkout holds real `.finnhub_key` / `.nvidia_key`, so
  `tests/test_keys.py` moves `helpers.__file__` into tmp, and the E2E launcher
  did the same — otherwise a "no keys" run silently loads the real ones.
- Verified end to end (fresh `CONVEXITY_HOME`, headless Chrome over CDP at
  DPR 1, all providers pointed at a local stub — no real Finnhub/NVIDIA call):
  banner on first run → Details opens API keys → empty submit refused in the
  page → a whitespace-padded key saved stripped, double submit wrote once →
  `config.json` mode 600 holding both fields → wrong Finnhub key tests
  "rejected (HTTP 401)", right one "works" → banner gone without reload. A
  news-only refresh with a wrong NVIDIA key produced the "key rejected"
  banner; saving the right key in Settings cleared it at once and the next
  refresh sent the new key to the NIM stub (8 calls), `llm_ok` true, no
  restart. Malformed `config.json` → 409 message, file untouched. Remove both →
  `config.json` is `{}`, banner back; dismiss survives a reload. The sentinel
  never appeared in any response body the page received, `/api/logs`, the
  server's console log, or `localStorage`.
- Real keys, with the user's OK (temp data folder, keys from the checkout's
  legacy files): Test → Finnhub ok (HTTP 200, 456 ms), NIM ok (HTTP 200,
  879 ms). Wrong keys against the real providers: Finnhub 401, NIM **403**
  (not 401), both reported as "rejected" — `_classify_http` treats 401 and
  403 alike for that reason.
- Found in the verification pass and fixed: the key routes accepted a
  cross-site "simple" POST (text/plain needs no CORS preflight), so any web page
  open in the user's browser could have swapped or deleted a key. They now
  require JSON, a loopback Host and a same-origin Origin (403 otherwise; tested
  for text/plain, a foreign Origin, another local port and a rebinding Host).
  Follow-up done the same day: the guard now covers every route (Host check
  on all methods — a rebinding page could otherwise *read* the holdings —
  Origin on POST/DELETE, JSON on every POST); see CLAUDE.md §4 HTTP routes. Also: the LLM's "NVIDIA key missing"
  reason still pointed at hand-editing config.json; it now points at Settings.
- Not done here: `install.sh`'s copy of legacy keys still writes `config.json`
  itself rather than through `keys.py` (same fields and mode). Browsers may
  offer to save a pasted key in their password manager (it is a password
  field); the desktop app's QtWebEngine has no password manager.

---

## Phase 7 — Repository tidy-up (v2.0.0)

Goal: looks and behaves like a maintained product. Can run any time after
Phase 4.

- [x] `src/` layout: move `convexity/` → `src/convexity/`; fix pyproject, CI,
      hooks, tests, CLAUDE.md paths.
- [x] Root contains only: README, LICENSE, CHANGELOG, SECURITY, AGENTS.md,
      CLAUDE.md, pyproject.toml, uv.lock, install.sh, `.github/`, `src/`,
      `tests/`, `docs/`, `ml/`, `scripts/`, `packaging/`, `assets/`.
  - `install.ps1`, `update.*`, `Launch Dashboard.command` → `packaging/`
    (keep `install.sh` at root as the curl target, or add a root stub).
  - `icon.png` → `assets/` (and into package data).
  - `build_symbol_db.py` → `convexity build-symbols` subcommand.
  - `dashboard.py` shim → delete.
- [x] ruff replaces pyflakes (config in pyproject); make lint blocking; run
      `ruff format` in its own commit.
- [x] Split CLAUDE.md: keep a short CLAUDE.md (rules, map, gotchas index) and
      move deep sections to `docs/architecture/*.md` (backend, frontend, news,
      mpt, desktop, jobs, security). Nothing is deleted, only moved and linked.
- [x] README: badges (CI, release, license), screenshot (synthetic portfolio),
      one-line install per OS, "what you need" (free Finnhub + NVIDIA keys),
      uninstall instructions.
- [x] `.github/ISSUE_TEMPLATE/` (bug, feature).
- [ ] Optional, later: split `static/app.js` (~490 KB) into ES modules, no
      build step. (Skipped in Phase 7 by decision.)

Deviations and findings (2026-09-28, eight commits on `distribution` plus this note):
- **src layout.** hatch builds from `src/convexity` and still ships
  `static/`, `assets/icon.png` and `data/lm_lexicon.json` as package data.
  Proof, as CI's `tool-install-smoke` does it: `uv tool install .` from a
  clean copy of the tree into scratch `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`, the
  installed `convexity` booted from outside the checkout with
  `CONVEXITY_HOME=$(mktemp -d)` → `/api/health` version 1.15.0 and the temp
  `data_dir`, `/` and `/static/app.js` 200. `install.sh` end to end into
  throwaway dirs gave a valid `.app` (icns, exec target, bundle version).
  `paths.legacy_root()` and the legacy key walk-up still find the repo root
  (they walk up to `.git`), so no logic changed. pytest gets
  `pythonpath = ["src", "."]`; scripts and `ml/scripts` add `src/` to
  `sys.path` (the lint job runs them with `--no-project`).
- **No `assets/` at the root.** The icon stays only in
  `src/convexity/assets/icon.png` (package data, what the installers read
  through the tool's interpreter); a second copy would drift.
- **The Windows one-liner URL changes** to
  `…/main/packaging/install.ps1`. The old `…/main/install.ps1` 404s once
  v2.0.0 reaches `main`; README, CHANGELOG and the in-app hints say so. A root
  stub was not added (the task said `packaging/`); revisit only if old links
  are known to be in circulation. `packaging/install.ps1` looks for legacy key
  files beside itself and in its parent (the checkout root) — verified with a
  fake key in a scratch copy under pwsh.
- **pwsh on macOS**: `install.ps1` reaches the Windows-only `WScript.Shell`
  step (icon.ico generated) only with a `uv` shim on `PATH` that symlinks
  `Scripts\python.exe` / `convexity-app.exe` *after* `uv tool install`:
  symlinking beforehand is useless, `--force` recreates the tool env. Noted in
  docs/architecture/desktop.md.
- **`convexity build-symbols`** lives in the new `cli.py`, which is now the
  `convexity` entry point (`python -m convexity` too): no arguments runs the
  server exactly as before, the builder's imports stay off the server's start
  path, an unknown command exits 2. Phase 8's `convexity build-reference-pack`
  belongs next to it.
- **ruff**: the default rule set (F + E4/E7/E9) found no pyflakes-class issue
  at all; it found 63 E702 (semicolon statements, all removed by the
  formatter), 12 E402 (deliberate: `server.py`'s warnings filter precedes its
  imports; tests set `sys.path` first → per-file ignores) and 10 E741 (the
  LP's `l`/`h` bounds → ignored, named as in the maths). `ruff format` (line
  length 100) touched 67 files, 643 tests pass unchanged; it is its own commit
  and is listed in `.git-blame-ignore-revs`. CI pins `ruff@0.16.9` to match
  uv.lock — bump both together.
- **CLAUDE.md split**: 2493 → ~530 lines. Kept: intro, §1–3, §9, §10, §15,
  §18, plus a new §0 (section map + linked gotcha index). Moved unchanged:
  §4–8, §11–14, §16–17 to `docs/architecture/{backend,news,security,frontend,
  mpt,ci,desktop,jobs}.md`. `§N` references keep their numbers (the §0 table
  maps them) rather than being rewritten as links — every code comment that
  says "CLAUDE.md §18" etc. stays correct. Checked mechanically: all 2251
  non-empty lines of the old file exist verbatim in the new set; every
  relative link and heading anchor resolves.
- **README**: the existing screenshots already show a synthetic 12-name
  mega-cap book of public tickers, so they were kept rather than regenerated.
  Stale lines fixed along with the architecture section (localStorage
  watchlists, CSV export, "Save Watchlist").

---

## Phase 8 — Reference pack, built in this repo (v2.0.0)

Goal: new and returning users get a calibrated Market read and a meaningful
Track record on day one. Depends on: Phase 5 (model download), Phase 7 (paths).

Design — **one repository**: the collector is a scheduled GitHub Actions
workflow in `ithakis/Convexity`, not a separate repo, and it publishes to a
GitHub release of this repo, not to Hugging Face. (Revised 2026-09-28; the
earlier draft had an `ithakis/Convexity-data` repo and a Hugging Face dataset.)
- `.github/workflows/reference-pack.yml`: `schedule` weekdays after the US
  close, plus `workflow_dispatch`. It runs the `convexity` package **from the
  same commit** (`uv sync --locked`), so the featurizer is identical by
  construction — train/serve parity without pinning a separate dependency.
- Universe: S&P 500, a fixed list committed as package data (public tickers
  only, never the user's book).
- Per run: Finnhub company news for the universe (rate-limited, ~9 min), the
  **Market read only** (the local LightGBM model, downloaded by
  `model_fetch` and SHA-256 checked as in the app — no LLM, no look-ahead),
  forward returns from yfinance joined once known.
- Publishes to a **rolling release** with the fixed tag `reference-pack`
  (separate from the app's `vX.Y.Z` and the model's `model-*` tags), replacing
  its assets each run with `gh release upload --clobber`:
  - `anchor.json.gz` — last 90 days of `market_score` across the universe.
  - `history.json.gz` — records shaped like the local sentiment history.
  - `manifest.json` — date, row counts, model version, schema version, and the
    SHA-256 of the two data files.
  JSON rather than parquet: pyarrow is deliberately not a dependency (it
  switches pandas' string backing, CLAUDE.md §4).
- Published: ticker, date, derived scores, at most a URL. **Never headline or
  article text** (news licensing). Nothing from users is ever uploaded.
- **The workflow never commits.** Data goes only to the release asset, so the
  public git history stays code-only (§18) and the repo doesn't grow daily.
- Secrets: a **separate free Finnhub key** for the workflow, stored as the
  Actions secret `FINNHUB_API_KEY` — sharing the user's own key would split
  its 60/min budget with their running app. No NVIDIA key needed (no LLM).
- Permissions: the repo default stays `contents: read`; only the publish job
  gets `contents: write`. Third-party actions SHA-pinned (§18). Scheduled
  workflows only run on the default branch, so it goes live with the v2.0.0
  merge; test it before that with `workflow_dispatch` — which needs the branch
  pushed, so it is the one thing in this phase that waits for the release.
- Monitoring: GitHub's own failed-run emails to the owner, plus the manifest
  date shown in the app ("reference data is N days old"). No healthchecks.io:
  every new outbound host needs the user's OK (§18), and this adds none.
- Risk to check first: Yahoo sometimes rate-limits or blocks cloud IPs. Before
  building the rest, run the forward-return fetch for the full universe on a
  GitHub runner and confirm it completes.

Why a rolling asset is safe although §18 says "never replace an existing
tag's asset": that rule protects **code** pinned by hash in old installs (the
model). The pack is **data** that changes daily and cannot be pinned. The app
downloads it only from `github.com/ithakis/Convexity/releases/download/reference-pack/`
over HTTPS, checks each file against the manifest's SHA-256 and a size cap,
parses it with `json` only (never pickle, never executed), and validates schema
and model version before use; anything that fails is ignored and logged.

Tasks:
- [ ] Workflow + a `convexity build-reference-pack` subcommand (in `cli.py`,
      next to `build-symbols`) it runs (so it
      can be run and tested locally with `CONVEXITY_HOME=$(mktemp -d)`).
- [ ] App: `src/convexity/reference_pack.py` fetches the pack when older than 24 h,
      verifies manifest hashes + size cap, validates schema + model version,
      parses as data only.
- [ ] Market read anchor: `reference` when local history < 200, `live` after;
      the UI names which one (extends the `anchor` values).
- [ ] Track record: add a "Model (500 names)" view from the pack next to "Your
      holdings" (local).
- [ ] Settings toggle to disable the download (privacy: it only reveals that
      the app is running, not what you hold — say so).
- [ ] Tests with a local HTTP stub only (bad hash, oversize, wrong schema,
      wrong model version, offline) — no real network, as with model_fetch.
- [ ] CLAUDE.md: the rolling-asset exception in §18; the workflow in §13
      (since Phase 7: `docs/architecture/ci.md`).

---

## Phase 9 — Website on GitHub Pages, from this repo (optional, v2.0.0)

- Static site source in `docs/site/` of this repo, deployed by a Pages
  workflow (`actions/deploy-pages`, SHA-pinned; `pages: write` +
  `id-token: write` on that job only) to `ithakis.github.io/Convexity`. No
  second repo, no Cloudflare account. A custom domain is optional and a
  separate decision.
- Plain HTML/CSS, no build step and no third-party scripts or analytics.
  Download buttons link to `github.com/ithakis/Convexity/releases/latest`;
  the install command is picked by the visitor's OS, with the others shown too.
- `/status`: generated **at deploy time** from the reference pack's
  `manifest.json` (the Pages workflow runs after `reference-pack.yml` via
  `workflow_run`), not fetched by the browser — release downloads don't send
  CORS headers.
- Screenshots from a synthetic portfolio of public tickers only (§18).
- Goes live with the v2.0.0 merge (Pages deploys from the default branch).
- 2FA on the GitHub account (the only account involved).

---

## Deliberately out of scope

- Signed/notarized `.app` or PyInstaller bundles (cost, 500 MB+, Gatekeeper).
- Hosting the FNSPID training data (already public upstream; rebuildable).
- Crowdsourcing scores from users.
