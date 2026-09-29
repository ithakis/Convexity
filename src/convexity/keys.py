"""API keys: Settings -> API keys (roadmap Phase 6).

Stores the Finnhub and NVIDIA NIM keys in the data folder's ``config.json``
(the same file and field names ``helpers._load_local_secret`` reads), reloads
them into the running modules without a restart, and tests them with one cheap
call each.

The contract is that a key VALUE goes to exactly two places: ``config.json``
and the provider's own auth header. Nothing here returns it, logs it, or puts
it in an exception message or a URL — ``status()`` and ``check()`` return
booleans, a source name and a fixed status word. ``tests/test_keys.py`` posts
a sentinel key and greps every response body and the log ring for it.

Writes are merged (other fields and the other key are kept), atomic
(``persistence._atomic_write``: temp file beside the target + ``os.replace``;
``mkstemp`` creates it 0600, and it is chmod-ed 0600 again after the replace)
and refused when the existing file is not a JSON object — overwriting a
hand-edited file the user broke would silently throw away whatever else it
held.
"""

import contextlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

from convexity import helpers, paths

# provider -> (env var, legacy key file). Field names in config.json come from
# helpers._CONFIG_KEYS so reader and writer can never disagree.
PROVIDERS: dict[str, tuple[str, str]] = {
    "finnhub": ("FINNHUB_API_KEY", ".finnhub_key"),
    "nvidia": ("NVIDIA_API_KEY", ".nvidia_key"),
}
_LABELS = {"finnhub": "Finnhub", "nvidia": "NVIDIA NIM"}

# Module constants so tests (and a scratch E2E launcher) can point the Test
# buttons at a local stub instead of the real providers.
_FINNHUB_BASE = "https://finnhub.io/api/v1/"
_NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
_FH_TIMEOUT_S = 8.0
_NV_TIMEOUT_S = 20.0
_MAX_KEY_LEN = 512

_WRITE_LOCK = threading.Lock()
_MALFORMED_WARNED: list = []


class ConfigMalformed(Exception):
    """config.json exists but is not a JSON object; we refuse to overwrite it."""


class InvalidKey(ValueError):
    """The submitted key is empty or not key-shaped. Messages are fixed text."""


def _field(provider: str) -> str:
    return helpers._CONFIG_KEYS[PROVIDERS[provider][0]]


def _check_provider(provider) -> str:
    if provider not in PROVIDERS:
        raise InvalidKey("unknown provider")
    return provider


def _normalise(key) -> str:
    if not isinstance(key, str):
        raise InvalidKey("key must be text")
    key = key.strip()
    if not key:
        raise InvalidKey("key is empty")
    if len(key) > _MAX_KEY_LEN:
        raise InvalidKey("key is too long")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in key):
        # A pasted key with a space or line break inside is a copy error, and a
        # control character would end up in an HTTP header.
        raise InvalidKey("key contains spaces or control characters")
    return key


def _config_state() -> tuple[bool, bool]:
    """(exists, ok) — ok is False when the file exists but is not a JSON object."""
    cfg = paths.config_file()
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, True
    except (OSError, ValueError):
        return True, False
    return True, isinstance(data, dict)


def status() -> dict:
    """What Settings shows. Booleans and source names only — never a value.
    ``in_config`` says whether config.json holds a key (so Clear means
    something even while an env var wins)."""
    exists, ok = _config_state()
    in_cfg: dict = {}
    if exists and ok:
        try:
            data = json.loads(paths.config_file().read_text(encoding="utf-8"))
            in_cfg = {
                p: bool(isinstance(data.get(_field(p)), str) and data[_field(p)].strip())
                for p in PROVIDERS
            }
        except (OSError, ValueError):
            pass
    out: dict = {"config_ok": ok, "config_path": str(paths.config_file())}
    for p, (env_var, fname) in PROVIDERS.items():
        src = helpers.secret_source(env_var, fname)
        out[p] = {
            **src,
            "in_config": bool(in_cfg.get(p)),
            "env_var": env_var,
            "env_overrides": src["source"] == "env" and bool(in_cfg.get(p)),
        }
    return out


def _write(provider: str, key: str | None) -> None:
    cfg = paths.config_file()
    with _WRITE_LOCK:
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            _warn_malformed(cfg, type(exc).__name__)
            raise ConfigMalformed() from None
        if not isinstance(data, dict):
            _warn_malformed(cfg, type(data).__name__)
            raise ConfigMalformed()
        field = _field(provider)
        if key is None:
            if field not in data:
                return
            data.pop(field)
        else:
            if data.get(field) == key:
                return  # a double submit: nothing to write
            data[field] = key
        from convexity.persistence import _atomic_write

        _atomic_write(cfg, json.dumps(data, indent=2) + "\n")
        with contextlib.suppress(OSError):
            os.chmod(cfg, 0o600)


def _warn_malformed(cfg, what: str) -> None:
    if not _MALFORMED_WARNED:
        _MALFORMED_WARNED.append(True)
        print(
            f"[keys] refusing to write {cfg}: it is not a JSON object ({what}) — "
            f"fix or delete it, then save the key again",
            file=sys.stderr,
        )


def save(provider, key) -> dict:
    provider = _check_provider(provider)
    key = _normalise(key)
    _write(provider, key)
    reload_all()
    print(f"[keys] {_LABELS[provider]} key saved to {paths.config_file().name}", file=sys.stderr)
    return status()


def clear(provider) -> dict:
    provider = _check_provider(provider)
    _write(provider, None)
    reload_all()
    print(
        f"[keys] {_LABELS[provider]} key removed from {paths.config_file().name}", file=sys.stderr
    )
    return status()


def reload_all() -> None:
    """Push the current keys into every module that caches one. Also re-arms
    the one-time "malformed config.json" warnings, so a file the user fixed is
    not reported as ignored forever — and a newly broken one is reported."""
    del helpers._CONFIG_WARNED[:]
    del _MALFORMED_WARNED[:]
    from convexity import finnhub_adapter, news_sentiment

    news_sentiment.reload_keys()
    finnhub_adapter.reload_keys()


# ----------------------------- Test buttons ---------------------------------


def _classify_http(code: int) -> str:
    if code in (401, 403):
        return "rejected"
    if code == 429:
        return "rate_limited"
    if code in (404, 410) or code >= 500:
        return "unavailable"
    return "error"


def _call(req: urllib.request.Request, timeout: float, limiter) -> dict:
    snap = limiter.snapshot()
    if snap["used"] >= snap["limit"]:
        # Report it rather than block the Settings click for up to a minute.
        return {"status": "rate_limited", "http": None}
    limiter.acquire()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(4096)
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code == 429:
            limiter.penalize()
        return {"status": _classify_http(code), "http": code}
    except (urllib.error.URLError, TimeoutError, OSError):
        # Never str(exc): nothing in it is useful to the user and it is the one
        # place an unexpected message could carry request details.
        return {"status": "network_error", "http": None}
    except Exception:
        return {"status": "error", "http": None}
    return {"status": "ok" if code == 200 else "error", "http": code}


def check(provider) -> dict:
    """One cheap authenticated call with the key currently in effect. The key
    travels in a header, never in the URL (urllib error text and proxies can
    echo URLs)."""
    provider = _check_provider(provider)
    env_var, fname = PROVIDERS[provider]
    key = helpers._load_local_secret(env_var, fname)
    if not key:
        return {"provider": provider, "status": "no_key", "http": None, "ms": 0}
    t0 = time.monotonic()
    if provider == "finnhub":
        req = urllib.request.Request(
            _FINNHUB_BASE + "quote?symbol=AAPL",
            headers={"X-Finnhub-Token": key, "User-Agent": "Convexity/1.0"},
        )
        res = _call(req, _FH_TIMEOUT_S, helpers._FH_LIMITER)
    else:
        from convexity.news_sentiment import _MODEL as model

        body = json.dumps(
            {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ok"}]}
        ).encode()
        req = urllib.request.Request(
            _NVIDIA_BASE.rstrip("/") + "/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Convexity/1.0",
            },
        )
        res = _call(req, _NV_TIMEOUT_S, helpers._NV_LIMITER)
    del key
    res.update({"provider": provider, "ms": int((time.monotonic() - t0) * 1000)})
    print(
        f"[keys] {_LABELS[provider]} key test: {res['status']}"
        + (f" (HTTP {res['http']})" if res.get("http") else ""),
        file=sys.stderr,
    )
    return res
