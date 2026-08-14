"""Small diagnostic CLI for the operational data layer."""
from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import date
from typing import Sequence

from .models import StreetLookupQuery
from .repository import OperationalRepository
from .services import OperationalQueryService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GeoFusion operational lookup")
    parser.add_argument("--inventory", action="store_true", help="write/read operational_data_capabilities.json")
    parser.add_argument("--street")
    parser.add_argument("--number")
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--id", dest="record_id")
    parser.add_argument("--reference-date", default=date.today().isoformat())
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--iterations", type=int, default=20)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = OperationalRepository()
    if args.inventory:
        payload = repository.write_inventory(reference_date=date.fromisoformat(args.reference_date))
        print(json.dumps({"output": "data/processed/operational_data_capabilities.json", "version": payload["version"], "capabilities": payload["capabilities"]}, ensure_ascii=False, indent=2))
        return 0
    query = StreetLookupQuery(street=args.street, number=args.number, latitude=args.lat, longitude=args.lon, record_id=args.record_id)
    if not any(value is not None and value != "" for value in (query.street, query.number, query.latitude, query.longitude, query.record_id)):
        build_parser().print_help()
        return 2
    service = OperationalQueryService(repository)
    started = time.perf_counter()
    result = service.lookup(query, reference_date=date.fromisoformat(args.reference_date))
    cold = time.perf_counter() - started
    payload = {"result": result.to_dict(), "performance": {"cold_first_query_seconds": round(cold, 6)}}
    if args.benchmark:
        timings = []
        for _ in range(max(1, args.iterations)):
            started = time.perf_counter()
            service.lookup(query, reference_date=date.fromisoformat(args.reference_date))
            timings.append(time.perf_counter() - started)
        ordered = sorted(timings)
        p95_index = min(len(ordered) - 1, max(0, int(round(0.95 * len(ordered))) - 1))
        payload["performance"].update({
            "warm_query_iterations": len(timings),
            "warm_query_median_seconds": round(statistics.median(timings), 6),
            "warm_query_p95_seconds": round(ordered[p95_index], 6),
        })
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
