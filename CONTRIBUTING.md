# Contributing to GeoFusion

## Local setup

Use Python 3.11+, create a virtual environment, and install `requirements.txt`. Keep operational inputs and generated files under the ignored `data/raw/`, `data/processed/`, and `data/cache/` directories.

## Changes

Create a focused branch, preserve the separation between official and diagnostic outputs, and avoid changing routing, resolution heuristics, or schemas without tests and an explicit design decision.

Before opening a pull request, run:

```powershell
python -m compileall -q src dashboard tests
python -m pytest -q
git diff --check
```

Pull requests should explain the user-visible or maintenance impact, include relevant tests, and call out any data or compatibility implications.
