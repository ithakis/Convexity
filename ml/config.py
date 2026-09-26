"""Single source of truth for the MLNews training pipeline.

Everything the numbered scripts in ml/scripts/ share lives here: paths,
feature dimensions, date boundaries, seeds. Production inference does NOT
import this module — the deployed artifact carries its own feature_schema.json
so the app never depends on the ml/ tree.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# ---------------------------------------------------------------- paths
ML_DIR = Path(__file__).resolve().parent
REPO_ROOT = ML_DIR.parent
# The 4.8 GB data tree is not in git and does not travel between worktrees.
# MLNEWS_DATA_DIR points the pipeline at an existing copy in place instead of
# duplicating it (it lives in whichever checkout first ran 00-06).
DATA_DIR = Path(os.environ["MLNEWS_DATA_DIR"]) if os.environ.get("MLNEWS_DATA_DIR") \
    else ML_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PARQUET_DIR = DATA_DIR / "parquet"
FEATURES_DIR = DATA_DIR / "features"
ARTIFACTS_DIR = DATA_DIR / "artifacts"
REPORTS_DIR = DATA_DIR / "reports"

NEWS_PARQUET = PARQUET_DIR / "news"          # partitioned by year
PRICES_PARQUET = PARQUET_DIR / "prices.parquet"
SPY_PARQUET = PARQUET_DIR / "spy.parquet"    # fallback if SPY absent from FNSPID prices
CALENDAR_PARQUET = PARQUET_DIR / "calendar.parquet"
TICKER_STATS_PARQUET = PARQUET_DIR / "ticker_daily_stats.parquet"
LABELED_PARQUET = PARQUET_DIR / "labeled.parquet"

TIMESTAMP_AUDIT_JSON = REPORTS_DIR / "timestamp_audit.json"
MANIFEST_JSON = RAW_DIR / "MANIFEST.json"

# ---------------------------------------------------------------- dataset
HF_REPO_ID = "Zihan1004/FNSPID"
HF_NEWS_FILE = "Stock_news/nasdaq_exteral_data.csv"   # [sic] upstream typo
HF_PRICE_FILE = "Stock_price/full_history.zip"

# ---------------------------------------------------------------- text/features
HASH_DIM = 2 ** 18
NGRAM_RANGE = (1, 2)
SUMMARY_PRIORITY = ("Textrank_summary", "Lsa_summary", "Luhn_summary", "Lexrank_summary")
SUMMARY_MAX_CHARS = 1500

# ---------------------------------------------------------------- labels
SIGMA_WINDOW = 60          # trailing obs for window-type sigma
SIGMA_MIN_OBS = 40
BETA_WINDOW = 252          # trailing obs for daily beta vs SPY
BETA_MIN_OBS = 120
BETA_BLUME = (0.67, 0.33)  # shrinkage toward 1.0
BETA_CLIP = (0.0, 3.0)
SAR_WINSOR = 5.0

# ---------------------------------------------------------------- training
TEST_START = "2023-07-01"  # 6-month holdout: FNSPID ends ~Dec 2023
TRAIN_ROW_BUDGET = 3_000_000
N_CV_FOLDS = 3
TIME_BUDGET_S = 4 * 3600
N_JOBS = 8
SEED = 42
ARTIFACT_VERSION = "mlsent-v1"

# ---------------------------------------------------------------- v2 (ticker-day)
# The Market read v2 predicts at the unit the app actually scores: one row per
# (ticker, as-of trading day D). The window/attention lengths are NOT here:
# they are serving constants too, so they live in convexity.ml_features.
ENCODER_FIRST_YEAR = 2011      # expanding-window encoders start once >=100k rows exist
ENCODER_MAX_ROWS = 1_500_000   # per cross-fit (8 GB box; v1 config is 56 trees)
SEL_START = "2023-01-01"       # model selection / tier calibration window start
HORIZONS = (1, 5)              # next-1d and next-5d SAR labels


def ensure_dirs() -> None:
    for d in (RAW_DIR, PARQUET_DIR, FEATURES_DIR, ARTIFACTS_DIR, REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_timestamp_audit() -> dict:
    """Downstream scripts read the audit's decisions (e.g. SOURCE_TZ) from here."""
    with open(TIMESTAMP_AUDIT_JSON) as fh:
        return json.load(fh)
