# Security

## Reporting a vulnerability

Please report security issues privately via GitHub's
[private vulnerability reporting](https://github.com/ithakis/Convexity/security/advisories/new)
rather than a public issue. You will get a reply as soon as possible.

## What Convexity does and does not do

- **Runs locally.** The server binds to `127.0.0.1` only; it is not reachable
  from other machines. There are no accounts, logins or cloud storage.
- **Your data stays on your machine.** Portfolios, watchlists, optimizer runs
  and the news cache are plain JSON files next to the app (`.convexity_*.json`),
  never uploaded anywhere and never committed to this repository.
- **Outbound network calls** go only to: Yahoo Finance (prices, via
  `yfinance`), Finnhub (company news, optional), NVIDIA NIM (the News read,
  optional), and the KaTeX CDN (formula rendering in the help text).
- **API keys** are read from environment variables or local files
  (`.finnhub_key`, `.nvidia_key`) that are git-ignored. They are sent only to
  their own provider and are never logged or exposed by the app's API.

## Supported versions

Only the latest release receives fixes.
