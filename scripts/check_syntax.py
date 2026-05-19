#!/usr/bin/env python3
"""AST syntax check for all .py files — used by pre-commit and CI."""
import ast
import pathlib
import sys

errors = []
checked = 0
for p in pathlib.Path(".").rglob("*.py"):
    if any(part.startswith(".") for part in p.parts):
        continue  # skip .venv, __pycache__, .git, etc.
    try:
        ast.parse(p.read_text(encoding="utf-8"))
        checked += 1
    except SyntaxError as e:
        errors.append(f"{p}:{e.lineno}: {e.msg}")

if errors:
    print("Syntax errors found:")
    for err in errors:
        print(f"  {err}")
    sys.exit(1)

print(f"Syntax OK — checked {checked} files")
