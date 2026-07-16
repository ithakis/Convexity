"""Download FNSPID raw files from Hugging Face with resume + integrity manifest.

Idempotent: hf_hub_download resumes interrupted transfers natively via
.incomplete files, and a completed file is skipped on re-run. Each file's
size + sha256 is recorded in ml/data/raw/MANIFEST.json only after the
download fully completes, so downstream scripts can gate on the manifest.

Usage:
    python ml/scripts/00_download_fnspid.py            # news CSV only (default: biggest first)
    python ml/scripts/00_download_fnspid.py --prices   # prices zip only
    python ml/scripts/00_download_fnspid.py --all
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml import config  # noqa: E402

RETRIES = 5


def _sha256(path: Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _manifest() -> dict:
    if config.MANIFEST_JSON.exists():
        return json.loads(config.MANIFEST_JSON.read_text())
    return {}


def _save_manifest(m: dict) -> None:
    config.MANIFEST_JSON.write_text(json.dumps(m, indent=2))


def download(filename: str, min_free_gb: float) -> Path:
    from huggingface_hub import hf_hub_download

    free_gb = shutil.disk_usage(config.RAW_DIR).free / 1e9
    if free_gb < min_free_gb:
        sys.exit(f"ABORT: only {free_gb:.1f} GB free, need {min_free_gb} GB for {filename}")

    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            path = hf_hub_download(
                repo_id=config.HF_REPO_ID,
                repo_type="dataset",
                filename=filename,
                local_dir=config.RAW_DIR,
            )
            return Path(path)
        except Exception as e:  # network hiccups: back off and resume
            last_err = e
            wait = min(60, 2 ** attempt * 5)
            print(f"[attempt {attempt}/{RETRIES}] {type(e).__name__}: {e} — retrying in {wait}s", flush=True)
            time.sleep(wait)
    raise SystemExit(f"Download failed after {RETRIES} attempts: {last_err}")


def fetch(filename: str, min_free_gb: float) -> None:
    m = _manifest()
    local = config.RAW_DIR / filename
    if filename in m and local.exists() and local.stat().st_size == m[filename]["size"]:
        print(f"SKIP {filename} — already downloaded and in manifest", flush=True)
        return
    t0 = time.time()
    path = download(filename, min_free_gb)
    size = path.stat().st_size
    print(f"downloaded {filename}: {size/1e9:.2f} GB in {(time.time()-t0)/60:.1f} min; hashing…", flush=True)
    m[filename] = {
        "size": size,
        "sha256": _sha256(path),
        "path": str(path),
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save_manifest(m)
    print(f"OK {filename} sha256={m[filename]['sha256'][:16]}…", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--news", action="store_true")
    ap.add_argument("--prices", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    config.ensure_dirs()

    want_news = args.news or args.all or not (args.prices)
    want_prices = args.prices or args.all
    if want_news:
        fetch(config.HF_NEWS_FILE, min_free_gb=26.0)
    if want_prices:
        fetch(config.HF_PRICE_FILE, min_free_gb=10.0)


if __name__ == "__main__":
    main()
