"""Certify the News read (LLM, five lenses) before trusting it in production.
Re-run after ANY change to the prompt, the schema or the model — and only
then: every run spends real NIM quota (~100 calls for the gold set).

Two benchmarks, both through the production transport (news_sentiment.
read_headlines: strict json_schema, thinking off, two passes):

1. Gold set — tests/data/news_gold.jsonl: ~150 real headlines from the app's
   own cache across 50 tickers, hand-labelled with a lens and a direction
   (-1/0/+1). Several headlines appear twice with different targets on
   purpose (a Datadog story is `none` for NVDA and `financials` for DDOG):
   target attribution is the failure the old engine had. Headlines are
   batched per target, <= 15 per call, exactly like a refresh.

2. Financial PhraseBank (Malo et al. 2014) direction check — sign accuracy
   on the positive/negative sentences of the 66%-agreement split. Not
   vendored (CC BY-NC-SA); download on use:
       curl -sL -o /tmp/fpb.zip "https://huggingface.co/datasets/takala/financial_phrasebank/resolve/main/data/FinancialPhraseBank-v1.0.zip"
       unzip -d /tmp/fpb /tmp/fpb.zip

Acceptance (plan §4, Gate 1):
    lens accuracy (gold lens != none)          >= 0.75
    `none` precision                           >= 0.85
    direction accuracy (gold direction != 0)   >= 0.80
    PhraseBank direction                       >= 0.80
    two-pass agreement mean                    >= 0.85
    JSON validity after retries                == 100%
    p95 latency per ticker (two passes)        <= 30 s

Usage:
    <pt python> scripts/benchmark_news_read.py [--phrasebank /tmp/fpb/FinancialPhraseBank-v1.0]
        [--out report.json]

The gold metrics are also reported on the confidently-labelled subset
(`gold_certain_only`); the gates use the full set.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from convexity import news_sentiment as ns  # noqa: E402

GOLD = Path(__file__).resolve().parent.parent / "tests" / "data" / "news_gold.jsonl"

_PHRASEBANK_PROMPT = """You are an equity analyst. Each numbered sentence is about one company. Score what it says about THAT company's business: integer -2 clearly negative, -1 negative, 0 neutral or purely factual, +1 positive, +2 clearly positive. Pick the closest lens (financials, outlook, competition, regulation, street, other); never use none. fact: at most 14 words, no adjectives. Include every id exactly once. brief: an empty string."""


def _batches(rows: list[dict], size: int = ns._SCORE_BATCH):
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_sym[r["symbol"]].append(r)
    for sym, items in by_sym.items():
        for i in range(0, len(items), size):
            yield sym, items[i:i + size]


def run_gold(rows: list[dict]) -> dict:
    preds: dict[int, dict] = {}
    latencies, agreements, failed = [], [], 0
    for sym, items in _batches(rows):
        arts = [{"headline": r["headline"], "summary": r["summary"], "source": r["source"],
                 "datetime": 0, "n_duplicates": 0} for r in items]
        prompt = ns._build_articles_prompt(arts, sym, {"name": items[0]["name"]})
        t0 = time.time()
        res = ns.read_headlines(ns._NEWS_READ_PROMPT, prompt, len(items))
        latencies.append(time.time() - t0)
        if res is None:
            failed += 1
            print(f"  {sym}: FAILED ({ns.llm_status().get('error')})", flush=True)
            continue
        got, _brief, agreement = res
        if agreement is not None:
            agreements.append(agreement)
        for r, g in zip(items, got):
            preds[r["id"]] = g
        print(f"  {sym:>10}: {len(items):2d} headlines  {latencies[-1]:5.1f}s  "
              f"agreement {agreement if agreement is not None else '—'}", flush=True)
    return {"preds": preds, "latencies": latencies, "agreements": agreements,
            "failed_batches": failed, "n_batches": len(latencies)}


def score_gold(rows: list[dict], preds: dict[int, dict]) -> dict:
    scored = [r for r in rows if r["id"] in preds]
    lens_rows = [r for r in scored if r["lens"] != "none"]
    lens_acc = sum(preds[r["id"]]["lens"] == r["lens"] for r in lens_rows) / max(1, len(lens_rows))
    pred_none = [r for r in scored if preds[r["id"]]["lens"] == "none"]
    none_prec = sum(r["lens"] == "none" for r in pred_none) / max(1, len(pred_none))
    gold_none = [r for r in scored if r["lens"] == "none"]
    none_rec = sum(preds[r["id"]]["lens"] == "none" for r in gold_none) / max(1, len(gold_none))
    dir_rows = [r for r in scored if r["direction"] != 0]

    def sign(x):
        return (x > 0) - (x < 0)

    dir_acc = sum(sign(preds[r["id"]]["score"]) == r["direction"] for r in dir_rows) \
        / max(1, len(dir_rows))
    confusion = Counter((r["lens"], preds[r["id"]]["lens"]) for r in scored)
    misses = [{"id": r["id"], "symbol": r["symbol"], "headline": r["headline"],
               "gold": f"{r['lens']} {r['direction']:+d}",
               "pred": f"{preds[r['id']]['lens']} {preds[r['id']]['score']:+.1f}",
               "uncertain": r["uncertain"]}
              for r in scored
              if preds[r["id"]]["lens"] != r["lens"]
              or (r["direction"] and sign(preds[r["id"]]["score"]) != r["direction"])]
    return {"n": len(scored), "lens_accuracy": round(lens_acc, 3),
            "none_precision": round(none_prec, 3), "none_recall": round(none_rec, 3),
            "direction_accuracy": round(dir_acc, 3), "n_direction": len(dir_rows),
            "confusion": {f"{g}->{p}": c for (g, p), c in sorted(confusion.items())},
            "misses": misses}


def run_phrasebank(path: Path, n: int, seed: int = 42) -> dict:
    rows = []
    for line in (path / "Sentences_66Agree.txt").read_text(encoding="latin-1").splitlines():
        if "@" not in line:
            continue
        sent, label = line.rsplit("@", 1)
        label = label.strip().lower()
        if label in ("positive", "negative"):
            rows.append((sent.strip(), 1 if label == "positive" else -1))
    rng = random.Random(seed)
    pos = [r for r in rows if r[1] > 0]
    neg = [r for r in rows if r[1] < 0]
    rows = rng.sample(pos, min(n // 2, len(pos))) + rng.sample(neg, min(n // 2, len(neg)))
    rng.shuffle(rows)
    correct = total = failed = 0
    for i in range(0, len(rows), 10):
        batch = rows[i:i + 10]
        user = "Sentences:\n" + "\n".join(f"[{j + 1}] {s}" for j, (s, _) in enumerate(batch))
        res = ns.read_headlines(_PHRASEBANK_PROMPT, user, len(batch))
        if res is None:
            failed += 1
            continue
        for (_, lab), it in zip(batch, res[0]):
            total += 1
            correct += ((it["score"] > 0) - (it["score"] < 0)) == lab
    return {"n": total, "direction_accuracy": round(correct / max(1, total), 3),
            "failed_batches": failed}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phrasebank", type=str, default=None)
    ap.add_argument("--pb-n", type=int, default=200)
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    ns.llm_unblock()
    rows = [json.loads(line) for line in GOLD.read_text().splitlines() if line.strip()]
    print(f"[gold] {len(rows)} headlines, {len({r['symbol'] for r in rows})} tickers, "
          f"model {ns._MODEL}", flush=True)
    run = run_gold(rows)
    gold = score_gold(rows, run["preds"])
    certain = score_gold([r for r in rows if not r["uncertain"]], run["preds"])
    lat = sorted(run["latencies"])
    p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))] if lat else float("nan")
    agree = sum(run["agreements"]) / max(1, len(run["agreements"]))
    report = {"model": ns._MODEL, "gold": gold, "gold_certain_only": {
        k: certain[k] for k in ("n", "lens_accuracy", "none_precision", "direction_accuracy")},
        "agreement_mean": round(agree, 3), "p95_latency_s": round(p95, 1),
        "json_valid": 1 - run["failed_batches"] / max(1, run["n_batches"])}
    if args.phrasebank:
        print("[phrasebank] scoring...", flush=True)
        report["phrasebank"] = run_phrasebank(Path(args.phrasebank), args.pb_n)
    gates = {
        "lens accuracy >= 0.75": gold["lens_accuracy"] >= 0.75,
        "none precision >= 0.85": gold["none_precision"] >= 0.85,
        "direction accuracy >= 0.80": gold["direction_accuracy"] >= 0.80,
        "two-pass agreement >= 0.85": agree >= 0.85,
        "JSON validity 100%": report["json_valid"] == 1.0,
        "p95 latency <= 30 s": p95 <= 30.0,
    }
    if "phrasebank" in report:
        gates["PhraseBank direction >= 0.80"] = report["phrasebank"]["direction_accuracy"] >= 0.80
    report["gates"] = gates
    print(json.dumps({k: v for k, v in report.items() if k != "gold"}, indent=2))
    print(json.dumps({k: v for k, v in gold.items() if k != "misses"}, indent=2))
    print("\nmisses:")
    for m in gold["misses"]:
        print(f"  [{m['id']:3d}] {m['symbol']:>9} gold {m['gold']:<16} pred {m['pred']:<16}"
              f"{' (uncertain)' if m['uncertain'] else ''}  {m['headline'][:90]}")
    print("\n" + "\n".join(f"  {'PASS' if ok else 'FAIL'}  {g}" for g, ok in gates.items()))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
