"""Multiple My Investments portfolios (books.py): the first stays where the one
book always was, each other is sealed in its own folder, and nothing started
for one portfolio can land in another. Public tickers, no network."""

import http.server
import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest

from convexity import attention, books, jobs, long_run, notify, paths, persistence, server
from convexity import investments as inv

BUY = {"date": "2024-01-02", "type": "buy", "symbol": "AAPL", "qty": 1, "price": 100.0}


def _page(symbol="AAPL", value=1000.0):
    return {"empty": False, "ccy": "USD", "rev": 1, "series": {"dates": ["2024-01-02"]},
            "headline": {"value": value, "cash_weight": 0.0}, "positions": [
                {"symbol": symbol, "name": symbol, "weight": 100.0, "value": value,
                 "quote_ccy": "USD", "price": 10.0, "return_pct": 1.0, "xray": {}}]}  # fmt: skip


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    inv._PAGE_MEMO.clear()
    inv._LAST_PAGE["v"] = (None, None)
    persistence._write_views_raw({})  # views.json binds its path at import: shared across tests
    monkeypatch.setattr(inv, "_fetch_quote_ccy", lambda s: "USD")
    monkeypatch.setattr(inv, "_pack_hit", lambda t: None)
    monkeypatch.setattr(inv, "_pack_kind", lambda s, n=None: {"sector": None, "fund": False})
    monkeypatch.setattr(inv, "_resolve", lambda text: text.strip().upper())
    monkeypatch.setattr(inv, "_fetch_splits", lambda s: [])
    monkeypatch.setattr(inv, "_fx_now", lambda ccy, base: 1.0)
    # The ledger's replay stands in for the prices it would fetch.
    monkeypatch.setattr(inv, "book", lambda ccy=None: _page())
    yield
    inv._PAGE_MEMO.clear()
    inv._LAST_PAGE["v"] = (None, None)


@pytest.fixture
def srv():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _post(base, path, body):
    req = urllib.request.Request(base + path, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=15) as r:
        return json.loads(r.read())


def _partner():
    books.set_enabled(True)
    books.add("Partner")
    return books.showing()


def _state_files():
    root = paths.state_dir()
    return (
        sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        if root.exists()
        else []
    )


# ----------------------------------------------------------------- the seam


def test_no_registry_is_one_implicit_portfolio_and_writes_nothing():
    assert books.showing() == books.active_id() == books.MAIN
    assert books.state()["books"][0]["name"] == "Mine" and not books.state()["enabled"]
    assert inv._path() == paths.state_file("investments")
    assert inv._cache_path() == paths.state_file("book_cache")
    assert attention._state_path() == paths.state_file("attention")
    assert attention._plans_path() == paths.state_file("plans")
    assert notify._recaps_path() == paths.state_file("recaps")
    assert books.expect(None) == books.MAIN
    assert inv.books_state(True)["books"][0]["holdings"] == 0
    assert books.phrase("of your book") == "of your book"
    assert _state_files() == []


def test_other_books_use_the_same_names_in_their_own_folder():
    pid = _partner()
    assert inv._path() == paths.state_dir() / "books" / pid / "investments.json"
    assert attention._plans_path().parent == inv._path().parent
    with books.pin(books.MAIN):
        assert inv._path() == paths.state_file("investments")
    for shared in ("splits", "events", "long_run", "sentiment_history"):
        assert paths.state_file(shared).parent == paths.state_dir()


def test_bad_ids_cannot_leave_the_folder():
    for bad in ("../../etc", "..", "a/b", "", "ABCDEF012345", 5, "0" * 13):
        with pytest.raises((ValueError, books.Refused)):
            if bad == "":
                raise ValueError
            books.book_dir(bad)
    with books.pin("../../etc"), pytest.raises(ValueError):
        books.book_file("investments")
    good = {"enabled": True, "active": "main", "books": [{"id": "main", "name": "Mine"}]}
    bad = {**good, "books": [*good["books"], {"id": "../../x", "name": "Bad"}]}
    paths.state_file("books").parent.mkdir(parents=True)
    paths.state_file("books").write_text(json.dumps(bad))
    assert books.state()["unreadable"] and books.showing() == books.MAIN


@pytest.mark.parametrize("body", ["{not json", "[]", '{"enabled": true}',
                                  '{"enabled": true, "active": "x", "books": [{"id": "main", "name": "M"}]}'])  # fmt: skip
def test_a_malformed_registry_is_never_overwritten_and_refuses_writes(body):
    path = paths.state_file("books")
    path.parent.mkdir(parents=True)
    path.write_text(body)
    assert books.showing() == books.MAIN and books.state()["unreadable"]
    assert inv._path() == paths.state_file("investments")
    for change in (
        lambda: books.set_enabled(True),
        lambda: books.add("X"),
        lambda: books.show("main"),
    ):
        with pytest.raises(books.Refused) as err:
            change()
        assert err.value.status == 409
    assert path.read_text() == body


# ---------------------------------------------------------------- isolation


def test_two_books_are_isolated():
    inv.add(BUY, 0)
    attention.dismiss("holding|AAPL", 5)
    attention.set_plan("AAPL", {"why": "Mine's reason"}, 10.0)
    notify._save_recap({"week_end": "2024-01-05"})
    main_book = inv._path().read_text()
    pid = _partner()
    assert inv._load() is None
    assert attention.load_state()["dismissed"] == {}
    assert attention.load_plans() == {}
    assert notify.load_recaps() == {}
    assert inv.cached_page("USD") is None
    inv.add({**BUY, "symbol": "MSFT"}, 0)
    attention.dismiss("holding|MSFT", 5)
    assert inv.book_symbols() == ["MSFT"]
    books.show(books.MAIN)
    assert inv.book_symbols() == ["AAPL"] and inv._path().read_text() == main_book
    assert "holding|MSFT" not in attention.load_state()["dismissed"]
    assert "AAPL" in attention.load_plans()
    books.show(pid)
    assert "holding|AAPL" not in attention.load_state()["dismissed"]


def test_switching_keeps_dismissals_through_a_prune():
    attention.dismiss("holding|AAPL", 5)
    pid = _partner()
    # B's healthy page prunes what isn't firing there, in B's file only.
    attention.prune(set(), {"holding|AAPL"})
    books.show(books.MAIN)
    assert "holding|AAPL" in attention.load_state()["dismissed"]
    books.show(pid)
    attention.dismiss("holding|MSFT", 5)
    books.show(books.MAIN)
    attention.prune({"holding|AAPL"}, {"holding|AAPL"})
    books.show(pid)
    assert "holding|MSFT" in attention.load_state()["dismissed"]


# ------------------------------------------------------------ work in flight


def test_a_page_computed_for_one_book_is_not_kept_or_served_for_another(monkeypatch):
    inv.add(BUY, 0)
    inv._PAGE_MEMO.clear()
    inv._LAST_PAGE["v"] = (None, None)
    paths.state_file("book_cache").unlink()

    def slow(ccy=None):
        _partner()  # the owner switches while it computes
        return _page()

    monkeypatch.setattr(inv, "book", slow)
    out = inv.page("USD")
    assert out["positions"][0]["symbol"] == "AAPL"
    assert not inv._PAGE_MEMO and inv._LAST_PAGE["v"] == (None, None)
    assert not paths.state_file("book_cache").exists()
    assert not books.book_file("book_cache").exists()
    assert inv.computed_page("USD") is None and inv.last_page() is None


def test_memo_and_last_page_do_not_cross_books():
    inv.add(BUY, 0)
    assert inv.page("USD")["positions"][0]["symbol"] == "AAPL"
    assert inv.price_now("AAPL", "USD") == 10.0
    assert inv.cached_page("USD")
    _partner()
    assert inv.last_page() is None
    assert inv.computed_page("USD") is None and inv.cached_page("USD") is None
    assert inv.price_now("AAPL", "USD") is None
    assert inv.attention_now("USD")["page"] is False
    books.show(books.MAIN)
    assert inv.cached_page("USD")["positions"][0]["symbol"] == "AAPL"


def test_a_pinned_write_lands_in_its_own_book_after_a_switch():
    with books.pin(books.MAIN):
        _partner()
        inv.add(BUY, 0)
        assert inv._path() == paths.state_file("investments")
    assert inv._load() is None
    with books.pin(books.MAIN):
        assert inv.book_symbols() == ["AAPL"]


def test_no_notification_for_a_book_no_longer_showing(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(notify, "settings", lambda: {"notify_on": True, "quiet_on": False,
                                                     "quiet_from": "22:00", "quiet_to": "08:00"})  # fmt: skip
    attention._update(lambda st: st.update(armed=True))
    att = {"page": True, "high": [{"key": "holding|AAPL", "level": 20.0, "text": "AAPL", "rule": "holding",
                                   "severity": "high", "unread": True}], "info": []}  # fmt: skip
    with books.pin(books.MAIN):
        _partner()
        assert notify.notify_rail(att) == 0
    assert sent == []


def test_the_switch_cancels_the_books_jobs_but_not_a_research_refresh(monkeypatch):
    seen = {}
    release = threading.Event()

    def run(job):
        seen["book"] = books.active_id()
        release.wait(5)

    monkeypatch.setattr(jobs, "_run", run)
    job, _ = jobs.submit(scope="current", phases=["longrun"], days=7,
                         entries_by_view={jobs.BOOK_VIEW: ""}, context={"background": True})  # fmt: skip
    inv.change_books(lambda: _partner())
    assert job.cancel.is_set()
    release.set()
    job._thread.join(2)
    assert seen["book"] == books.MAIN
    job.state = "cancelled"
    release.clear()
    research, _ = jobs.submit(
        scope="current", phases=["quotes"], days=7, entries_by_view={"Tech": "AAPL"}
    )
    inv.change_books(lambda: books.show(books.MAIN))
    assert not research.cancel.is_set()
    release.set()
    research._thread.join(2)
    research.state = "done"


def test_the_switch_answers_with_the_new_books_page_in_one_go():
    inv.add(BUY, 0)
    inv.page("USD")
    out = inv.change_books(lambda: _partner(), "USD")
    assert out["symbols"] == [] and out["page"].get("positions") is not None
    back = inv.change_books(lambda: books.show(books.MAIN), "USD")
    assert back["symbols"] == ["AAPL"] and back["page"]["stale"]
    assert inv.change_books(lambda: books.rename(books.MAIN, "Ours"), "USD").get("page") is None


def test_held_notifications_and_analytics_go_with_the_switch():
    persistence.save_view("__book__", "AAPL", [{"symbol": "AAPL"}], set_last=False)
    persistence.upsert_analytics_cache("__book__", "k", {"a": 1})
    notify._HELD.update(items=True, recap={"week_end": "2024-01-05"})
    attention._update(lambda st: st.update(armed=True))
    inv.change_books(lambda: _partner())
    assert notify._HELD == {"items": False, "recap": None}
    assert persistence.get_analytics_cache("__book__") == {}
    inv.change_books(lambda: books.show(books.MAIN))
    assert attention.load_state()["armed"] is False  # back on the rail: read there, not sent


def test_a_stale_save_cannot_overwrite_the_mirror():
    inv.add(BUY, 0)
    pid = _partner()
    inv.add({**BUY, "symbol": "MSFT"}, 0)
    assert persistence.save_view("__book__", "AAPL", [{"symbol": "AAPL"}], set_last=False) == {}
    assert persistence.load_view("__book__") == {}
    assert persistence.save_view("__book__", "MSFT", [{"symbol": "MSFT"}], set_last=False)
    assert persistence.load_view("__book__")["entries"] == "MSFT"
    # A job pinned to the old book still checks against what shows.
    with books.pin(books.MAIN):
        assert persistence.save_view("__book__", "AAPL", [{"symbol": "AAPL"}], set_last=False) == {}
    assert books.showing() == pid


def test_a_single_book_saves_its_mirror_whatever_it_says():
    assert persistence.save_view("__book__", "AAPL", [{"symbol": "AAPL"}], set_last=False)


def test_long_run_matrix_is_per_book_and_main_keeps_its_key():
    long_run.reset_memo()
    positions = [
        {"symbol": "AAPL", "value": 50.0, "weight": 50.0},
        {"symbol": "MSFT", "value": 50.0, "weight": 50.0},
    ]
    m = {"month": "2024-01", "symbols": ["AAPL", "MSFT"], "asked": ["AAPL", "MSFT"],
         "cov": (np.eye(2) * 0.04).tolist()}  # fmt: skip
    assert long_run._cov_key("EUR") == "EUR"
    long_run._update(cov={"EUR": m})
    assert long_run.book_view(positions, "EUR")
    pid = _partner()
    assert long_run._cov_key("EUR") == f"{pid}|EUR"
    assert not long_run.book_view(positions, "EUR")
    long_run._update(cov={long_run._cov_key("EUR"): m})
    assert long_run.book_view(positions, "EUR")
    books.show(books.MAIN)
    long_run.forget_book(pid)
    assert set(long_run.load()["cov"]) == {"EUR"}
    long_run.reset_memo()


# ----------------------------------------------------- the list and its rules


def test_first_and_showing_cannot_be_deleted_and_delete_keeps_a_copy():
    pid = _partner()
    inv.add(BUY, 0)
    with pytest.raises(books.Refused, match="Show another"):
        books.delete(pid)
    books.show(books.MAIN)
    with pytest.raises(books.Refused, match="first portfolio"):
        books.delete(books.MAIN)
    folder = paths.state_dir() / "books" / pid
    body = (folder / "investments.json").read_text()
    books.delete(pid)
    assert not folder.exists() and [b["id"] for b in books.state()["books"]] == [books.MAIN]
    kept = list((paths.state_dir() / "books" / "deleted").glob(f"{pid}-*"))
    assert len(kept) == 1 and (kept[0] / "investments.json").read_text() == body


def test_delete_with_nothing_written_only_leaves_the_list():
    pid = _partner()
    books.show(books.MAIN)
    books.delete(pid)
    assert not (paths.state_dir() / "books").exists()


def test_turning_off_returns_to_the_first_and_keeps_the_others():
    pid = _partner()
    inv.add(BUY, 0)
    out = inv.change_books(lambda: books.set_enabled(False))
    assert out["active"] == books.MAIN and books.showing() == books.MAIN
    assert [b["id"] for b in books.state()["books"]] == [books.MAIN, pid]
    assert books.phrase("of your book") == "of your book"
    with pytest.raises(books.Refused):
        books.show(pid)
    books.set_enabled(True)
    books.show(pid)
    assert inv.book_symbols() == ["AAPL"]


def test_names_are_validated():
    books.set_enabled(True)
    for bad in ("", "   ", "x" * 21, None, 5, "mine"):
        with pytest.raises(books.Refused):
            books.add(bad)
    books.add("  Two   words ")
    assert books.state()["books"][1]["name"] == "Two words"
    books.rename(books.MAIN, "MINE")
    assert books.state()["books"][0]["name"] == "MINE"
    assert len(books.state()["books"][1]["id"]) == 12


def test_the_wording_helper_leaves_the_owners_words_alone():
    rail = {"high": [{"text": "12% of your book", "sub": "\u201cTrim your book\u201d"}],
            "info": [{"text": "Your book rose"}], "near": [{"text": "12% of your book, limit 15%"}],
            "plans": {"AAPL": {"why": "Trim your book"}}}  # fmt: skip
    assert books.words(rail) == rail
    assert books.phrase("Already in your book") == "Already in your book"
    _partner()
    out = books.words(rail)
    assert out["high"][0] == {"text": "12% of this portfolio", "sub": "\u201cTrim your book\u201d"}
    assert out["info"][0]["text"] == "This portfolio rose"
    assert out["near"][0]["text"] == "12% of this portfolio, limit 15%"
    assert out["plans"]["AAPL"]["why"] == "Trim your book"
    assert books.phrase("Your book has 3") == "This portfolio has 3"


def test_a_plan_note_reaches_the_rail_verbatim():
    _partner()
    inv.add(BUY, 0)
    attention.set_plan("AAPL", {"review": 50.0, "why": "Trim your book"}, 100.0)
    page = _page()
    page["positions"][0]["price"] = 10.0
    att = inv.with_attention(page)["attention"]
    review = next(it for it in att["high"] if it["rule"] == "review")
    assert "Trim your book" in review["sub"]
    assert att["plans"]["AAPL"]["why"] == "Trim your book"
    big = next(it for it in att["high"] if it["rule"] == "holding")
    assert "this portfolio" in big["text"]


@pytest.mark.parametrize("bad", ["A\u0000B", "A\u202eB", "A\u200bB", "A\tB\x7f"], ids=range(4))
def test_names_refuse_control_and_bidi_characters(bad):
    books.set_enabled(True)
    with pytest.raises(books.Refused, match="letters"):
        books.add(bad)
    books.add("Zo\u00eb \u00c9lodie")
    assert books.state()["books"][1]["name"] == "Zo\u00eb \u00c9lodie"


def test_facts_read_the_files_alone():
    inv.add(BUY, 0)
    inv.page("USD")
    _partner()
    main, other = inv.books_state(True)["books"]
    assert (main["holdings"], main["entries"], main["value"], main["ccy"]) == (1, 1, 1000.0, "USD")
    assert main["changed"]
    assert other["holdings"] == 0 and "value" not in other and other["created_at"]


# --------------------------------------------------------------------- routes


def test_a_write_for_the_wrong_book_is_409_even_when_revs_match(srv):
    code, _ = _post(srv, "/api/investments/entries", {"base_rev": 0, "entry": BUY})
    assert code == 200
    code, st = _post(srv, "/api/investments/books/enabled", {"on": True})
    assert code == 200 and st["enabled"]
    code, st = _post(srv, "/api/investments/books/add", {"name": "Partner"})
    assert code == 200 and st["active"] != books.MAIN and st["symbols"] == []
    # Both at rev 1 once the partner has one entry: only the id tells them apart.
    code, b = _post(
        srv, "/api/investments/entries", {"base_rev": 0, "entry": BUY, "book_id": st["active"]}
    )
    assert code == 200 and b["book_id"] == st["active"]
    before = paths.state_file("investments").read_text()
    for path, body in [("entries", {"base_rev": 1, "entry": BUY}), ("undo", {"base_rev": 1}),
                       ("clear", {"base_rev": 1}), ("import/apply", {"base_rev": 1, "entries": [BUY]}),
                       ("import/check", {"entries": []}), ("dividends", {"base_rev": 1, "entries": []}),
                       ("attention/dismiss", {"key": "k", "level": 5}),
                       ("attention/plan", {"symbol": "AAPL", "plan": {}})]:  # fmt: skip
        code, out = _post(srv, f"/api/investments/{path}", {**body, "book_id": books.MAIN})
        assert code == 409 and out["error"] == "BookMoved", path
    assert paths.state_file("investments").read_text() == before
    # An older page names no portfolio: it can only ever mean the first.
    assert _post(srv, "/api/investments/undo", {"base_rev": 1})[0] == 409


def test_overlapping_switches_never_pair_a_name_with_another_page(srv, monkeypatch):
    monkeypatch.setattr(inv, "book", lambda ccy=None: _page((inv.book_symbols() or ["AAPL"])[0]))
    inv.add(BUY, 0)
    inv.page("USD")
    pid = _partner()
    inv.add({**BUY, "symbol": "MSFT"}, 0)
    inv.page("USD")
    want = {books.MAIN: "AAPL", pid: "MSFT"}
    answers = []

    def show(book_id):
        answers.append(_post(srv, "/api/investments/books/show", {"id": book_id, "ccy": "USD"})[1])

    for _ in range(25):
        pair = [threading.Thread(target=show, args=(b,)) for b in (books.MAIN, pid)]
        for t in pair:
            t.start()
        for t in pair:
            t.join()
    moved = [a for a in answers if "page" in a]
    assert moved
    for a in moved:
        assert a["page"]["book_id"] == a["active"]
        assert a["symbols"] == [want[a["active"]]]
        assert a["page"]["positions"][0]["symbol"] == want[a["active"]]


def test_the_book_refresh_follows_the_portfolio_showing(srv, monkeypatch):
    inv.add(BUY, 0)
    _partner()
    inv.add({**BUY, "symbol": "MSFT"}, 0)
    seen = {}

    def submit(**kw):
        seen.update(kw)
        return None, "created"

    monkeypatch.setattr(jobs, "submit", submit)
    monkeypatch.setattr(
        server.Handler, "_send_refresh_job", lambda self, j, o: self._send_json(200, {})
    )
    body = {"view": "__book__", "book_id": books.showing(), "entries": "AAPL", "phases": ["quotes"]}
    _post(srv, "/api/refresh-job", body)
    assert seen["entries_by_view"] == {"__book__": "MSFT"}


def test_the_export_reads_one_portfolio(srv, monkeypatch):
    inv.add(BUY, 0)
    pid = _partner()
    books.show(books.MAIN)
    seen = {}

    def page(ccy=None):
        seen["page"] = books.active_id()
        books.show(pid)  # the owner switches while it reads
        return _page()

    def risk(ccy=None):
        seen["risk"] = books.active_id()
        return {}

    monkeypatch.setattr(inv, "page", page)
    monkeypatch.setattr(inv, "holding_risk", risk)
    monkeypatch.setattr("convexity.xlsx_export.build_workbook", lambda *a, **k: b"xlsx")
    with urllib.request.urlopen(srv + "/api/export-xlsx?ccy=USD", timeout=15) as r:
        assert r.read() == b"xlsx"
    assert seen == {"page": books.MAIN, "risk": books.MAIN}


def test_the_red_tab_repaints_with_the_name():
    """Static: booksFollow sets the constituents before its first await, and the
    client queues its requests and keeps only the newest answer."""
    from pathlib import Path

    js = (Path(server.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("async function booksFollow") :]
    body = body[: body.index("\n}\n")]
    before = body[: body.index("await ")]
    for must in ("DATA = []", "render()", "renderAnalyticsBody()", "setEntries(bookEntries())"):
        assert must in before, must
    assert "n === _booksAsked ? d : null" in js and "d.page?.book_id || d.active" in js


def test_routes_for_the_list(srv):
    assert _get(srv, "/api/investments/books")["enabled"] is False
    assert not paths.state_file("books").exists()
    _post(srv, "/api/investments/books/enabled", {"on": True})
    _, st = _post(srv, "/api/investments/books/add", {"name": "Partner"})
    pid = st["active"]
    assert _get(srv, "/api/investments?ccy=USD")["book_id"] == pid
    assert _post(srv, "/api/investments/books/delete", {"id": pid})[0] == 400
    assert _post(srv, "/api/investments/books/delete", {"id": books.MAIN})[0] == 400
    assert _post(srv, "/api/investments/books/show", {"id": "nope"})[0] == 404
    assert _post(srv, "/api/investments/books/nope", {})[0] == 404
    _, st = _post(srv, "/api/investments/books/show", {"id": books.MAIN})
    assert st["active"] == books.MAIN
    code, st = _post(srv, "/api/investments/books/delete", {"id": pid})
    assert code == 200 and len(st["books"]) == 1
    req = urllib.request.Request(srv + "/api/investments/books/enabled", method="POST",
                                 data=b'{"on": false}', headers={"Content-Type": "text/plain"})  # fmt: skip
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=15)
    assert err.value.code == 403


def test_a_damaged_list_answers_409(srv):
    paths.state_file("books").parent.mkdir(parents=True)
    paths.state_file("books").write_text("{oops")
    code, out = _post(srv, "/api/investments/books/enabled", {"on": True})
    assert code == 409 and "damaged" in out["message"]
    assert paths.state_file("books").read_text() == "{oops"
    assert _get(srv, "/api/investments/books")["unreadable"] is True


def test_the_frontend_says_your_book_only_through_the_helper():
    from pathlib import Path

    js = (Path(server.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    html = (Path(server.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    left = [
        ln
        for ln in js.splitlines()
        if "our book" in ln and "yourBook = " not in ln and "booksMany()" not in ln
    ]
    assert left == []
    assert "<span data-your-book>your book</span>" in html
    assert html.count("your book") == 1


def test_a_refresh_for_the_book_sent_after_a_switch_is_refused(srv):
    inv.add(BUY, 0)
    _partner()  # now showing
    for view in ("__book__", "__investments__"):
        status, body = _post(
            srv, "/api/refresh-job", {"view": view, "book_id": books.MAIN, "phases": ["quotes"]}
        )
        assert status == 409 and "job_id" not in body


def test_only_the_newest_tab_activation_paints():
    from pathlib import Path

    js = (Path(server.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("async function activateTab(") :]
    assert body.index("if (stale()) return;") < body.index("DATA = rows;")
    follow = js[js.index("async function booksFollow(") :]
    assert follow.index("_activateGen++") < follow.index("await ")


def test_a_build_saves_under_the_tab_it_started_on():
    from pathlib import Path

    js = (Path(server.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    build = js[js.index("async function build(") :]
    assert build.index("const targetName = BUILD_VIEW") < build.index("await fetch(")
    tab = js[js.index("async function activateTab(") :]
    assert tab.index("BUILD_VIEW !== name") < tab.index("STATE.activeView = name")
