# Security mechanisms

[← CLAUDE.md](../../CLAUDE.md) · part of the engineering notes; `§N` references name the original CLAUDE.md sections (map in CLAUDE.md).

The standing public-repo rules (§18) stay in full in CLAUDE.md. This file holds the two mechanisms from §4 they rely on.

## Request-origin guard

From §4 "HTTP routes (`Handler` in `server.py`)" — the route list itself is in [backend.md](backend.md#http-routes-handler-in-serverpy-line-92).

**Request-origin guard (v1.15) — runs before dispatch in `do_GET` / `do_POST`
/ `do_DELETE` (`Handler._request_allowed`, 403 `{"error": "cross-origin
request refused"}` plus a `refused cross-origin …` stderr line):**
- **Every method:** `Host` must be a loopback name (`127.0.0.1`, `localhost`,
  `[::1]`, any port). A DNS-rebinding page is *same-origin* with us — without
  this it could read `/api/watchlists` (the user's holdings) and skip every
  CORS rule. Browsers can't forge `Host`.
- **POST / DELETE:** an `Origin`, when sent, must equal `http://<Host>`.
  Browsers send it on every non-GET fetch, same-origin included.
- **POST:** `Content-Type` must be `application/json`. text/plain, form and
  multipart POSTs are CORS "simple requests" — sent cross-site with no
  preflight — so without this any web page open in the user's browser could
  overwrite, rename or delete portfolios or start quota-spending jobs. JSON
  forces a preflight, and there is **deliberately no `do_OPTIONS`** (501), so
  it fails. DELETE is never a simple request, so it needs no JSON rule.
- A missing `Host` / `Origin` passes: only non-browser local clients (curl,
  tests, the desktop readiness probe) omit them.

Consequences: **every frontend POST must send `Content-Type:
application/json`, bodyless ones too** (they send `body: "{}"`), and nothing
may use `sendBeacon` (it can't set that header) —
`tests/test_origin_guard.py::test_every_frontend_post_sends_json` scans
`app.js` for both. A new route needs nothing: the guard is at the method
level. Never add a `do_OPTIONS` or CORS headers.

## Settings → API keys (`keys.py`)

From §4 "News & Sentiment", *Fetch and cache*.

- **`keys.py` (Settings → API keys, v1.15).** `POST /api/keys`
  `{provider, action: "set"|"clear", key?}` merges one field into
  `config.json` (`_CONFIG_KEYS` names only; other fields and the other key kept)
  via `persistence._atomic_write` + `chmod 0600`; strips padding, rejects empty /
  inner-whitespace / control chars (400, fixed text). A `config.json` that exists
  but isn't a JSON object is **never overwritten** (409, logged once). The
  explicit `action` means an empty submit can never clear a key. `GET /api/keys`
  = `{set, source: env|config|legacy|null, in_config, env_overrides}` per
  provider — never a value. `POST /api/keys/test` makes one cheap call through
  the shared limiters (Finnhub `quote?symbol=AAPL` with `X-Finnhub-Token`; NIM a
  `max_tokens: 1` completion with Bearer) and returns a fixed status word
  (`ok/rejected/rate_limited/unavailable/network_error/no_key/error`); the key
  is only ever in a header, never a URL, and a full limiter reports
  `rate_limited` instead of blocking the click. These branches never put
  `str(exc)` or a traceback in a response or log. Like every route, they sit behind the
  request-origin guard (§4 HTTP routes). `tests/test_keys.py`'s leak
  test posts a sentinel and greps every response, stdout/stderr and the log ring.
  `keys._FINNHUB_BASE` / `_NVIDIA_BASE` are module constants so tests point them
  at a local stub. First-run banner: `/api/health` `finnhub_key_set` /
  `nvidia_key_set` → `syncKeysBanner` (dismissal in localStorage until both are
  set; `syncLlmBanner` stands down while the NVIDIA key is missing).
