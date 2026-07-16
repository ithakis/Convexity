"""Finalize + deploy the artifact bundle.

Collects everything production inference needs into
ml/data/artifacts/mlsent-v1/ (07 wrote model.lgbm.txt + idf.npy +
train_metrics.json; 09 wrote tier_cuts.json), adds feature_schema.json and
meta.json (provenance), verifies completeness, and — with --deploy — copies
the bundle to ~/.portfolio_tracker/ml_model/mlsent-v1/ (or MLSENT_MODEL_DIR)
where portfolio_tracker.ml_sentiment loads it.

Usage:
    python ml/scripts/10_export_artifact.py            # bundle + verify
    python ml/scripts/10_export_artifact.py --deploy   # + copy to model dir
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml import config  # noqa: E402

REQUIRED = ("model.lgbm.txt", "idf.npy", "col_mask.npy", "feature_schema.json",
            "tier_cuts.json", "meta.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deploy", action="store_true")
    args = ap.parse_args()

    from portfolio_tracker import ml_features as mf

    out = config.ARTIFACTS_DIR / config.ARTIFACT_VERSION
    out.mkdir(parents=True, exist_ok=True)

    (out / "feature_schema.json").write_text(json.dumps(mf.feature_schema(), indent=2))

    git_sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, cwd=config.REPO_ROOT).stdout.strip()
    metrics = json.loads((out / "train_metrics.json").read_text())
    manifest = {}
    if config.MANIFEST_JSON.exists():
        manifest = {k: v["sha256"][:16] for k, v in
                    json.loads(config.MANIFEST_JSON.read_text()).items()}
    (out / "meta.json").write_text(json.dumps({
        "version": config.ARTIFACT_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git_sha": git_sha,
        "data_manifest_sha256_prefixes": manifest,
        "train_rows": metrics["train_rows"],
        "test_rows": metrics["test_rows"],
        "holdout_metrics": metrics["holdout"],
        "best_config": metrics["best_config"],
        "label": "SAR: timing-aware abnormal return / trailing window-type sigma, winsor +/-5",
        "test_window": [config.TEST_START, "end-of-data"],
    }, indent=2))

    missing = [f for f in REQUIRED if not (out / f).exists()]
    if missing:
        sys.exit(f"FAIL: bundle incomplete, missing {missing}")
    sizes = {f: round((out / f).stat().st_size / 1e6, 2) for f in REQUIRED}
    print(json.dumps({"bundle": str(out), "files_mb": sizes}, indent=2), flush=True)

    if args.deploy:
        import os

        dst = Path(os.environ.get("MLSENT_MODEL_DIR")
                   or Path.home() / ".portfolio_tracker" / "ml_model" / config.ARTIFACT_VERSION)
        dst.mkdir(parents=True, exist_ok=True)
        for f in REQUIRED:
            shutil.copy2(out / f, dst / f)
        print(f"deployed -> {dst}", flush=True)


if __name__ == "__main__":
    main()
