"""Shared loaders/metrics for 07_train_flaml.py, 08_validate.py, 09_tier_cuts.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml import config  # noqa: E402


def load_split(split: str, idf, max_rows: int | None = None):
    """Assemble X (tfidf | dense CSR float32), y, weights, meta for one split.
    Shards were written in d1 order, so concatenation preserves time order."""
    import numpy as np
    import pandas as pd
    import scipy.sparse as sp

    from portfolio_tracker import ml_features as mf

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
