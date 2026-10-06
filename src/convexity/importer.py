"""My Investments import: text, spreadsheets and screenshots -> entries to review.

The owner's way into the book (roadmap Phases 5 and 6, run together): drop
broker screenshots or statements, paste what the broker app shows, or just
describe it. Nothing is written here. ``read()`` returns a *proposal* that
the page shows as a review table, and only the owner's Apply writes it, as
one undoable batch (``investments.add_batch``).

Two NIM stages, both on news_sentiment's client, rate limiter and circuit
breaker (one quota, shared with the News read and company search):

1. **Screenshots are transcribed to plain text** by a vision model: the
   nemotron-3 nano omni with thinking off. On the synthetic gold screenshots
   it read every field right in ~7 s, where Llama 3.2 90B misread two dates
   and a currency and took 75 s (probed 2026-10-06; nemotron-parse was
   unavailable). Plain text, not JSON: in JSON mode it split rows into cells.
   A phone screenshot must keep its resolution: shrunk, the same model read
   405.00 as 405.05.
2. **All text** (pasted, the transcripts, CSV/Excel rendered as CSV) goes to
   the app's text model under a strict schema of ledger rows.

Then plain code decides what to trust, the same rule as company search: the
model may suggest, never decide.
- Every ticker is checked against the symbol pack, and the listing is
  chosen by the currency the row is in (a £ price makes "Shell" SHEL.L, not
  the NYSE line, which is the pack's home listing for the name).
- qty x price is checked against the row's total; a mismatch is flagged
  with the price the total implies (a misread digit is the usual cause).
- What the model guessed is marked (purple on the page) and what is missing
  is marked (amber); at most three one-click questions, each with a default.

Logs carry counts and timings only: never amounts, text or file contents.
"""

from __future__ import annotations

import base64
import io
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from convexity.helpers import Cancelled, Deadline, major_ccy
from convexity.investments import BookError

VISION_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"

MAX_FILES = 8
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TEXT = 60_000  # characters of pasted text
# Rows per text-stage call. Each row comes back as ~16 JSON fields (~80
# tokens): 90 rows overran the 8,000-token cap and every reply was cut-off
# JSON (verify, 2026-10-06); 30 rows leave ample room.
_CHUNK_LINES = 30
_MAX_TOKENS = 8000
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
_SHEET_EXT = (".csv", ".tsv", ".txt", ".xlsx")

TYPES = ("buy", "sell", "dividend", "deposit", "withdrawal", "fee", "split", "holding")

# The currency a listing trades in, from its Yahoo exchange code, so a row's
# currency can pick the right line of a company listed in several places.
_EXCH_CCY = {
    **dict.fromkeys(("NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS", "PNK", "OQX", "OEM"), "USD"),
    **dict.fromkeys(("LSE", "IOB", "AQS"), "GBP"),
    **dict.fromkeys(("GER", "FRA", "STU", "DUS", "MUN", "BER", "HAM", "AMS", "PAR", "MIL",
                     "MCE", "BRU", "LIS", "ISE", "HEL", "VIE", "ENX"), "EUR"),  # fmt: skip
    "EBS": "CHF", "TOR": "CAD", "VAN": "CAD", "CNQ": "CAD", "ASX": "AUD", "JPX": "JPY",
    "HKG": "HKD", "STO": "SEK", "CPH": "DKK", "OSL": "NOK",
}  # fmt: skip
_CCY_SUFFIX = {
    "GBP": (".L",), "EUR": (".DE", ".AS", ".PA", ".MI", ".MC", ".BR", ".IR", ".HE", ".VI", ".LS"),
    "CHF": (".SW",), "CAD": (".TO", ".V"), "AUD": (".AX",), "JPY": (".T",), "HKD": (".HK",),
    "SEK": (".ST",), "DKK": (".CO",), "NOK": (".OL",),
}  # fmt: skip


class ImportRefused(BookError):
    """A refusal the page shows as is (bad file, nothing readable, no key).
    A BookError, so the investments routes answer it like any other."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ inputs


def _decode_files(files) -> list[dict]:
    """``[{name, type, data(base64)}]`` from the page -> checked bytes."""
    if files is None:
        return []
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ImportRefused(f"Add at most {MAX_FILES} files at a time.")
    out = []
    for f in files:
        if not isinstance(f, dict):
            raise ImportRefused("A file couldn't be read.")
        name = str(f.get("name") or "file")[:120]
        mime = str(f.get("type") or "").lower()
        try:
            data = base64.b64decode(str(f.get("data") or ""), validate=True)
        except (ValueError, TypeError):
            raise ImportRefused(f"{name} couldn't be read.") from None
        if not data:
            raise ImportRefused(f"{name} is empty.")
        if len(data) > MAX_FILE_BYTES:
            raise ImportRefused(f"{name} is larger than {MAX_FILE_BYTES // 1024 // 1024} MB.")
        low = name.lower()
        if mime in _IMAGE_TYPES:
            kind = "image"
        elif low.endswith(_SHEET_EXT) or mime in ("text/csv", "text/plain"):
            kind = "sheet"
        elif low.endswith(".pdf") or mime == "application/pdf":
            kind = "pdf"
        elif low.endswith((".heic", ".heif")):
            raise ImportRefused(f"{name} is a HEIC photo. Save it as PNG or JPEG first.")
        elif low.endswith(".xls"):
            # Old-format Excel needs a reader the app doesn't ship (xlrd).
            raise ImportRefused(f"{name} is an old Excel file. Save it as .xlsx or CSV first.")
        else:
            raise ImportRefused(f"{name}: use screenshots (PNG, JPEG), CSV, Excel or text.")
        out.append({"name": name, "mime": mime, "data": data, "kind": kind})
    return out


def sheet_parts(name: str, data: bytes) -> list[tuple[str, str]]:
    """A spreadsheet or text file as (label, CSV text), one per sheet, each
    starting with its own header row: ``_chunks`` repeats a block's first
    line on every chunk, so a long sheet keeps its column names (a label
    line there once made every chunk after the first lose them)."""
    import pandas as pd

    try:
        if name.lower().endswith(".xlsx"):
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str, header=None)
            parts = []
            for sheet, df in sheets.items():
                df = df.dropna(how="all").dropna(axis=1, how="all")
                if not df.empty:
                    parts.append((f"{name}, sheet {sheet}", df.to_csv(index=False, header=False)))
            return parts
        text = data.decode("utf-8-sig", errors="replace")
    except Exception:
        raise ImportRefused(f"{name} couldn't be opened as a spreadsheet.") from None
    return [(name, text.replace("\r\n", "\n").replace("\r", "\n"))]


MAX_PDF_PAGES = 20
_PDF_TEXT_MIN = 40  # characters: fewer means a scanned page (a picture of text)


def pdf_parts(name: str, data: bytes) -> tuple[str, list[bytes]]:
    """A PDF statement as (its text, PNGs of its scanned pages). Pages with a
    text layer are read as text, which is exact and free; a scanned page has
    none, so it is rendered and goes to the vision model like a screenshot."""
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:
        raise ImportRefused(
            f"{name} couldn't be opened. If it has a password, save a copy without one."
        ) from None
    try:
        if len(pdf) > MAX_PDF_PAGES:
            raise ImportRefused(
                f"{name} has more than {MAX_PDF_PAGES} pages. Add the pages with your trades."
            )
        texts, images = [], []
        for i in range(len(pdf)):
            page = pdf[i]
            text = page.get_textpage().get_text_range()
            if len(text.strip()) >= _PDF_TEXT_MIN:
                texts.append(f"page {i + 1}:\n{text.strip()}")
            else:
                bmp = page.render(scale=2.0, rev_byteorder=True)
                images.append(png_bytes(bmp.to_numpy()))
            page.close()
    finally:
        pdf.close()
    return "\n\n".join(texts), images


def png_bytes(pixels) -> bytes:
    """An RGB(A) uint8 array as a PNG, with the standard library alone (the
    app ships no imaging library; pdfium hands back raw pixels)."""
    import struct
    import zlib

    import numpy as np

    arr = np.ascontiguousarray(pixels[..., :3] if pixels.ndim == 3 else pixels, dtype=np.uint8)
    h, w = arr.shape[:2]
    ctype = 2 if arr.ndim == 3 else 0
    # Each scanline starts with filter byte 0 (none).
    raw = np.hstack([np.zeros((h, 1), np.uint8), arr.reshape(h, -1)]).tobytes()

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + tag
            + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    head = struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", head)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


# ------------------------------------------------------------- NIM stages


def _vision_call(image: bytes, mime: str, label: str) -> str | None:
    """One screenshot -> plain-text rows (stage 1). Retries a 503 (routine on
    NIM); shares the limiter and the circuit breaker with the other callers."""
    from convexity import news_sentiment as ns

    client = ns._get_client()
    if client is None or not ns.NVIDIA_API_KEY:
        return None
    url = f"data:{mime};base64," + base64.b64encode(image).decode()
    messages = [
        {"role": "system", "content": "/no_think\n" + _VISION_PROMPT},
        {"role": "user", "content": [{"type": "text", "text": f"{label}:"},
                                     {"type": "image_url", "image_url": {"url": url}}]},
    ]  # fmt: skip
    deadline = Deadline(_STAGE_DEADLINE_S)
    for attempt in range(4):
        try:
            ns._wait_for_circuit_breaker("NVIDIA NIM", "_nv_rate_limit_until", cancel=deadline)
            ns._NV_LIMITER.acquire(cancel=deadline)
        except Cancelled:
            print("[import] vision step timed out waiting for NIM", flush=True)
            return None
        # The deadline bounds the calls and their retries too, not just the
        # waits: four 90 s attempts would otherwise outlast it by minutes.
        left = deadline.at - time.time()
        if left < 5:
            print("[import] vision step out of time", flush=True)
            return None
        try:
            r = client.chat.completions.create(
                model=VISION_MODEL, messages=messages, temperature=0.0, max_tokens=2500,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}}, timeout=min(90.0, left),
            )  # fmt: skip
            return (r.choices[0].message.content or "").strip()
        except Exception as exc:
            code = ns._http_status(exc)
            if code in (401, 403, 404, 410):
                print(f"[import] vision model unavailable ({code})", flush=True)
                return None
            if code == 429:
                ns._NV_LIMITER.penalize()
            print(
                f"[import] vision call failed (attempt {attempt + 1}): {code or type(exc).__name__}",
                flush=True,
            )
            time.sleep(2.0 + 2.0 * attempt)
    return None


# Each list entry in a broker app is a block of up to four texts: a title
# (the company), a smaller line under it (the type and date: "Buy · 12 Mar"),
# and on the right an amount with a smaller line under it ("10 shares at
# $182.40"). Asked loosely, the model sometimes dropped the smaller lines,
# and the text stage then had to guess every type and date; with section
# headers on lines of their own it sometimes dropped those, and the year
# with them (eval, 2026-10-06). So every row carries its header.
# No literal example values in this prompt: given one ("Buy · 12 Mar, 15:42"),
# the model copied it into a scanned table, turning 12.03.2024 into a date
# with an invented time and no year (eval, 2026-10-06).
_VISION_PROMPT = """You transcribe screenshots of brokerage and banking apps and statements.

Output one line per transaction or holding, with ALL the text of that entry
joined by ' | '.
- In a phone list, each entry is a block: a title (company or description),
  a smaller line under it (usually the type and the date), an amount on the
  right and a smaller line under that (usually shares and price). Output, in
  this order: the section header the entry sits under (a month with its
  year, or a word like Holdings; "-" if there is none) | the title | the
  smaller line under the title | the amount | the smaller line under the
  amount. Never leave out the smaller lines. Skip logos and icons.
- In a table, output the column headers once as the first line, then each
  row's cells in order.
Copy every character exactly as it appears: dates in their own format with
their year, numbers, signs and currency symbols. Never reformat, convert,
round, or add anything that is not in the image (no times, years or words of
your own). No commentary."""


def _system(today: str, base: str) -> str:
    return f"""You turn what someone pasted or uploaded from their broker into entries
for their investment ledger. Today is {today}. Their book is kept in {base}.

Return one item per transaction or holding you can see. Types:
- buy / sell: a trade. qty = number of shares; price = price per share exactly
  as shown, in the row's currency (pence stay pence: currency GBX); fee if shown.
- dividend: cash received for a holding. total = the cash received (the
  row's main amount); price = the amount per share only if shown; tax if shown.
- deposit / withdrawal: money put in or taken out (top up, transfer in, cash
  in, withdrawal, transfer out). total = the amount.
- fee: a standalone charge (custody fee, account fee). total = the amount,
  positive.
- split: a stock split. ratio = new shares per old share.
- holding: a position in a list of current holdings (a snapshot, not a trade).
  qty = shares held; price = the average price paid if shown, else null;
  date = the date shown for it, else "" (never today's date as a guess).
- A cash balance in a holdings list ("Cash", "USD 4,050") is a deposit with
  total = the balance, name "Cash balance", date "" unless one is shown.

Rules:
- date: YYYY-MM-DD. Take the year from section headers or nearby rows.
  Relative dates ("last month", "yesterday", "in March") count back from
  today, never from other rows. If you had to infer the year or the day, set
  date_guessed true. No date at all: "".
  Dates written with dots (12.03.2024) are day.month.year. With slashes,
  read them day first unless the account is in US dollars or a day is over
  12 in the month position; if you can't tell, set date_guessed true.
- ticker: the ticker if shown (for a row from a London account in pence it is
  still the plain ticker, like SHEL). If not shown, your best guess of the
  ticker and ticker_guessed true. name: the company as written.
- currency: the ISO code the price or amount is shown in (USD, GBP, EUR; GBX
  for pence), "" if you can't tell.
- total: the row's total cash amount as a positive number if shown, else null;
  total_currency: its currency.
- source: where the row is, like "image 1, row 3", "csv row 5" or "line 2".
- Never leave out a transaction or holding because you are unsure of its
  ticker, exchange or date: give your best guess and mark it guessed (the
  person checks every row). skipped is only for lines that are not
  transactions or holdings.
- Never invent a number: unknown numbers are null. Skip portfolio values,
  balances, percentage changes, column headers and totals; list what you
  skipped and why in skipped (short phrases)."""


def _schema() -> dict:
    s, n, b = {"type": "string"}, {"type": ["number", "null"]}, {"type": "boolean"}
    item = {
        "type": {"type": "string", "enum": list(TYPES)},
        "date": s, "date_guessed": b, "name": s, "ticker": s, "ticker_guessed": b,
        "qty": n, "price": n, "currency": s, "total": n, "total_currency": s,
        "fee": n, "tax": n, "ratio": n, "source": s,
    }  # fmt: skip
    obj = lambda props: {"type": "object", "additionalProperties": False,  # noqa: E731
                         "required": list(props), "properties": props}  # fmt: skip
    return obj({"items": {"type": "array", "items": obj(item)},
                "skipped": {"type": "array", "items": s}})  # fmt: skip


# Every NIM wait is bounded (helpers.Deadline): the limiter and circuit
# breaker are shared with news refreshes, and an eval call once sat 46
# minutes in that queue. Someone waiting on "Read it" gets "try again".
_STAGE_DEADLINE_S = 150.0

# A left-out line whose reason is doubt rather than "not an entry".
_UNSURE = re.compile(
    r"ticker|exchange|unsure|uncertain|confiden|ambiguous|not sure|can't tell|cannot tell",
    re.IGNORECASE,
)


def _structure(text: str, today: str, base: str) -> dict | None:
    from convexity import news_sentiment as ns

    try:
        return ns._nvidia_call(_system(today, base), text, (), Deadline(_STAGE_DEADLINE_S), schema=_schema(),
                               name="ledger_import", record=False, tag="import",
                               timeout=120.0, max_tokens=_MAX_TOKENS)  # fmt: skip
    except Cancelled:
        print("[import] reading step timed out", flush=True)
        return None


_TWICE_LINES = 20  # blocks this short (typed text, a phone screen) are read twice


def _score(out) -> tuple[int, int]:
    """More entries first; between equal counts, the reading with more of its
    numbers and dates filled in (the other one left a cell empty)."""
    if not isinstance(out, dict):
        return -1, -1
    items = [x for x in out.get("items") or [] if isinstance(x, dict)]
    filled = sum(
        x.get(k) not in (None, "") for x in items for k in ("date", "qty", "price", "total")
    )
    return len(items), filled


def _read_block(blk: str, today: str, base: str) -> dict | None:
    """One block through the text stage.

    - A short block is read twice at once and the fuller reading wins: about
      one read in twenty left a plain typed holding out, without saying why
      (eval, 2026-10-06), and the second read costs seconds.
    - The prompt forbids leaving out an entry out of doubt, yet about one
      read in eight did ("the ticker can't be confirmed"): such a block is
      read once more, naming what was dropped."""
    if blk.count("\n") + 1 > _TWICE_LINES:
        out = _structure(blk, today, base)
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.map(lambda _: _structure(blk, today, base), range(2))
        out = a if _score(a) >= _score(b) else b
    unsure = [str(s) for s in (out or {}).get("skipped") or [] if _UNSURE.search(str(s))]
    if unsure:
        again = _structure(blk + "\n\nLast time these entries were left out; include each one "
                           "as an item with your best guess:\n- " + "\n- ".join(unsure), today, base)  # fmt: skip
        if _score(again) > _score(out):
            out = again
    return out


def _chunks(block: str) -> list[str]:
    """Long text split into calls of at most _CHUNK_LINES rows; a CSV header
    (the first line of a sheet) goes with every chunk so columns stay named."""
    lines = [ln for ln in block.split("\n") if ln.strip()]
    if len(lines) <= _CHUNK_LINES:
        return ["\n".join(lines)] if lines else []
    head, rest = lines[0], lines[1:]
    step = _CHUNK_LINES - 1
    return ["\n".join([head, *rest[i : i + step]]) for i in range(0, len(rest), step)]


# --------------------------------------------------------------- matching


def _exch_ccy(hit) -> str | None:
    return _EXCH_CCY.get(hit.exchange or "") if hit else None


def alias_key(name: str | None) -> str:
    """How a name as written is remembered: lower-case words only, so
    "Shell", "SHELL" and "Shell " are one alias."""
    return " ".join(re.findall(r"[a-z0-9]+", (name or "").lower()))[:60]


def match_listing(
    name: str, ticker: str, ccy: str | None, aliases: dict | None = None
) -> tuple[object | None, list, bool]:
    """(the listing, alternatives, guessed) for a row. A name the owner has
    corrected before wins outright (``aliases``, saved on Apply). Otherwise
    the row's currency picks among a company's listings; with no currency
    the pack's ranking stands and the same company's other listings become
    a question."""
    from convexity import symbol_db

    remembered = (aliases or {}).get(alias_key(name))
    if remembered:
        hit = symbol_db.get(remembered)
        if hit is not None:
            return hit, [], False
    ccy = major_ccy(ccy) if ccy else None
    seen: dict[str, object] = {}

    def add(h):
        if (
            h is not None
            and h.ticker not in seen
            and (h.type or "stock") in ("stock", "etf", "fund")
        ):
            seen[h.ticker] = h

    t = re.sub(r"[^A-Za-z0-9.\-]", "", ticker or "").upper()
    base = t.split(".")[0] if t else ""
    if t:
        add(symbol_db.get(t))
    if base and ccy:
        for suf in _CCY_SUFFIX.get(ccy, ()):
            add(symbol_db.get(base + suf))
    if base and base != t:
        add(symbol_db.get(base))
    if name:
        for h in symbol_db.lookup(name, limit=6, min_score=80.0):
            add(h)
    if t and not seen:
        for h in symbol_db.lookup(t, limit=3, min_score=90.0):
            add(h)
    cands = list(seen.values())
    if not cands:
        return None, [], True
    if ccy:
        same = [h for h in cands if _exch_ccy(h) == ccy]
        if same:
            exact = [h for h in same if h.ticker.split(".")[0] == base] if base else []
            pick = (exact or same)[0]
            stem = _stem(pick.name)
            return (
                pick,
                [h for h in cands if h is not pick and _stem(h.name) == stem][:3],
                not exact,
            )
    pick = cands[0]
    exact = bool(t) and pick.ticker == t
    # Only the same company on another exchange is a real alternative (a
    # name-alike like Apple Hospitality is not worth a question).
    stem = _stem(pick.name)
    others = [h for h in cands[1:6] if _exch_ccy(h) != _exch_ccy(pick) and _stem(h.name) == stem]
    return pick, others[:3], not exact


_CORP_WORDS = frozenset(["the", "inc", "incorporated", "corp", "corporation", "co", "company", "plc", "ltd", "limited", "holding", "holdings", "group", "nv", "sa", "ag", "se", "spa", "ab", "asa", "oyj", "adr", "ads", "class"])  # fmt: skip


def _stem(name: str | None) -> str:
    """A company's name without its legal form, so "Shell plc" on London and
    New York match while "Apple Hospitality REIT" is not "Apple Inc."."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return " ".join(w for w in words if w not in _CORP_WORDS and len(w) > 1 or w.isdigit())


# ------------------------------------------------------------------ build


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def _iso(d: str) -> str:
    try:
        x = date.fromisoformat((d or "").strip()[:10])
    except ValueError:
        return ""
    return x.isoformat() if date(1970, 1, 1) <= x <= date.today() else ""


def _review_item(i: int, it: dict, base: str, aliases: dict | None = None) -> dict:
    """One model row -> one review row: typed, matched, checked."""
    from convexity.investments import _exchange_name

    t = it.get("type") if it.get("type") in TYPES else None
    cur = (it.get("currency") or "").strip()
    ccy_raw = cur.upper()
    # Pence: Yahoo writes GBp, brokers GBX; the case matters (GBP is pounds).
    pence = cur in ("GBp", "GBX", "gbx", "GBx") or cur.lower() in ("pence", "p")
    ccy = "GBP" if pence else (ccy_raw if re.fullmatch(r"[A-Z]{3}", ccy_raw) else None)
    unit = 0.01 if pence else 1.0
    qty, price, fee = _num(it.get("qty")), _num(it.get("price")), _num(it.get("fee"))
    total, tax, ratio = _num(it.get("total")), _num(it.get("tax")), _num(it.get("ratio"))
    row = {"i": i, "type": t, "date": _iso(it.get("date") or ""), "symbol": None, "name": it.get("name") or "",
           "exchange": None, "qty": None, "price": None, "ccy": ccy, "amount": None, "fee": None,
           "tax": None, "ratio": None, "source": (it.get("source") or "")[:60], "guessed": [],
           "missing": [], "checks": [], "fix": None, "alts": [], "include": True,
           "as_read": (it.get("name") or "")[:60]}  # fmt: skip
    if t == "holding" and it.get("date_guessed") and row["date"] == date.today().isoformat():
        row["date"] = ""  # "since today" is the question's default, not a reading
    if it.get("date_guessed") and row["date"]:
        row["guessed"].append("date")
    if t in ("buy", "sell", "dividend", "split", "holding"):
        remembered = alias_key(row["name"]) in (aliases or {})
        hit, alts, guessed = match_listing(row["name"], it.get("ticker") or "", ccy, aliases)
        if hit is None:
            row["missing"].append("symbol")
        else:
            row |= {
                "symbol": hit.ticker,
                "name": hit.name,
                "exchange": _exchange_name(hit.exchange),
            }
            row["alts"] = [{"symbol": h.ticker, "name": h.name, "exchange": _exchange_name(h.exchange)}
                           for h in alts]  # fmt: skip
            # A name the owner corrected before is theirs, not a guess.
            if not remembered and (guessed or it.get("ticker_guessed")):
                row["guessed"].append("symbol")
            if not ccy:
                row["ccy"] = _exch_ccy(hit)
                if row["ccy"]:
                    row["guessed"].append("ccy")
    if t in ("buy", "sell", "holding"):
        row["qty"] = qty
        row["price"] = price * unit if price is not None else None
        row["fee"] = fee * unit if fee is not None and pence else fee
        if qty is None:
            row["missing"].append("qty")
        if price is None:
            row["missing"].append("price")
        tc = (it.get("total_currency") or "").strip().upper()
        same_ccy = not tc or tc == ccy_raw or (pence and tc == "GBP")
        if qty and price is not None and total and same_ccy:
            tot = total * (unit if tc != "GBP" else 1.0) if pence else total
            gross = qty * row["price"]
            # Rounding allowance: a price shown to the cent is off by at most
            # half a cent a share, plus a cent on the total. A misread digit
            # (405.05 for 405.00 on 20 shares: $1) is well outside it.
            tol = 0.005 * qty + 0.011
            ok = any(abs(x - tot) <= tol for x in
                     (gross, gross + (row["fee"] or 0), gross - (row["fee"] or 0)))  # fmt: skip
            if not ok:
                implied = round(tot / qty, 4)
                row["checks"].append(
                    f"{qty:g} × {row['price']:,.2f} = {gross:,.2f}, but the total shown is {tot:,.2f}"
                )
                row["fix"] = {"price": implied}
    elif t in ("dividend", "deposit", "withdrawal", "fee"):
        amt = _num(it.get("total")) if _num(it.get("total")) is not None else None
        amt = (
            amt if amt is not None else (qty * price if t == "dividend" and qty and price else None)
        )
        row["amount"] = abs(amt) * unit if amt is not None else None
        row["tax"] = tax
        if row["amount"] is None:
            row["missing"].append("amount")
        if not row["ccy"]:
            row["ccy"] = base if t != "dividend" else None
            if t != "dividend":
                row["guessed"].append("ccy")
    elif t == "split":
        row["ratio"] = ratio
        if not ratio:
            row["missing"].append("ratio")
    else:
        row["missing"].append("type")
    if not row["date"]:
        row["missing"].append("date")
    return row


def _questions(rows: list[dict]) -> list[dict]:
    """At most three one-click questions, each with its default chosen."""
    qs = []
    snapshot = any(r["type"] == "holding" for r in rows)
    held = [r for r in rows if "date" in r["missing"]
            and (r["type"] == "holding" or (snapshot and r["type"] == "deposit"))]  # fmt: skip
    if held:
        qs.append({"id": "since", "text": f"Since when have you held {'these' if len(held) > 1 else 'this'}?",
                   "hint": "Performance counts from this date. Today is fine if you don't know.",
                   "options": [{"value": "today", "label": "Today"}, {"value": "pick", "label": "Pick a date"}],
                   "default": "today", "rows": [r["i"] for r in held]})  # fmt: skip
    for r in rows:
        if len(qs) >= 3:
            break
        if r["symbol"] and r["alts"] and "symbol" in r["guessed"] and "ccy" in r["guessed"]:
            opts = [
                {"value": r["symbol"], "label": f"{r['exchange'] or r['symbol']} · {r['symbol']}"}
            ]
            opts += [
                {"value": a["symbol"], "label": f"{a['exchange'] or a['symbol']} · {a['symbol']}"}
                for a in r["alts"][:2]
            ]
            qs.append({"id": f"listing-{r['i']}", "text": f"Which {r['name']} is it?", "hint": "",
                       "options": opts, "default": r["symbol"], "rows": [r["i"]]})  # fmt: skip
    return qs[:3]


def _summary(rows: list[dict], n_img: int, n_sheet: int, has_text: bool) -> str:
    words = {"buy": ("buy", "buys"), "sell": ("sale", "sales"), "dividend": ("dividend", "dividends"),
             "deposit": ("deposit", "deposits"), "withdrawal": ("withdrawal", "withdrawals"),
             "fee": ("fee", "fees"), "split": ("split", "splits"), "holding": ("holding", "holdings")}  # fmt: skip
    counts: dict[str, int] = {}
    for r in rows:
        if r["type"]:
            counts[r["type"]] = counts.get(r["type"], 0) + 1
    what = ", ".join(f"{n} {words[t][n != 1]}" for t, n in counts.items()) or "nothing to add"
    src = [f"{n_img} screenshot{'s' * (n_img != 1)}"] if n_img else []
    src += [f"{n_sheet} file{'s' * (n_sheet != 1)}"] if n_sheet else []
    src += ["your text"] if has_text else []
    return f"Found {what} in {' and '.join(src)}."


def read(
    text: str = "",
    files=None,
    *,
    base: str = "USD",
    aliases: dict | None = None,
    held: dict | None = None,
) -> dict:
    """The page's "Read it": everything given -> a proposal to review.
    ``aliases`` are the owner's earlier name corrections; ``held`` is what
    the book holds now (shares by ticker), so a holdings list is reconciled
    against it instead of doubling what is already there."""
    from convexity import news_sentiment as ns

    text = (text or "").strip()
    if len(text) > MAX_TEXT:
        raise ImportRefused(
            f"That's more than {MAX_TEXT:,} characters. Split it, or add the file instead."
        )
    got = _decode_files(files)
    if not text and not got:
        raise ImportRefused("Add a screenshot or a file, or type what you hold.")
    if sum(len(f["data"]) for f in got) > 20 * 1024 * 1024:
        raise ImportRefused("Those files add up to more than 20 MB. Add fewer at a time.")
    if not ns.NVIDIA_API_KEY:
        raise ImportRefused(
            "Reading needs an NVIDIA key. Add one in Settings → API keys, or add holdings by hand.",
            409,
        )
    t0 = time.time()
    images = [f for f in got if f["kind"] == "image"]
    sheets = [f for f in got if f["kind"] == "sheet"]
    blocks: list[str] = []
    n_pdf = 0
    for f in (f for f in got if f["kind"] == "pdf"):
        # Text pages are read as text; scanned pages join the screenshots.
        n_pdf += 1
        body, scans = pdf_parts(f["name"], f["data"])
        blocks += [f"File {f['name']}, statement text:\n{c}" for c in _chunks(body)]
        images += [{"name": f"{f['name']} (scanned page {k + 1})", "mime": "image/png", "data": png}
                   for k, png in enumerate(scans)]  # fmt: skip
    if len(images) > MAX_FILES * 2:
        raise ImportRefused("That's too many pages to read at once. Add fewer files.")
    if images:
        with ThreadPoolExecutor(max_workers=min(3, len(images))) as pool:
            outs = list(pool.map(lambda a: _vision_call(a[1]["data"], a[1]["mime"], f"Image {a[0] + 1}"),
                                 enumerate(images)))  # fmt: skip
        failed = [images[i]["name"] for i, o in enumerate(outs) if not o]
        # Refuse only when nothing at all is left to read: a PDF's text pages
        # (already in ``blocks``), files or typed text still go ahead, and
        # the page names whatever couldn't be read (``unread``).
        if failed and len(failed) == len(images) and not (blocks or sheets or text):
            raise ImportRefused(
                "The screenshots couldn't be read right now. Try again in a minute.", 503
            )
        blocks += [f"Image {i + 1} ({images[i]['name']}):\n{o}" for i, o in enumerate(outs) if o]
    else:
        failed = []
    for f in sheets:
        for label, body in sheet_parts(f["name"], f["data"]):
            blocks += [f"File {label}, csv rows:\n{c}" for c in _chunks(body)]
    if text:
        blocks += [f"Typed or pasted text:\n{c}" for c in _chunks(text)]
    t1 = time.time()
    today = date.today().isoformat()
    raw_items, skipped = [], []
    # Blocks are read a few at a time (order kept): a long statement is a
    # dozen 30-row chunks, each a NIM call of ~10 s.
    with ThreadPoolExecutor(max_workers=3) as pool:
        outs = list(pool.map(lambda blk: _read_block(blk, today, base), blocks))
    for blk, out in zip(blocks, outs, strict=True):
        if not isinstance(out, dict):
            raise ImportRefused("The reading step didn't answer. Try again in a minute.", 503)
        for x in out.get("items") or []:
            if isinstance(x, dict) and x.get("type") == "holding" and _num(x.get("qty")) is None:
                # A "holding" with no share count is a line the model
                # over-read (a portfolio total, an account number): never
                # bookable, so it goes to the left-out list, not the review.
                skipped.append(f"{(x.get('name') or 'a line')[:40]}: no share count")
            elif isinstance(x, dict):
                # A ticker counts as read only if it is in the source itself;
                # otherwise it is the model's guess, whatever it says.
                tk = re.escape((x.get("ticker") or "").strip())
                # Case-sensitive: tickers like ON, ALL or IT are ordinary words.
                if tk and not re.search(rf"(?<![A-Za-z0-9]){tk}(?![A-Za-z0-9])", blk):
                    x["ticker_guessed"] = True
                raw_items.append(x)
        skipped += [str(x)[:80] for x in out.get("skipped") or []][:10]
    rows = [_review_item(i, it, base, aliases) for i, it in enumerate(raw_items)]
    _reconcile(rows, held or {})
    print(f"[import] read {len(images)} image(s), {len(sheets) + n_pdf} file(s), text={bool(text)}: "
          f"{len(rows)} row(s), files and screenshots {t1 - t0:.1f}s, total {time.time() - t0:.1f}s", flush=True)  # fmt: skip
    return {
        "summary": _summary(
            rows,
            len(images) - sum("scanned page" in i["name"] for i in images),
            len(sheets) + n_pdf,
            bool(text),
        ),  # fmt: skip
        "items": rows,
        "questions": _questions(rows),
        "skipped": skipped[:10],
        "unread": failed,
    }


def _reconcile(rows: list[dict], held: dict) -> None:
    """A holdings list against the book: a holding already there with the
    same shares is left out by default; a different count says so, and the
    page offers to add only the difference. Never applied silently."""
    for r in rows:
        if r["type"] != "holding" or not r["symbol"] or r["symbol"] not in held:
            continue
        have, qty = held[r["symbol"]], r["qty"]
        r["book_qty"] = have
        if qty is not None and abs(have - qty) <= 1e-6 * max(1.0, qty):
            r["include"] = False
            r["checks"].append(f"Already in your book ({have:g} shares)")
        elif qty is not None:
            r["checks"].append(f"Your book has {have:g} shares; this shows {qty:g}")
            if qty > have:
                r["fix"] = {"qty": round(qty - have, 6)}
