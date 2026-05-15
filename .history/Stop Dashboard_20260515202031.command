#!/usr/bin/env bash

set -e

cd "$(dirname "$0")"

PID_FILE=".dashboard.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "Portfolio Dashboard is not running."
    exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [ -z "$PID" ]; then
    rm -f "$PID_FILE"
    echo "Portfolio Dashboard is not running."
    exit 0
fi

if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "Stopped Portfolio Dashboard (PID $PID)."
else
    echo "Portfolio Dashboard is not running."
fi

rm -f "$PID_FILE"