"""Finalize + deploy the Market read artifact bundle (mlsent-v1.1).

The bundle is the per-article encoder (07 --stage encoder; files in
artifacts/mlsent-v1/) plus tier_cuts.json from 09 --model v1.1 — the
recalibrated v1 score. The v2 window model (07 --stage window) failed its
gates and has no export path: the app cannot serve it
(docs/ml_sentiment_design.md).

feature_schema.json is regenerated from convexity.ml_features here,
so the bundle states the exact feature contract the app verifies on load.
meta.json records provenance and the tier-cut verification. With --deploy the
bundle is copied to <data>/models/mlsent-v1.1/ (src/convexity/paths.py; or
MLSENT_MODEL_DIR), where convexity.ml_sentiment loads it.

With --tarball the bundle is packed into the release asset that
src/convexity/model_fetch.py downloads on first run (deterministic, so the same
files always give the same SHA-256). --from packs an existing bundle directory
instead of exporting one — that is how the published asset was built, from a
copy of the deployed model. The printed SHA-256 goes into
model_fetch.MODEL_SHA256 (CLAUDE.md §4 "Model release procedure").

Usage:
    python ml/scripts/10_export_artifact.py [--deploy] [--tarball [--out PATH]]
    python ml/scripts/10_export_artifact.py --tarball --from <bundle dir> [--out PATH]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ml import config  # noqa: E402

ENCODER_FILES = ("model.lgbm.txt", "idf.npy", "col_mask.npy")


def _read(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deploy", action="store_true")
    ap.add_argument(
        "--tarball",
        action="store_true",
        help="also write <version>.tar.gz, the first-run download asset",
    )
    ap.add_argument(
        "--from",
        dest="src",
        type=Path,
        help="with --tarball: pack this existing bundle dir, skip the export",
    )
    ap.add_argument("--out", type=Path, help="tarball path (default: next to the bundle)")
    args = ap.parse_args()
    if args.src:
        if not args.tarball:
            ap.error("--from requires --tarball")
        _tarball(args.src, args.out)
        return

    from convexity import ml_features as mf
    from convexity.ml_sentiment import ARTIFACT_VERSION as version

    enc = config.ARTIFACTS_DIR / config.ARTIFACT_VERSION
    out = config.ARTIFACTS_DIR / version
    out.mkdir(parents=True, exist_ok=True)
    for f in ENCODER_FILES:
        shutil.copy2(enc / f, out / f)
    (out / "feature_schema.json").write_text(json.dumps(mf.feature_schema(), indent=2))
    required = [*ENCODER_FILES, "feature_schema.json", "tier_cuts.json", "meta.json"]

    cuts = _read(out / "tier_cuts.json")
    if not cuts:
        sys.exit(f"FAIL: {out / 'tier_cuts.json'} missing — run 09_tier_cuts.py --model v1.1")
    if int(cuts.get("horizon_days", -1)) != 1:
        sys.exit("FAIL: tier_cuts.json horizon does not match the exported model")
    git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=config.REPO_ROOT
    ).stdout.strip()
    (out / "meta.json").write_text(
        json.dumps(
            {
                "version": version,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "git_sha": git_sha,
                "encoder": _read(enc / "meta.json") or {"source": str(enc)},
                "horizon_days": cuts["horizon_days"],
                "tier_holdout": cuts.get("holdout_verification"),
                "label": f"SAR over the next {cuts['horizon_days']} trading day(s) from close(D): "
                "beta-adjusted abnormal return / trailing sigma, winsor +/-5",
                "calibration_window": cuts.get("calibration_window"),
            },
            indent=2,
            default=str,
        )
    )

    missing = [f for f in required if not (out / f).exists()]
    if missing:
        sys.exit(f"FAIL: bundle incomplete, missing {missing}")
    sizes = {f: round((out / f).stat().st_size / 1e6, 2) for f in required}
    print(json.dumps({"bundle": str(out), "files_mb": sizes}, indent=2), flush=True)

    if args.deploy:
        from convexity import paths

        dst = Path(os.environ.get("MLSENT_MODEL_DIR") or paths.models_dir() / version)
        dst.mkdir(parents=True, exist_ok=True)
        for f in required:
            shutil.copy2(out / f, dst / f)
        print(f"deployed -> {dst}", flush=True)
    if args.tarball:
        _tarball(out, args.out)


def _tarball(src: Path, out: Path | None) -> None:
    from convexity import model_fetch

    out = out or src.parent / f"{model_fetch.MODEL_VERSION}.tar.gz"
    sha = model_fetch.pack(src, out)
    print(
        json.dumps(
            {
                "tarball": str(out),
                "bytes": out.stat().st_size,
                "sha256": sha,
                "next": "set model_fetch.MODEL_SHA256 to this sha256, publish "
                "the file as a release asset",
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
