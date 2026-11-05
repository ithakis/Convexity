"""Which portfolio My Investments shows, and where each one's files live.

The first portfolio stays exactly where the single book always was
(``state/investments.json`` and its neighbours); every other one has the same
file names in ``state/books/<id>/``. ``state/books.json`` is the list:
``{enabled, active, books: [{id, name, created_at}]}``. No file means one
portfolio, ``main``, switch off, and nothing is written until the owner changes
something. A damaged file reads as that too and is never overwritten.

``book_file`` is the one seam. A request or a job pins the portfolio it started
on (``pin``), so a switch halfway through can't send its reads or writes to
another portfolio's files. Logs carry ids and operations only.
"""

import contextvars
import json
import os
import re
import secrets
import threading
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime

from convexity import paths
from convexity.persistence import _atomic_write

MAIN = "main"
NAME_MAX = 20
_ID = re.compile(r"[0-9a-f]{12}")
_LOCK = threading.Lock()
_PIN: contextvars.ContextVar = contextvars.ContextVar("convexity_book", default=None)


class BookMoved(Exception):
    """A change sent for a portfolio that is no longer showing."""

    status = 409
    message = "The portfolio showing changed in another window, so nothing was saved."


class Refused(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _implicit() -> dict:
    return {"enabled": False, "active": MAIN,
            "books": [{"id": MAIN, "name": "Mine", "created_at": None}]}  # fmt: skip


def _sound(raw) -> bool:
    if not isinstance(raw, dict) or not isinstance(raw.get("enabled"), bool):
        return False
    rows = raw.get("books")
    if not isinstance(rows, list) or not rows or not all(isinstance(b, dict) for b in rows):
        return False
    ids = [b.get("id") for b in rows]
    return (ids[0] == MAIN and len(set(map(str, ids))) == len(ids) and raw.get("active") in ids
            and all(isinstance(b.get("name"), str) and b["name"].strip() for b in rows)
            and all(isinstance(i, str) and _ID.fullmatch(i) for i in ids[1:]))  # fmt: skip


def _read() -> dict | None:
    """The list; ``None`` when the file can't be trusted."""
    try:
        raw = json.loads(paths.state_file("books").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _implicit()
    except (OSError, ValueError):
        return None
    return raw if _sound(raw) else None


def state() -> dict:
    st = _read()
    return {**st, "unreadable": False} if st else {**_implicit(), "unreadable": True}


def showing() -> str:
    """The portfolio on screen, whatever this thread has pinned."""
    st = _read()
    return st["active"] if st and st["enabled"] else MAIN


def active_id() -> str:
    """The portfolio this request or job works on."""
    return _PIN.get() or showing()


def is_showing() -> bool:
    return active_id() == showing()


def several() -> bool:
    """More than one exists, the switch on or off."""
    return len(state()["books"]) > 1


def expect(sent) -> str:
    """The portfolio a change names (an older page names none: the first), or
    409. Revs can't tell: two portfolios can share one."""
    book_id = sent or MAIN
    if book_id != showing():
        print(f"[books] refused a change for {str(book_id)[:12]}: not showing")
        raise BookMoved
    return book_id


@contextmanager
def pin(book_id: str | None = None):
    token = _PIN.set(book_id or active_id())
    try:
        yield
    finally:
        _PIN.reset(token)


def book_dir(book_id: str):
    if book_id == MAIN:
        return paths.state_dir()
    if not isinstance(book_id, str) or not _ID.fullmatch(book_id):
        raise ValueError("bad portfolio id")
    return paths.state_dir() / "books" / book_id


def book_file(name: str):
    """``book_file("plans")``: that file of the portfolio this thread works on."""
    book_id = active_id()
    return paths.state_file(name) if book_id == MAIN else book_dir(book_id) / f"{name}.json"


def _many() -> bool:
    st = state()
    return st["enabled"] and len(st["books"]) > 1


def _this(text: str) -> str:
    return text.replace("your book", "this portfolio").replace("Your book", "This portfolio")


def phrase(text: str) -> str:
    """Our own sentence: with several portfolios "your book" is "this portfolio"."""
    return _this(text) if _many() else text


def words(att: dict) -> dict:
    """The rail, a copy. Only each item's ``text``, which is ours: a plan's note
    (``sub``, ``plans``) is the owner's own words and stays as typed."""
    if not _many():
        return att
    return {**att, **{k: [{**it, "text": _this(it["text"])} for it in att[k]]
                      for k in ("high", "info", "near") if k in att}}  # fmt: skip


# ------------------------------------------------------------------- changes


def _change(apply) -> dict:
    with _LOCK:
        st = _read()
        if st is None:
            raise Refused("The list of portfolios (state/books.json) is damaged, so it was left "
                          "untouched. Repair or remove it to make changes.", 409)  # fmt: skip
        apply(st)
        _atomic_write(
            paths.state_file("books"), json.dumps(st, ensure_ascii=False, indent=1) + "\n"
        )
        return st


def _row(st: dict, book_id) -> dict:
    for b in st["books"]:
        if b["id"] == book_id:
            return b
    raise Refused("That portfolio no longer exists.", 404)


def _name(st: dict, name, own=None) -> str:
    clean = " ".join(name.split()) if isinstance(name, str) else ""
    if not clean or len(clean) > NAME_MAX:
        raise Refused(f"Give it a name of at most {NAME_MAX} characters.")
    if any(unicodedata.category(c).startswith("C") for c in clean):
        raise Refused("Use letters, numbers and punctuation in the name.")
    if any(b["name"].casefold() == clean.casefold() and b["id"] != own for b in st["books"]):
        raise Refused("You already have a portfolio with that name.")
    return clean


def set_enabled(on: bool) -> None:
    """Off deletes nothing: the first portfolio shows, the others are kept."""

    def apply(st):
        st["enabled"] = on
        if not on:
            st["active"] = MAIN

    _change(apply)
    print(f"[books] switch {'on' if on else 'off'}")


def add(name) -> None:
    """A new, empty portfolio, showing at once. Its folder appears with its first write."""
    book_id = secrets.token_hex(6)

    def apply(st):
        if not st["enabled"]:
            raise Refused("Turn on Multiple portfolios first.")
        st["books"].append({"id": book_id, "name": _name(st, name),
                            "created_at": datetime.now(UTC).strftime("%Y-%m-%d")})  # fmt: skip
        st["active"] = book_id

    _change(apply)
    print(f"[books] added {book_id}")


def rename(book_id, name) -> None:
    def apply(st):
        _row(st, book_id)["name"] = _name(st, name, book_id)

    _change(apply)
    print(f"[books] renamed {book_id}")


def show(book_id) -> None:
    def apply(st):
        _row(st, book_id)
        if not st["enabled"]:
            raise Refused("Turn on Multiple portfolios first.")
        st["active"] = book_id

    _change(apply)
    print(f"[books] showing {book_id}")


def delete(book_id) -> None:
    """Out of the list, then its folder moves to ``books/deleted/<id>-<when>/``.
    The first portfolio and the one showing are refused, so one always remains.
    A failed move leaves the folder where it is: unlisted, nothing lost."""

    def apply(st):
        row = _row(st, book_id)
        if row["id"] == MAIN:
            raise Refused("The first portfolio can't be deleted. Clear it instead.")
        if row["id"] == st["active"]:
            raise Refused("Show another portfolio first.")
        st["books"].remove(row)

    _change(apply)
    src = book_dir(book_id)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    try:
        if src.exists():
            dest = paths.state_dir() / "books" / "deleted" / f"{book_id}-{stamp}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)
    except OSError as exc:
        print(f"[books] keeping the folder of {book_id}: {type(exc).__name__}")
    print(f"[books] deleted {book_id}")
