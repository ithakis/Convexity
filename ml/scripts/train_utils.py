"""Shared loaders/metrics for 07_train_flaml.py, 08_validate.py, 09_tier_cuts.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ml import config  # noqa: E402


def load_split(split: str, idf, max_rows: int | None = None):
    """Assemble X (tfidf | dense CSR float32), y, weights, meta for one split.
    Shards were written in d1 order, so concatenation preserves time order."""
    import numpy as np
    import pandas as pd
    import scipy.sparse as sp

    from convexity import ml_features as mf

    d = config.FEATURES_DIR / split
    mask_path = config.FEATURES_DIR / "col_mask.npy"
    mask = np.load(mask_path) if mask_path.exists() else None
    Xs, metas = [], []
    n = 0
    for cp in sorted(d.glob("counts_*.npz")):
        i = cp.stem.split("_")[1]
        counts = sp.load_npz(cp)
        dense = np.load(d / f"dense_{i}.npy")
        meta = pd.read_parquet(d / f"meta_{i}.parquet")
        tfidf = mf.apply_idf(counts, idf)
        if mask is not None:
            tfidf = tfidf[:, mask]
        Xs.append(sp.hstack([tfidf, sp.csr_matrix(dense)], format="csr", dtype=np.float32))
        metas.append(meta)
        n += len(meta)
        if max_rows and n >= max_rows:
            break
    X = sp.vstack(Xs, format="csr")
    meta = pd.concat(metas, ignore_index=True)
    if max_rows and len(meta) > max_rows:
        X, meta = X[:max_rows], meta.iloc[:max_rows].reset_index(drop=True)
    y = meta["sar"].to_numpy(dtype="float32")
    w = meta["group_weight"].to_numpy(dtype="float32")
    return X, y, w, meta


def group_spearman(meta, pred) -> float:
    """Rank IC at (symbol, d1, session_class) group level — kills the
    pseudo-replication from same-story articles sharing one label."""
    import numpy as np

    g = meta.assign(pred=pred).groupby(["symbol", "d1", "session_class"], observed=True)
    gp = g.agg(pred=("pred", "mean"), y=("sar", "first"))
    x, y = gp["pred"].to_numpy(), gp["y"].to_numpy()
    if len(x) < 5 or x.std() == 0 or y.std() == 0:
        return float("nan")
    rx = np.argsort(np.argsort(x)).astype("float64")
    ry = np.argsort(np.argsort(y)).astype("float64")
    return float(np.corrcoef(rx, ry)[0, 1])


# ----------------------------------------------------------- v2: date-clustered
def load_panel(columns: list[str] | None = None, where: str = ""):
    """The ticker-day panel (06 --stage panel), sorted by (date, symbol)."""
    import duckdb

    cols = ", ".join(columns) if columns else "*"
    q = f"SELECT {cols} FROM read_parquet('{config.FEATURES_DIR / 'panel.parquet'}')"
    if where:
        q += f" WHERE {where}"
    return duckdb.connect().execute(q + " ORDER BY date, symbol").df()


def daily_ic(dates, score, label, horizon: int = 1, min_names: int = 20) -> dict:
    """Mean of per-date cross-sectional Spearman IC and its t-statistics.

    Each date is ONE observation — pooling ticker-days would treat thousands
    of same-day names as independent draws and overstate t by ~sqrt(names/day).
    `t` is the plain daily-series t; `t_nw` is Newey-West with lag horizon-1,
    because consecutive dates' h-day labels overlap and their ICs are
    autocorrelated (the gate uses t_nw)."""
    import numpy as np
    import pandas as pd

    df = pd.DataFrame({"d": np.asarray(dates), "s": np.asarray(score, "float64"),
                       "y": np.asarray(label, "float64")}).dropna()
    ics = []
    for d, g in df.groupby("d", sort=True):
        if len(g) < min_names or g["s"].nunique() < 3:
            continue
        rs = g["s"].rank().to_numpy()
        ry = g["y"].rank().to_numpy()
        c = np.corrcoef(rs, ry)[0, 1]
        if np.isfinite(c):
            ics.append((d, c))
    # The statistic itself is the app's own (Track record), not a copy.
    from convexity.news_diagnostics import clustered_mean_t

    if len(ics) < 3:
        return {"mean": float("nan"), "t": float("nan"), "t_nw": float("nan"),
                "n_days": len(ics), "series": ics}
    x = [c for _, c in ics]
    plain, nw = clustered_mean_t(x, 1), clustered_mean_t(x, horizon)
    nan = float("nan")
    return {"mean": plain["mean"], "t": plain["t"] if plain["t"] is not None else nan,
            "t_nw": nw["t"] if nw["t"] is not None else nan, "n_days": len(x), "series": ics}


def decile_means(dates, score, label) -> list[float]:
    """Mean label by per-date score decile (deciles formed WITHIN each date, so
    a regime where every score drifts cannot fake a spread)."""
    import numpy as np
    import pandas as pd

    df = pd.DataFrame({"d": np.asarray(dates), "s": np.asarray(score, "float64"),
                       "y": np.asarray(label, "float64")}).dropna()
    df["dec"] = df.groupby("d")["s"].transform(
        lambda s: np.floor(s.rank(method="first") * 10 / (len(s) + 1)).clip(0, 9))
    return [float(df.loc[df["dec"] == k, "y"].mean()) for k in range(10)]
