# CI and quality gates

[← CLAUDE.md](../../CLAUDE.md) · part of the engineering notes; `§N` references name the original CLAUDE.md sections (map in CLAUDE.md).

## 13. CI / Quality gates

### GitHub Actions (`.github/workflows/ci.yml`)

Runs on every push to `main` or `claude/**` branches and on every PR to
`main`. Python jobs install through `astral-sh/setup-uv` (SHA-pinned, cache
on) and `uv sync --locked` from `uv.lock`; `server-smoke` uses the base deps,
`desktop-import-smoke` adds `--extra desktop`. The three installer jobs
instead do what a user's machine does: `uv tool install`, which resolves from
pyproject's ranges, not the lockfile (§4). Eight jobs — the
first two gate the rest (`needs: [lint, test]`), so a trivial syntax error
fails in seconds instead of waiting on the platform-specific jobs first:

| Job | Runner | What it proves |
|---|---|---|
| `secrets` | ubuntu | gitleaks (checksum-pinned binary) over the **full history**, plus a filename check that no runtime-state / key / `settings.local.json` file exists in any commit — §18. Independent of the others so a leak fails fast |
| `lint` | ubuntu | Every `.py` parses (`scripts/check_syntax.py`); `ruff check .` and `ruff format --check .` (**blocking**, ruff pinned to the uv.lock version); dependency manifests cover `envcheck.REQUIRED`; `uv lock --check` (lockfile matches pyproject); `packaging/install.ps1`/`update.ps1` parse via PowerShell Core's own `Parser.ParseFile`; `install.sh`/`packaging/update.sh`/`packaging/Launch Dashboard.command` pass `bash -n`. Runs `uv run --no-project` — no dependency install |
| `test` | ubuntu | `uv sync --locked --extra dev` then `uv run pytest tests/` — the full unit suite |
| `server-smoke` | ubuntu | Real HTTP requests against a real running server (`scripts/smoke_test_server.py`) — `/`, `/api/watchlists`, `/api/views`, `/static/*` must return real 200s with real bodies. This is the answer to "is the app actually working," not just "does it import." |
| `desktop-import-smoke` | ubuntu | `convexity.desktop` imports cleanly under a real (headless, `QT_QPA_PLATFORM=offscreen`) `QApplication` — catches PySide6/QtWebEngine API breakage the plain lint job can't see, since lint never installs PySide6. Needs a handful of system graphics libraries (`libegl1`, `libgl1`, etc.) installed via `apt-get` first — the bare runner has none, not even for the offscreen platform plugin |
| `tool-install-smoke` | ubuntu | `uv tool install .`, envcheck with the tool's interpreter, then boots the **installed** `convexity` from a directory outside the checkout and asserts `/api/health` reports this `__version__` and `data_dir == CONVEXITY_HOME` — the package (static files, icon, lexicon) works without a checkout |
| `macos-install-smoke` | **macos-latest** | The whole `install.sh` against the checkout (`CONVEXITY_SOURCE`) into throwaway dirs (`INSTALL_APPS_DIR`, no Desktop shortcut, stdin `/dev/null` so the libomp prompt answers no): real `sips`/`iconutil`, then asserts the bundle's `.icns`, the launcher's exec target and `CFBundleShortVersionString` |
| `windows-install-smoke` | **windows-latest** | The whole `packaging/install.ps1 -Source .` on a disposable runner — uv tool install, envcheck, the `uv run --with pillow` `.ico`, and the **real** Start Menu + Desktop shortcuts, re-read through `WScript.Shell` (TargetPath = the tool's `convexity-app.exe`, IconLocation = the `.ico`). `WScript.Shell` has no equivalent on macOS/Linux, not even under PowerShell Core |

Until v1.14 the two platform jobs ran only the icon/shortcut commands: the
conda installers cost 10-15 minutes per platform per push, mostly re-testing
conda-forge's solver. The uv installers take a couple of minutes, so CI now
runs them end to end. What CI still cannot show is a **clean machine**: the
runners have git, Homebrew and preinstalled Pythons. The closest local check is
an empty `HOME` and a minimal `PATH` with the installer piped in, as a
`curl | bash` user would run it:
`cat install.sh | env -i HOME=<tmp>/home PATH=/usr/bin:/bin CONVEXITY_SOURCE=<checkout> INSTALL_APPS_DIR=<tmp>/Apps CONVEXITY_HOME=<tmp>/data bash`
— that exercises the official uv installer and a managed Python download too.

**ruff replaced pyflakes in Phase 7** and is blocking: `ruff check` (rules
in `[tool.ruff.lint]` — ruff's default pyflakes + pycodestyle-error set, E741
off for the LP's `l`/`h` bounds, E402 allowed in `server.py` and tests) and
`ruff format --check` (line length 100). Run `uv run ruff format .` before
committing; the formatting commit is listed in `.git-blame-ignore-revs`. The
CI pin (`uvx ruff@<ver>`) must match the ruff version in `uv.lock` — bump both
together, or CI and local formatting can disagree.

**To add a check:** add a step to `ci.yml` without `|| true` — the next push
will enforce it.

### Reference pack workflow (`.github/workflows/reference-pack.yml`, Phase 8)

Builds the daily reference pack (news.md, "Reference pack") and replaces the
three assets of the rolling release `reference-pack`. `schedule` 22:30 UTC
Monday–Friday (after the US close) plus `workflow_dispatch` with
`mode: yahoo-check | build`. **Scheduled runs only fire on the default
branch**, so it goes live with the v2.0.0 merge.

| Job | Permissions / secrets | What it does |
|---|---|---|
| `yahoo-check` | `contents: read`, none | Dispatch only: `convexity build-reference-pack --returns-only` — a year of closes for all ~500 names + SPY, plus yfinance news on 20 names; fails under 90% coverage. Run this first: Yahoo sometimes blocks cloud IPs |
| `build` | `contents: read`; `FINNHUB_API_KEY` on the Build step only | `uv sync --locked` (the same commit as the app), `gh release download reference-pack` into `prev/` (absent on the first run), `build-reference-pack --out pack --previous prev` with `CONVEXITY_HOME=$RUNNER_TEMP/…`, upload `pack/` as a 7-day artifact. ~15 min: bound by the yfinance-news limiter (40/min) and Finnhub (55/min) |
| `publish` | `contents: write`, none | Download the artifact, re-verify the three files against the manifest, fail if the release does not exist (it **never** creates it), then `gh release upload reference-pack … --clobber` — data files first, manifest last. Never commits |

`tests/test_workflows.py` pins these rules (SHA pins with a tag comment on
every workflow, read-only default token, only `publish` writes, the secret
only in `build`, no `git commit`/`push`/`gh release create`). PyYAML is not a
dependency, so the checks are textual; `actionlint` (Homebrew) is the
stronger local check.

First run, after the branch is pushed (each step needs the owner):
1. Create the release once — a public release with no assets, marked
   **pre-release and not latest**:
   `gh release create reference-pack --title "Reference pack (rolling)" --notes "…" --prerelease --latest=false`.
   `install.sh` / `install.ps1` install whatever `/releases/latest` returns;
   were this release ever "Latest", new installs would try to install a data
   tag. A pre-release can never be "Latest", and `--clobber` uploads do not
   change that (the model's `model-*` release is already not-latest).
2. Confirm the `FINNHUB_API_KEY` Actions secret is the **separate** free key.
3. Actions → Reference pack → Run workflow, branch `distribution`,
   mode `yahoo-check`; then mode `build`, and check the three assets, their
   sizes and `manifest.json` (date, `sources`, `rows`).
4. Failed runs email the owner (GitHub's default); the app shows the pack's
   age in Settings → Models & Data and stops using it after 14 days.

`server-smoke` / `tool-install-smoke` boot the app, whose warm-up calls
`reference_pack.start()`: until the release exists that is one 404, logged
and ignored.

### Pre-commit hooks (`.claude/settings.json`)

Two hooks fire on every `git commit` inside a Claude Code session:

1. **Syntax check** (`command` hook, hard gate) — runs `python -c "import ast;
   ast.parse(open(f).read())"` on every staged `.py` file. Exits non-zero
   (blocks the commit) on any syntax error.

2. **AI code review** (`agent` hook, Haiku model) — runs `git diff --cached`,
   reviews for critical issues only (runtime exceptions, accidental secrets,
   broken cross-references). Blocks on genuine problems; passes silently on
   style/TODOs. Timeout 90 s.

To **evolve** the pre-commit checks as the project grows:
- Add new file types to the syntax-check step (e.g., `grep '\.js$'` for
  external JS if the app ever gains a separate JS bundle).
- Tighten the AI reviewer prompt in `.claude/settings.json` — e.g., add
  "also check that any new API route has a matching DELETE handler" once that
  pattern is established.
- Wire pytest once tests exist: add a third command hook that runs
  `python -m pytest tests/ -q` after the syntax gate.

### Manual review before committing

Run `/review` (or `/security-review`) at any point during a session to get a
full structured review of all staged changes. This is separate from the
pre-commit hook and can be used to catch architectural issues early.

### Merging PRs without leaving Claude Code

```bash
# Squash-merge (recommended — keeps main history linear)
gh pr merge <number> --squash --delete-branch

# Auto-merge once CI passes
gh pr merge <number> --squash --auto --delete-branch

# Check PR status
gh pr status
gh pr checks <number>
```

**After a merge, make the new version the live one — this does not happen
automatically.** PR work happens in a worktree (`.claude/worktrees/...`);
`gh pr merge` updates `origin/main` on GitHub, but neither the user's main
checkout at the repo root nor their installed app picks that up by itself.
Steps, all required:

1. **Pull the main checkout.** `git -C <repo root> pull origin main` (or
   `git status --branch` first to confirm it's actually behind — merging
   from a worktree never touches the root checkout's working tree).
2. **Tag + release** when the merge bumped the version (§15) — the installer
   installs the latest *release*, so an untagged version cannot reach the
   user's app.
3. **Reinstall + relaunch the app.** The installed app is a uv tool, not the
   checkout (§3): run `./install.sh` (latest release) and ask the user to
   quit and reopen Convexity. A long-lived process keeps serving the code it
   loaded at start — Python doesn't hot-reload, so an old process keeps
   reporting the old `__version__` via `/api/health` indefinitely.

Verify with `curl -s http://127.0.0.1:8765/api/health` and confirm
`version` matches the just-merged `__version__` before telling the user
the update is live.
