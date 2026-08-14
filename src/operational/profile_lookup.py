"""Baseline profiling for the operational lookup path.

This module intentionally profiles the current repository implementation without
changing it.  It is also reusable after the persisted lookup index is enabled.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from pyproj import Transformer
from shapely.geometry import LineString
from shapely.strtree import STRtree

from .models import StreetLookupQuery
from .repository import OperationalRepository, SegmentRecord
from .services import OperationalQueryService, StreetLookupService


def _timed(function: Callable[[], Any]) -> tuple[float, Any]:
    started = time.perf_counter()
    value = function()
    return time.perf_counter() - started, value


def _rss_mb() -> float | None:
    """Return Windows working-set size without adding a dependency."""
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


def _import_cold_seconds() -> float:
    code = "import src.operational.repository, src.operational.services"
    started = time.perf_counter()
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)
    return time.perf_counter() - started


def _component_profile(repository: OperationalRepository) -> dict[str, float]:
    path = repository.cache / "geosampa_segmento_logradouro.geojson"
    profile: dict[str, float] = {}
    started = time.perf_counter()
    Transformer.from_crs("EPSG:4326", "EPSG:31983", always_xy=True)
    profile["crs_transformer_init_s"] = time.perf_counter() - started
    started = time.perf_counter()
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    profile["geojson_read_s"] = time.perf_counter() - started
    started = time.perf_counter()
    payload = json.loads(raw) if raw else {"features": []}
    profile["json_parse_s"] = time.perf_counter() - started

    segments: list[SegmentRecord] = []
    parse_started = time.perf_counter()
    geometry_elapsed = 0.0
    normalization_elapsed = 0.0
    number_elapsed = 0.0
    for feature in payload.get("features", []):
        properties = feature.get("properties") or {}
        geometry_payload = feature.get("geometry") or {}
        coordinates = geometry_payload.get("coordinates")
        if geometry_payload.get("type") != "LineString" or not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        geometry_started = time.perf_counter()
        try:
            geometry = LineString([(float(x), float(y)) for x, y, *rest in coordinates])
        except (TypeError, ValueError):
            continue
        geometry_elapsed += time.perf_counter() - geometry_started
        street = repository._text(properties.get("nm_logradouro"))
        normalization_started = time.perf_counter()
        normalized = repository._normalize(street)
        normalization_elapsed += time.perf_counter() - normalization_started
        if not normalized:
            continue
        number_started = time.perf_counter()
        even_start = repository._integer(properties.get("cd_numero_inicial_par"))
        even_end = repository._integer(properties.get("cd_numero_final_par"))
        odd_start = repository._integer(properties.get("cd_numero_inicial_impar"))
        odd_end = repository._integer(properties.get("cd_numero_final_impar"))
        number_elapsed += time.perf_counter() - number_started
        segments.append(
            SegmentRecord(
                segment_id=repository._text(feature.get("id") or properties.get("cd_identificador")),
                street=street,
                normalized_street=normalized,
                codlog=repository._text(properties.get("codlog")) or None,
                geometry=geometry,
                number_initial_even=even_start,
                number_final_even=even_end,
                number_initial_odd=odd_start,
                number_final_odd=odd_end,
                source="data/cache/geosampa_segmento_logradouro.geojson",
            )
        )
    profile["geometry_and_record_parse_s"] = time.perf_counter() - parse_started
    profile["geometry_parse_s"] = geometry_elapsed
    profile["street_normalization_s"] = normalization_elapsed
    profile["number_range_parse_s"] = number_elapsed
    started = time.perf_counter()
    by_street: dict[str, list[SegmentRecord]] = {}
    for segment in segments:
        by_street.setdefault(segment.normalized_street, []).append(segment)
    profile["dictionary_build_s"] = time.perf_counter() - started
    started = time.perf_counter()
    STRtree([segment.geometry for segment in segments]) if segments else None
    profile["spatial_index_build_s"] = time.perf_counter() - started
    profile["parsed_segment_count"] = float(len(segments))
    profile["normalized_street_count"] = float(len(by_street))
    return profile


def profile(root: Path, *, label: str = "before") -> dict[str, Any]:
    profile: dict[str, Any] = {
        "label": label,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.version,
        "process_start_s": _import_cold_seconds(),
    }
    rss_before = _rss_mb()
    started, repository = _timed(lambda: OperationalRepository(root))
    profile["repository_creation_s"] = started
    profile["rss_before_mb"] = rss_before
    profile.update(_component_profile(repository))
    profile["rss_after_component_profile_mb"] = _rss_mb()

    started, _ = _timed(lambda: repository.segments)
    profile["repository_full_segments_s"] = started
    profile["rss_after_full_segments_mb"] = _rss_mb()

    started, street_service = _timed(lambda: StreetLookupService(OperationalRepository(root)))
    profile["street_service_init_s"] = started
    cold_repo = street_service.repository
    started, first = _timed(lambda: street_service.lookup(StreetLookupQuery(street="AVENIDA PAULISTA")))
    profile["first_street_lookup_s"] = started
    profile["first_street_lookup_result"] = first.to_dict()
    started, first_number = _timed(lambda: street_service.lookup(StreetLookupQuery(street="AVENIDA PAULISTA", number=1250)))
    profile["first_street_number_lookup_s"] = started
    profile["first_street_number_lookup_result"] = first_number.to_dict()
    started, warm = _timed(lambda: street_service.lookup(StreetLookupQuery(street="AVENIDA PAULISTA")))
    profile["warm_street_lookup_s"] = started
    started, warm_number = _timed(lambda: street_service.lookup(StreetLookupQuery(street="AVENIDA PAULISTA", number=1250)))
    profile["warm_street_number_lookup_s"] = started
    profile["warm_street_lookup_result"] = warm.to_dict()
    profile["warm_street_number_lookup_result"] = warm_number.to_dict()
    profile["rss_after_text_lookup_mb"] = _rss_mb()

    started, operational = _timed(lambda: OperationalQueryService(OperationalRepository(root)))
    profile["operational_service_init_s"] = started
    started, operational_result = _timed(lambda: operational.lookup(StreetLookupQuery(street="AVENIDA PAULISTA"), reference_date=date.today()))
    profile["first_operational_lookup_s"] = started
    profile["first_operational_lookup_result"] = operational_result.to_dict()

    coordinate_service = StreetLookupService(OperationalRepository(root))
    started, coordinate = _timed(lambda: coordinate_service.lookup(StreetLookupQuery(latitude=-23.5616, longitude=-46.6558)))
    profile["coordinate_cold_s"] = started
    profile["coordinate_cold_result"] = coordinate.to_dict()
    return profile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--label", default="before")
    args = parser.parse_args()
    result = profile(args.root.resolve(), label=args.label)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
