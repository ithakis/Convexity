"""Live check of how My Investments import *reads* (importer.read: the
vision model for screenshots and scanned pages, then the text model, then
the symbol pack). Each case in tests/data/import/gold.json lists the rows a
correct reading must produce; the script prints pass/fail per case and any
row that is missing, extra or different.

The gold set is synthetic (made-up amounts, public tickers): never add a
real statement. Rebuild its images with scripts/build_import_gold.py.

Needs NVIDIA_API_KEY in the environment (CLAUDE.md §3: pass it from the real
config.json, never print it) and a temp CONVEXITY_HOME with the symbol pack
installed. Re-run after any change to the prompts, the matching rules or a
model:

    CONVEXITY_HOME=<temp home with pack> NVIDIA_API_KEY=... uv run python scripts/eval_import.py [case-substring]
"""

import base64
import json
import sys
import time
from pathlib import Path

from convexity import importer

GOLD = Path(__file__).resolve().parent.parent / "tests" / "data" / "import"
MIME = {".png": "image/png", ".pdf": "application/pdf", ".csv": "text/csv"}
FIELDS = ("type", "date", "symbol", "qty", "price", "amount", "fee", "ccy")


def _file(name: str) -> dict:
    p = GOLD / name
    return {
        "name": name,
        "type": MIME.get(p.suffix, ""),
        "data": base64.b64encode(p.read_bytes()).decode(),
    }


def _same(want: dict, got: dict) -> list[str]:
    """The fields of ``want`` that ``got`` disagrees with (numbers to 1e-6)."""
    bad = []
    for k in FIELDS:
        if k not in want:
            continue
        w, g = want[k], got.get(k)
        if isinstance(w, (int, float)) and not isinstance(w, bool):
            if g is None or abs(float(g) - w) > 1e-6 * max(1.0, abs(w)):
                bad.append(f"{k} {g!r}≠{w!r}")
        elif (g or "") != w:
            bad.append(f"{k} {g!r}≠{w!r}")
    return bad


def _key(r: dict) -> tuple:
    return r.get("type"), r.get("symbol") or "", r.get("date") or ""


def run_case(case: dict) -> tuple[bool, list[str], float]:
    t0 = time.time()
    for attempt in range(3):
        try:
            out = importer.read(case.get("text", ""), [_file(f) for f in case.get("files", [])])
            break
        except importer.ImportRefused as exc:
            if exc.status != 503 or attempt == 2:
                return False, [f"refused: {exc.message}"], time.time() - t0
            time.sleep(8)
    got = list(out["items"])
    notes = []
    for want in case["rows"]:
        match = next((g for g in got if _key(g) == _key(want)), None)
        if match is None:
            match = next(
                (
                    g
                    for g in got
                    if g.get("type") == want["type"]
                    and (g.get("symbol") or "") == want.get("symbol", "")
                ),
                None,
            )
        if match is None:
            notes.append(f"missing {want}")
            continue
        got.remove(match)
        bad = _same(want, match)
        if bad:
            notes.append(f"{want['type']} {want.get('symbol', '')}: " + ", ".join(bad))
    notes += [f"extra {g['type']} {g.get('symbol')} {g.get('date')}" for g in got]
    asked = {q["id"] for q in out["questions"]}
    for q in case.get("questions", []):
        if q not in asked:
            notes.append(f"question {q!r} not asked")
    return not notes, notes, time.time() - t0


def main() -> int:
    only = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    cases = [
        c
        for c in json.loads((GOLD / "gold.json").read_text())["cases"]
        if only in c["name"].lower()
    ]
    passed = 0
    for c in cases:
        ok, notes, secs = run_case(c)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {c['name']}  ({secs:.1f}s)")
        for n in notes:
            print(f"      {n}")
    print(f"\n{passed}/{len(cases)} cases pass")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
