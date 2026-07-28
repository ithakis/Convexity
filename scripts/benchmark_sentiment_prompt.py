"""One-time benchmark of the per-article scoring prompt on Financial
PhraseBank (Malo et al. 2014) — the standard labeled dataset for financial
sentiment. Certifies the NIM model + array prompt against the published
FinBERT reference (~0.86 accuracy on the 66%-agreement split) before
trusting it in production; re-run whenever the prompt or model changes.

The dataset is NOT vendored (its license is CC BY-NC-SA; download on use):
    curl -sL -o /tmp/fpb.zip "https://huggingface.co/datasets/takala/financial_phrasebank/resolve/main/data/FinancialPhraseBank-v1.0.zip"
    unzip -d /tmp/fpb /tmp/fpb.zip

Mapping: our continuous score -> label via the neutral band used everywhere
else in the engine (> +0.15 positive, < -0.15 negative, else neutral).

Rate-limit care: sentences are batched 10 per NIM call through the SAME
array prompt shape production uses; a 300-sentence stratified sample is 30
calls (~35s at the 60/min limiter). Use --n 0 for the full split (~450
calls, ~8 min).

Usage:
    <pt python> scripts/benchmark_sentiment_prompt.py --data /tmp/fpb/FinancialPhraseBank-v1.0 [--n 300]
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_tracker import news_sentiment as ns

_BENCH_SYSTEM_PROMPT = """You are a senior financial analyst. Score each numbered financial news sentence INDIVIDUALLY for its sentiment from an investor's point of view.
- score: float in [-1,+1]. Positive developments (growth, beats, upgrades, wins) > 0; negative (losses, declines, layoffs, downgrades) < 0; factual statements with no directional information = 0.0.
- event: one of "earnings","guidance","ma","analyst","legal_regulatory","product","insider","macro","other".
- relevance: always "high".
OUTPUT — ONLY this JSON object, no markdown:
{"articles":[{"id":1,"score":0.0,"event":"other","relevance":"high"}, ...],"brief":""}
Include every id exactly once."""


def load_split(path: Path) -> list[tuple[str, str]]:
    rows = []
    for line in path.read_text(encoding="latin-1").splitlines():
        if "@" not in line:
            continue
        sent, label = line.rsplit("@", 1)
        label = label.strip().lower()
        if label in ("positive", "negative", "neutral"):
            rows.append((sent.strip(), label))
    return rows


def to_label(score: float) -> str:
    if score > 0.15:
        return "positive"
    if score < -0.15:
        return "negative"
    return "neutral"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="dir containing Sentences_66Agree.txt")
    ap.add_argument("--split", default="Sentences_66Agree.txt")
    ap.add_argument("--n", type=int, default=300, help="stratified sample size (0 = all)")
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rows = load_split(Path(args.data) / args.split)
    by_label = defaultdict(list)
    for r in rows:
        by_label[r[1]].append(r)
    if args.n:
        rng = random.Random(args.seed)
        per = args.n // 3
        rows = sum((rng.sample(v, min(per, len(v))) for v in by_label.values()), [])
        rng.shuffle(rows)
    print(f"[bench] {len(rows)} sentences ({Counter(l for _, l in rows)})", flush=True)

    t0 = time.time()
    preds: list[str | None] = []
    for i in range(0, len(rows), args.batch):
        batch = rows[i:i + args.batch]
        user = "Sentences:\n" + "\n".join(f"[{j+1}] {s}" for j, (s, _) in enumerate(batch))
        got = ns._score_articles_once(_BENCH_SYSTEM_PROMPT, user, len(batch))
        if got is None:  # one retry, then count the batch as failed
            got = ns._score_articles_once(_BENCH_SYSTEM_PROMPT, user, len(batch))
        scores = got[0] if got else [None] * len(batch)
        preds.extend(to_label(sc["score"]) if sc else None for sc in scores)
        done = min(i + args.batch, len(rows))
        if done % 50 < args.batch:
            print(f"[bench] {done}/{len(rows)} t={time.time()-t0:.0f}s", flush=True)

    pairs = [(p, t) for p, (_, t) in zip(preds, rows) if p is not None]
    n = len(pairs)
    acc = sum(p == t for p, t in pairs) / n if n else 0.0
    print(f"\n[bench] scored {n}/{len(rows)} sentences · accuracy {acc:.3f} "
          f"(FinBERT reference ~0.86)")
    f1s = []
    for lbl in ("positive", "negative", "neutral"):
        tp = sum(1 for p, t in pairs if p == lbl and t == lbl)
        fp = sum(1 for p, t in pairs if p == lbl and t != lbl)
        fn = sum(1 for p, t in pairs if p != lbl and t == lbl)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        f1s.append(f1)
        print(f"  {lbl:>9}: precision {prec:.3f}  recall {rec:.3f}  F1 {f1:.3f}")
    print(f"  macro-F1 {sum(f1s)/3:.3f}")
    conf = Counter((t, p) for p, t in pairs)
    print("\n  confusion (true -> pred):")
    for t in ("positive", "negative", "neutral"):
        line = "  ".join(f"{p[:3]}:{conf.get((t, p), 0):4d}" for p in ("positive", "negative", "neutral"))
        print(f"  {t:>9} | {line}")


if __name__ == "__main__":
    main()
