"""CLI for building the persisted GeoSampa textual lookup index."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .lookup_index import build_lookup_index


def main() -> None:
    parser = argparse.ArgumentParser(description="Build GeoFusion's persisted operational street lookup index.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true", help="Replace an existing index.")
    parser.add_argument("--no-sha256", action="store_true", help="Skip source SHA-256 during the offline build.")
    parser.add_argument("--benchmark", action="store_true", help="Print build and file-size metrics as JSON.")
    args = parser.parse_args()
    result = build_lookup_index(args.root.resolve(), output=args.output, force=args.force, source_sha256=not args.no_sha256)
    if args.benchmark:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Index built: {result['output']} ({result['record_count']} rows, {result['size_bytes']} bytes)")


if __name__ == "__main__":
    main()
