"""Reproducible cold/warm benchmark and equivalence check for street lookup."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .lookup_index import build_lookup_index, open_valid_index, sha256_file
from .models import StreetLookupQuery
from .repository import OperationalRepository
from .services import StreetLookupService


SAMPLE_RELATIVE = Path("data/processed/operational_lookup_query_sample.json")


def _rss_mb() -> float | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        return round(counters.WorkingSetSize / 1024 / 1024, 3) if ok else None
    except Exception:
        return None


def _first_number(row: tuple[Any, ...]) -> int | None:
    for start, end, parity in ((row[4], row[5], 0), (row[6], row[7], 1)):
        if start is None or end is None:
            continue
        low, high = sorted((int(start), int(end)))
        candidate = low if low % 2 == parity else low + 1
        if candidate <= high:
            return candidate
    return None


def build_query_sample(root: Path, *, limit: int = 1000, output: Path | None = None) -> dict[str, Any]:
    index = open_valid_index(root)
    if index is None:
        raise RuntimeError("A valid persisted lookup index is required to generate the deterministic sample.")
    rows = index.connection.execute(
        """
        SELECT source_order, segment_id, street_name, normalized_street,
               number_initial_even, number_final_even,
               number_initial_odd, number_final_odd
        FROM segments
        WHERE street_name <> ''
        ORDER BY segment_id, source_order
        """
    ).fetchall()
    queries: list[dict[str, Any]] = []
    for row in rows:
        number = _first_number(row)
        if number is None:
            continue
        queries.append({"street": row[2], "number": number, "source_segment_id": row[1], "source_order": row[0]})
        if len(queries) >= limit:
            break
    index.close()
    if len(queries) < limit:
        raise RuntimeError(f"Only {len(queries)} deterministic street+number queries could be generated; expected {limit}.")
    payload = {"version": 1, "count": len(queries), "queries": queries}
    target = output or root / SAMPLE_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _load_sample(root: Path, sample_path: Path | None = None) -> list[dict[str, Any]]:
    path = sample_path or root / SAMPLE_RELATIVE
    if not path.exists():
        return build_query_sample(root)["queries"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload["queries"])


def _result_summary(result: Any) -> dict[str, Any]:
    return {
        "confidence": result.confidence,
        "match_method": result.match_method,
        "segment_id": result.segment_id,
        "candidate_count": result.candidate_count,
    }


def _child(root: Path, *, legacy: bool, scenario: str, sample_path: Path | None, repeat: int) -> None:
    sample = _load_sample(root, sample_path)
    first = sample[0]
    if scenario == "street":
        query = StreetLookupQuery(street=first["street"])
    elif scenario == "street_number":
        query = StreetLookupQuery(street=first["street"], number=first["number"])
    elif scenario == "coordinate":
        query = StreetLookupQuery(latitude=-23.5616, longitude=-46.6558)
    else:
        raise ValueError(f"Unknown scenario: {scenario}")
    rss_before = _rss_mb()
    started = time.perf_counter()
    repository = OperationalRepository(root, use_persisted_lookup_index=not legacy)
    repository_creation_s = time.perf_counter() - started
    service = StreetLookupService(repository)
    service_init_s = time.perf_counter() - started - repository_creation_s
    started = time.perf_counter()
    first_result = service.lookup(query)
    first_lookup_s = time.perf_counter() - started
    warm_times: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        service.lookup(query)
        warm_times.append(time.perf_counter() - started)
    print(json.dumps({
        "legacy": legacy,
        "scenario": scenario,
        "repository_creation_s": repository_creation_s,
        "service_init_s": service_init_s,
        "first_lookup_s": first_lookup_s,
        "warm_min_s": min(warm_times) if warm_times else None,
        "warm_median_s": statistics.median(warm_times) if warm_times else None,
        "warm_p95_s": sorted(warm_times)[max(0, int(len(warm_times) * 0.95) - 1)] if warm_times else None,
        "rss_before_mb": rss_before,
        "rss_after_lookup_mb": _rss_mb(),
        "full_segments_loaded": repository._segments is not None,
        "lookup_index_open": repository._lookup_index is not None,
        "result": _result_summary(first_result),
    }, ensure_ascii=False))


def _run_child(root: Path, *, legacy: bool, scenario: str, sample_path: Path, repeat: int) -> dict[str, Any]:
    command = [sys.executable, "-m", "src.operational.lookup_benchmark", "--child", "--root", str(root), "--scenario", scenario, "--sample", str(sample_path), "--repeat", str(repeat)]
    if legacy:
        command.append("--legacy")
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=root, check=True, capture_output=True, text=True, timeout=180)
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    payload["process_wall_s"] = time.perf_counter() - started
    return payload


def run_benchmark(root: Path, *, sample_path: Path | None = None, cold_repetitions: int = 2, warm_repetitions: int = 20) -> dict[str, Any]:
    root = root.resolve()
    sample_path = (sample_path or root / SAMPLE_RELATIVE).resolve()
    if not sample_path.exists():
        build_query_sample(root, output=sample_path)
    result: dict[str, Any] = {
        "sample_path": str(sample_path),
        "sample_count": len(_load_sample(root, sample_path)),
        "cold_repetitions": cold_repetitions,
        "warm_repetitions": warm_repetitions,
        "cold": {"before_legacy": {}, "after_persisted_index": {}},
        "warm": {},
    }
    for label, legacy in (("before_legacy", True), ("after_persisted_index", False)):
        for scenario in ("street", "street_number", "coordinate"):
            repetitions = 1 if scenario == "coordinate" else cold_repetitions
            runs = [_run_child(root, legacy=legacy, scenario=scenario, sample_path=sample_path, repeat=0) for _ in range(repetitions)]
            result["cold"][label][scenario] = {
                "runs": runs,
                "process_wall_median_s": statistics.median(item["process_wall_s"] for item in runs),
                "first_lookup_median_s": statistics.median(item["first_lookup_s"] for item in runs),
            }
        result["warm"][label] = _run_child(root, legacy=legacy, scenario="street_number", sample_path=sample_path, repeat=warm_repetitions)
    return result


def run_equivalence(root: Path, *, sample_path: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    sample = _load_sample(root, sample_path)
    legacy_service = StreetLookupService(OperationalRepository(root, use_persisted_lookup_index=False))
    optimized_service = StreetLookupService(OperationalRepository(root, use_persisted_lookup_index=True))
    started = time.perf_counter()
    mismatches: list[dict[str, Any]] = []
    mismatch_count = 0
    for item in sample:
        query = StreetLookupQuery(street=item["street"], number=item["number"])
        before = legacy_service.lookup(query).to_dict()
        after = optimized_service.lookup(query).to_dict()
        if before != after:
            mismatch_count += 1
            mismatches.append({
                "source_segment_id": item.get("source_segment_id"),
                "street": item["street"],
                "number": item["number"],
                "different_fields": sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key)),
                "before": before,
                "after": after,
            })
            if len(mismatches) > 5:
                mismatches.pop()
    return {
        "sample_count": len(sample),
        "compared_count": len(sample),
        "mismatch_count": mismatch_count,
        "mismatch_examples_capped_at_five": len(mismatches),
        "all_equivalent": not mismatches,
        "elapsed_s": time.perf_counter() - started,
        "mismatches": mismatches,
    }


def run_determinism(root: Path) -> dict[str, Any]:
    index_path = root / "data" / "processed" / "operational_lookup_index.sqlite"
    initial_sha = sha256_file(index_path) if index_path.exists() else None
    first_build = build_lookup_index(root, force=True, source_sha256=True)
    first_sha = sha256_file(index_path)
    second_build = build_lookup_index(root, force=True, source_sha256=True)
    second_sha = sha256_file(index_path)
    return {
        "index_path": str(index_path),
        "initial_sha256": initial_sha,
        "first_rebuild_sha256": first_sha,
        "second_rebuild_sha256": second_sha,
        "initial_matches_rebuild": initial_sha == first_sha if initial_sha else None,
        "rebuilds_byte_identical": first_sha == second_sha,
        "first_build_seconds": first_build["build_seconds"],
        "second_build_seconds": second_build["build_seconds"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--sample", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--scenario", choices=("street", "street_number", "coordinate"))
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--equivalence", action="store_true")
    parser.add_argument("--determinism", action="store_true")
    parser.add_argument("--sample-only", action="store_true")
    parser.add_argument("--cold-repetitions", type=int, default=2)
    args = parser.parse_args()
    root = args.root.resolve()
    sample_path = (args.sample or root / SAMPLE_RELATIVE).resolve()
    if args.sample_only:
        result = build_query_sample(root, output=sample_path)
    elif args.child:
        _child(root, legacy=args.legacy, scenario=args.scenario or "street_number", sample_path=sample_path, repeat=args.repeat)
        return
    elif args.equivalence:
        result = run_equivalence(root, sample_path=sample_path)
    elif args.determinism:
        result = run_determinism(root)
    else:
        result = run_benchmark(root, sample_path=sample_path, cold_repetitions=args.cold_repetitions, warm_repetitions=args.repeat)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
