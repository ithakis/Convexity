# Optimizer (MPT)

[← CLAUDE.md](../../CLAUDE.md) · part of the engineering notes; `§N` references name the original CLAUDE.md sections (map in CLAUDE.md).

## 12. `mpt.py` — Black-Litterman + mean-CVaR optimizer

Standalone module (dependency-light, testable in isolation) implementing the two
engines behind the Optimize tab. Used by `compute_efficient_frontier` in
`src/convexity/frontier.py`. **No scipy on the hot path** — the LP solver is a
custom numba interior-point method; scipy/HiGHS lives only in
`tests/_cvar_reference.py` and certifies the fast solver to 1e-6 in CI.

The Markowitz mean-variance path (CLA, tangency, vol/return Monte-Carlo cloud,
`annualize`) was fully removed in v1.8.0 — do not resurrect it.

### Public API

```python
mpt.compute_returns(closes_df, freq="daily") -> returns_df
mpt.annualized_cov(returns_df, freq, model) -> cov_df           # sample / ledoit / ewma
mpt.ledoit_wolf_shrink(cov, returns=None) -> cov_df             # toward constant-corr target
mpt.black_litterman(symbols, cov, mkt_weights, views, *, rf, tau, haircut, ...) -> dict
    # -> {mu, prior, q, delta, tau, haircut, viewed, no_view}  (μ = total annual return)
mpt.mean_cvar_frontier(returns_df, mu, *, alpha, w_min, w_max, fully_invested, rf,
                       n_points, cov) -> {ok, frontier, min_cvar, max_ret, n_nonconv, _ctx}
    # w_min/w_max are scalar OR per-asset vectors (per-position box); _as_bound_vec
    # coerces. each point: {ret, cvar (√252-annualized, ORDERING only), var30, cvar30
    #   (10d→30d display loss), vol, mdd, cdar, weights, _t}
    # _ctx = {a_ret,l,h,kappa,fi,targets} — reused by bootstrap_cvar (strip before JSON)
mpt.bootstrap_cvar(R, ctx, seeds) -> cvar_ann[B,K]   # parallel (prange) frontier-stability
    # band: each seed resamples the T scenarios w/ replacement + re-solves at every target
mpt.portfolio_risk_metrics(weights, mu, cov, returns, alpha, rf, fully_invested) -> {...}
mpt.cvar_return_cloud(returns_df, mu, alpha, n, seed, w_min, w_max, fully_invested, rf)
    # -> [[cvar_30day, ret_ann], ...]  # prange
    # MUST receive the SAME constraints the frontier was solved under (frontier.py
    # passes its own lo_b/hi_b/fully_invested/rf). The cloud is read as the
    # achievable set, so the frontier has to be its upper-left envelope.
mpt.cvar_of(port_ret, alpha) / max_drawdown(port_ret) / cdar(port_ret, beta)
mpt.overlapping_h_returns(port_ret, h) -> overlapping h-day COMPOUNDED returns (len T−h+1)
mpt.var_cvar_horizon(port_ret, alpha, h_base=10, h_target=30) -> (var30, cvar30)
mpt.asset_risk_stats(returns_df, alpha) -> {sym: {ret_ann, ret_total, var30, cvar30}}
```

### The cloud and the frontier must share one feasible set (v1.11.3)
`cvar_return_cloud` sampled the bare simplex while `mean_cvar_frontier` solved
inside the per-position box, so with limits set the scatter was drawn from a
*different, larger* set than the frontier. Measured on the reported book (mins
summing to 80% across four names): **52.7% of 20 000 cloud points sat at a lower
CVaR than the frontier's own min-CVaR portfolio**, the frontier floated
mid-cloud instead of hugging its edge, and the axis stretched to a risk level
nothing feasible could reach (cloud 10.6–35.8% vs frontier 20.4–25.7%). After
passing the box through: cloud 20.2–25.7%, 0.6% left-of-frontier. The residual
is the **daily-vs-30-day estimator gap** (the LP minimises daily CVaR, the axis
shows the 10d→30d one), not a sampling error — the *unconstrained* control's
min sits 3.6% below its own frontier, relatively worse.

Sampling is `w = w_min + free_budget × Dirichlet`, then `_project_into_box`
water-fills the overflow into remaining headroom. **Not rejection sampling** —
with a tight box the acceptance rate collapses and the cloud never fills. With
no box, `free_budget = 1` and the mixture passes through untouched, so the
unconstrained cloud stays bit-identical (asserted in the scratch harness by
comparing a default call against an explicit `w_min=0, w_max=1` call).

Two frontend consequences, both in `mptComputeProj`:
- **The rf floor is bounded.** `yMin = min(yMin, rf)` alone spent ~85% of the
  height on empty space once every feasible portfolio sat between 25% and 29%
  return against a 4.5% rf. rf may now pull the floor down by at most half the
  data's own span; past that the line simply isn't drawn (`drawAxes` already
  guards on the domain). Wide unconstrained runs still show it, unchanged.
- **The streaming `done` handler always re-measures** (`mptRender()`), never
  reusing `_streamProj`. That domain is fixed from the frontier before any cloud
  point exists and is wrong in *both* directions — too small clips the scatter
  flat against the edge, too large strands the plot in an empty frame. Verifying
  it instead needs a magic "close enough" threshold; one honest re-measure of a
  finished cloud does not. `cloudRoom`'s reservation is correspondingly modest
  (1.25×) since it is now only a transient frame.

### Displayed tail risk — 10d→30d (FRTB-style), v1.9
The optimizer still minimises **daily** CVaR (the LP is unchanged). What the UI
*shows* is a separate, more interpretable estimator: empirical VaR **and** CVaR on
**overlapping 10-day compounded returns** (FRTB liquidity-horizon convention),
scaled 10→30d by **√3** (`LIQ_HORIZON`/`DISP_HORIZON` in mpt.py — the single place
to retune). Confidence follows the α slider. This replaced the old `×√252`
annualized CVaR on the x-axis, which routinely exceeded 100% and wasn't a real
loss number. Frontier points therefore carry BOTH `cvar` (daily×√252, used only to
keep the solver's ordering) and `var30`/`cvar30` (display). **The efficient
envelope is cleaned on `cvar30`** so the plotted line is monotone on the axis the
user actually sees. The bootstrap band stays daily (i.i.d. resampling destroys the
time-ordering the overlapping estimator needs) and is transferred onto the 30-day
value multiplicatively — `cvar30_{lo,hi} = cvar30·(band_{lo,hi}/band_med)` — the one
deliberate approximation. `app.js` reads everything through `mptCvar/mptVar/
mptCvarLo/mptCvarHi`, which fall back to the legacy annualized `cvar` for restored
pre-1.9 runs; the axis + stats rows then self-label "annualized · legacy".

**8-core (`prange`).** `_cloud_kernel` and `_bootstrap_cvar` are `@njit(parallel=True)`;
each `prange` iteration keeps its scratch buffers thread-local (bugs here = silent
races). `_bootstrap_cvar` seeds per-replica RNG (`np.random.seed(seeds[b])`) so the
band is deterministic regardless of thread count. `_warm_jit()` (import-time) and a
background thread in `server.main()` pre-compile so the first Optimize click never
pays the ~7 s JIT cost.

**Streaming orchestration (`frontier.py`).** `compute_efficient_frontier_stream(...)`
is a generator yielding `{"type":"progress"|"done"|"error"}`; `compute_efficient_frontier`
is a thin blocking wrapper that drains it (tests + non-streaming callers; pass
`max_seconds=0` to skip the wall-clock fill and run only the minimum). Budgets are
**wall-clock targets** (`_BUDGETS` light/standard/dense ≈ 5/15/60 s): the fixed-size
cloud is drawn first, then the bootstrap band runs until `t_total + target_s`
(`_BOOT_MIN` floor / `_BOOT_CAP` ceiling). pct = elapsed/target, ETA = target−elapsed
(both literally true). The server route stops the compute when the client
disconnects — it stops pulling the generator (`gen.close()`), so cancellation lands
at the next chunk boundary.

### Black-Litterman (return engine)
- Work in **excess-return space** (over rf); add rf back at the end so callers see
  total returns. Prior **Π = δ·Σ·w_mkt** (reverse optimization). δ calibrated from a
  target market risk premium / market variance (`risk_premium / (w_mktᵀΣw_mkt)`,
  clamped, default fallback ~2.5). `w_mkt` = market-cap weights — **caps are
  FX-normalized to USD** first (`frontier._market_cap_weights` →
  `analytics._market_cap_usd`, the same conversion the app's cap-weighting uses;
  yfinance reports marketCap in native currency and a ¥ cap is ~150× a $ cap
  numerically). A cap with no FX rate counts as 0, never at the wrong scale.
- Views: P = identity rows for assets with a valid analyst target;
  q_i = target/price − 1 + dy_i − rf. The forward dividend yield dy_i (row
  `dividend_yield`) is added since 2026-09-29: a price target is a price, while
  Π is a total-return premium, so without it every payer's view was too low by
  its yield. Ω diagonal, per-asset confidence from analyst count
  (`n/(n+k0)`) × dispersion (`1/(1+((hi-lo)/tgt)/d0)`). The **analyst-trust haircut**
  H ∈ [0,1] scales Ω by `(1-H)/H`: H→0 ⇒ Ω→∞ ⇒ posterior→prior; H→1 ⇒ Ω→0 ⇒
  posterior→views. Posterior μ_BL = Π + τΣPᵀ(PτΣPᵀ+Ω)⁻¹(Q−PΠ), k×k inverse (k≤N).
- Posterior covariance is **not** used for risk — risk is empirical CVaR.
- **Ledoit-Wolf** (`ledoit_wolf_shrink`) is the full 2004 constant-correlation
  estimator: δ = max(0, min(1, (π − ρ)/γ / T)). Before 2026-09-29 the ρ term
  (the target's own estimation noise) was left out, which overstated δ.
  `tests/test_finance_math.py` checks it against a line-by-line transcription of
  the authors' `covCor.m`.

### Mean-CVaR frontier (risk engine)
- Scenarios = **daily** FX-adjusted returns (~750/3Y). CVaR_α is optimized in daily
  units. The `cvar` field is the legacy ×√252 annualization, now kept only as the
  solver's ordering key — **what is plotted/reported is `cvar30`/`var30`** (see
  "Displayed tail risk" above).
- Frontier: sweep the return floor from the min-CVaR portfolio to the max-return
  portfolio (closed-form `_max_return_weights`), solving the Rockafellar-Uryasev LP
  at each. Constraints: long-only box `[w_min, w_max]`, and `Σw=1` (fully invested)
  or `Σw≤1` (cash allowed, remainder credited at rf via μ_ex = μ−rf).

### Solver — numba primal-dual interior-point (`_cvar_pdip`)
- Mehrotra predictor-corrector on the RU LP. Variables (w, ζ, u∈R^T). The u-block of
  the Newton system is diagonal, so u is eliminated by a Schur complement each
  iteration → a dense **(N+1)×(N+1)** solve (+1 border row for Σw=1). T-sized work
  stays as two matmuls. Balanced dual start (λ·s ≈ 1) → ~20–25 iters; cap 60 (cash
  degeneracies need more). Convergence gap 1e-9 (far past the 1e-6 accuracy bar).
- `mean_cvar_frontier` returns `n_nonconv`; frontier.py turns it into a warning.
- Certified vs `tests/_cvar_reference.py` (scipy HiGHS): |ΔCVaR|<1e-6 over hundreds
  of random problems. **Re-run `pytest tests/test_metrics.py -k cvar_pdip` after any
  solver edit.**

### Performance
- Data fetch dominates the *base* result (cold ~1–5 s); the base optimization math is
  ~40–150 ms (N≤30). The **budget itself is intentional wall-clock spend** on the
  bootstrap band (~5/15/60 s), all on `prange`, and is streamed with a real ETA — so
  "slow" here is the user's dial, not a regression. Cloud (fixed 20k/40k/80k) ~0.3–1.5 s.
- Reuse: `analytics._bulk_close` (price history), `fx._apply_fx_to_closes` (currency),
  `frontier._risk_free_history` / `/api/risk-free-history` (rf sparkline).

### `/api/efficient-frontier` params (`frontier.py`)
`_MPT_LOOKBACK_YF = {"1Y":"1y","3Y":"3y","5Y":"5y","10Y":"10y"}`. Request fields:
`lookback, display_ccy, rf, alpha, fully_invested, bounds ({sym:{min,max}} fractions),
cov_model, haircut, budget (light/standard/dense wall-clock tiers), current_weights`.
Legacy scalar `w_min`/`w_max` and `cloud_budget` are still accepted as fallbacks.
Frequency is fixed **daily**. Response frontier points carry `cvar_lo/cvar_med/cvar_hi`
(bootstrap band) plus `cvar30_lo/med/hi`; `meta.n_boot` is the achieved replica count.

**Streaming message order (v1.9):** the route is message-type-agnostic (it JSON-writes
every yielded dict), and the generator now emits
`progress…` → **`frontier`** (frontier + anchors + bl + params, so the client can fix
the axis domain and draw the line immediately) → **`cloud`** chunks (≤5000 `[cvar30,
ret]` pairs each — the client paints them additively so the scatter visibly fills)
→ `done`. **`done.cloud` is deliberately `[]`** — the cloud already went out in the
chunks; re-shipping 20-80k points would double the payload. The blocking wrapper
`compute_efficient_frontier` reassembles the chunks into `result["cloud"]` so tests
and non-streaming callers are unaffected. `done` also carries `asset_stats` and
`analyst_detail` (the per-company hover data).

### Saved run (`<data>/state/mpt.json`) — last 3 runs per portfolio
```jsonc
{ "runs": { "<portfolio name>": [          // newest-first LIST, capped at 3
    { "id": "run_...", "saved_at": "...",
      "params": {lookback, alpha, rf, fully_invested, cov_model, haircut, budget, bounds, display_ccy},
      "symbols": [...], "missing": [...],
      "frontier": [{ret, cvar, var30, cvar30, cvar30_lo/med/hi, cvar_lo/med/hi, vol, mdd, cdar, weights}, ...],
      "min_cvar": {...}, "max_ret": {...},
      "anchors": {equal, cap, current}, "bl": {...}, "meta": {...},
      "asset_stats": {...}, "analyst_detail": {...},
      "cloud": [[cvar30, ret], ...] },   // DOWNSAMPLED to ~2.5k client-side
    ...] } }
```
`save_mpt_run` unshifts and truncates to `_MPT_MAX_RUNS = 3`; a run whose `params`
equal the newest entry's **replaces** it (dedupe) rather than duplicating.
`get_mpt_runs` returns the list, `get_last_mpt_run` its head; `_as_run_list`
tolerates both legacy formats (bare dict, or an older list). Rename/delete cascades
move the whole value and are format-agnostic. The cloud **is** persisted now (it was
previously stripped and never re-sampled, so restored runs rendered an empty
scatter) — downsampled to ~2.5k points so 3 runs stay small on disk.
`save_mpt_run` overwrites (single run); `get_last_mpt_run` reads it (tolerates the
legacy list format → newest entry). Cascades on view rename/delete. The heavy `cloud`
is stripped client-side before saving and re-sampled on load.
