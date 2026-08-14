"""Persisted, lazy textual index for GeoSampa operational lookup.

The index stores the fields used by ``StreetLookupService`` in SQLite.  Geometry
is kept as a BLOB in the same file but is selected and materialized only for the
candidate segments that a query actually returns.  The full spatial resource
continues to live in ``OperationalRepository.segments`` and is not changed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from shapely.geometry import LineString
from shapely import wkb

try:
    from transform import normalizar_rua
except ImportError:  # pragma: no cover - package import
    from ..transform import normalizar_rua


SOURCE_RELATIVE = Path("data/cache/geosampa_segmento_logradouro.geojson")
INDEX_RELATIVE = Path("data/processed/operational_lookup_index.sqlite")
METADATA_RELATIVE = Path("data/processed/operational_lookup_index_metadata.json")
INDEX_VERSION = "operational-lookup-sqlite-v1"
SCHEMA_VERSION = 1
NORMALIZATION_VERSION = "transform.normalizar_rua-v1"
SOURCE_NAME = "data/cache/geosampa_segmento_logradouro.geojson"


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    rendered = str(value).strip()
    return "" if rendered.casefold() in {"", "nan", "none", "null", "<na>"} else rendered


def _integer(value: Any) -> int | None:
    try:
        number = float(str(value).replace(",", "."))
        return int(number) if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _normalize(value: Any) -> str:
    try:
        return normalizar_rua(_text(value))
    except Exception:
        return _text(value).upper()


def source_signature(root: Path) -> dict[str, Any]:
    source = root / SOURCE_RELATIVE
    try:
        stat = source.stat()
    except OSError:
        return {"source_path": str(SOURCE_RELATIVE).replace("\\", "/"), "source_size": 0, "source_mtime_ns": 0}
    return {
        "source_path": str(SOURCE_RELATIVE).replace("\\", "/"),
        "source_size": int(stat.st_size),
        "source_mtime_ns": int(stat.st_mtime_ns),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def index_status(root: Path, *, index_path: Path | None = None, metadata_path: Path | None = None) -> dict[str, Any]:
    index_path = index_path or root / INDEX_RELATIVE
    metadata_path = metadata_path or root / METADATA_RELATIVE
    if not index_path.exists() or not metadata_path.exists():
        return {"valid": False, "reason": "MISSING", "index_path": str(index_path), "metadata_path": str(metadata_path)}
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"valid": False, "reason": "INVALID_METADATA", "error": str(exc), "index_path": str(index_path), "metadata_path": str(metadata_path)}
    expected = source_signature(root)
    checks = {
        "index_version": metadata.get("index_version") == INDEX_VERSION,
        "schema_version": metadata.get("schema_version") == SCHEMA_VERSION,
        "normalization_version": metadata.get("normalization_version") == NORMALIZATION_VERSION,
        "source_path": metadata.get("source_path") == expected["source_path"],
        "source_size": metadata.get("source_size") == expected["source_size"],
        "source_mtime_ns": metadata.get("source_mtime_ns") == expected["source_mtime_ns"],
    }
    if not all(checks.values()):
        return {"valid": False, "reason": "STALE", "checks": checks, "metadata": metadata, "index_path": str(index_path), "metadata_path": str(metadata_path)}
    return {"valid": True, "reason": "VALID", "metadata": metadata, "index_path": str(index_path), "metadata_path": str(metadata_path)}


@dataclass
class PersistedSegment:
    """Segment-shaped object compatible with StreetLookupService.

    The ``geometry`` property is the only operation that reads the geometry
    BLOB.  Text and number filtering never materialize it.
    """

    index: "PersistedLookupIndex"
    source_order: int
    segment_id: str
    street: str
    normalized_street: str
    codlog: str | None
    number_initial_even: int | None
    number_final_even: int | None
    number_initial_odd: int | None
    number_final_odd: int | None
    source: str = SOURCE_NAME
    _geometry: Any = field(default=None, init=False, repr=False)

    @property
    def geometry(self) -> Any:
        if self._geometry is None:
            self._geometry = self.index.geometry_for(self.source_order)
        return self._geometry

    def number_range(self, number: int | None) -> tuple[int | None, int | None]:
        if number is None:
            return None, None
        return (
            (self.number_initial_odd, self.number_final_odd)
            if number % 2
            else (self.number_initial_even, self.number_final_even)
        )

    def matches_number(self, number: int) -> bool:
        start, end = self.number_range(number)
        return start is not None and end is not None and min(start, end) <= number <= max(start, end)

    def to_candidate(self, *, number: int | None = None, distance_m: float | None = None):
        from .models import StreetLookupCandidate

        start, end = self.number_range(number)
        return StreetLookupCandidate(
            segment_id=self.segment_id,
            street=self.street,
            normalized_street=self.normalized_street,
            codlog=self.codlog,
            geometry_wkt=self.geometry.wkt if self.geometry is not None else None,
            number_match="MATCHED" if number is not None and self.matches_number(number) else ("RANGE_AVAILABLE" if number is not None else "NOT_REQUESTED"),
            number_range={"start": start, "end": end},
            distance_to_segment_m=round(float(distance_m), 6) if distance_m is not None else None,
            source=self.source,
        )


class PersistedLookupIndex:
    def __init__(self, path: Path):
        self.path = path
        uri = f"file:{path.as_posix()}?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.connection.execute("PRAGMA query_only=ON")

    def close(self) -> None:
        self.connection.close()

    def segments_for_street(self, normalized_street: str) -> list[PersistedSegment]:
        rows = self.connection.execute(
            """
            SELECT source_order, segment_id, street_name, normalized_street,
                   codlog, number_initial_even, number_final_even,
                   number_initial_odd, number_final_odd
            FROM segments
            WHERE normalized_street = ?
            ORDER BY source_order
            """,
            (normalized_street,),
        ).fetchall()
        return [PersistedSegment(self, *row) for row in rows]

    def geometry_for(self, source_order: int) -> Any:
        row = self.connection.execute("SELECT geometry_wkb FROM segments WHERE source_order = ?", (source_order,)).fetchone()
        if row is None or row[0] is None:
            return None
        return wkb.loads(bytes(row[0]))

    def count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM segments").fetchone()
        return int(row[0] if row else 0)


class LazyStreetMapping(Mapping[str, list[PersistedSegment]]):
    def __init__(self, index: PersistedLookupIndex):
        self.index = index

    def __getitem__(self, key: str) -> list[PersistedSegment]:
        result = self.index.segments_for_street(key)
        if not result:
            raise KeyError(key)
        return result

    def __iter__(self) -> Iterator[str]:
        rows = self.index.connection.execute("SELECT DISTINCT normalized_street FROM segments ORDER BY normalized_street")
        yield from (str(row[0]) for row in rows)

    def __len__(self) -> int:
        row = self.index.connection.execute("SELECT COUNT(DISTINCT normalized_street) FROM segments").fetchone()
        return int(row[0] if row else 0)

    def get(self, key: str, default: Any = None) -> Any:
        result = self.index.segments_for_street(key)
        return result if result else default


def open_valid_index(root: Path) -> PersistedLookupIndex | None:
    status = index_status(root)
    if not status["valid"]:
        return None
    try:
        return PersistedLookupIndex(Path(status["index_path"]))
    except (OSError, sqlite3.Error):
        return None


def _feature_rows(source: Path):
    payload = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {"features": []}
    for source_order, feature in enumerate(payload.get("features", [])):
        properties = feature.get("properties") or {}
        geometry_payload = feature.get("geometry") or {}
        coordinates = geometry_payload.get("coordinates")
        if geometry_payload.get("type") != "LineString" or not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            geometry = LineString([(float(x), float(y)) for x, y, *rest in coordinates])
        except (TypeError, ValueError):
            continue
        street = _text(properties.get("nm_logradouro"))
        normalized = _normalize(street)
        if not normalized:
            continue
        yield (
            source_order,
            _text(feature.get("id") or properties.get("cd_identificador")),
            street,
            normalized,
            _text(properties.get("codlog")) or None,
            _integer(properties.get("cd_numero_inicial_par")),
            _integer(properties.get("cd_numero_final_par")),
            _integer(properties.get("cd_numero_inicial_impar")),
            _integer(properties.get("cd_numero_final_impar")),
            sqlite3.Binary(bytes(geometry.wkb)),
        )


def build_lookup_index(root: Path, *, output: Path | None = None, force: bool = False, source_sha256: bool = True) -> dict[str, Any]:
    root = root.resolve()
    source = root / SOURCE_RELATIVE
    output = (output or root / INDEX_RELATIVE).resolve()
    metadata_path = output.with_name("operational_lookup_index_metadata.json")
    if output.exists() and not force:
        raise FileExistsError(f"Index already exists: {output}. Use --force to rebuild it.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(prefix=f"{output.stem}.", suffix=".tmp", dir=output.parent)
    os.close(temp_fd)
    temp_index = Path(temp_name)
    temp_metadata = metadata_path.with_suffix(".tmp")
    started = datetime.now(timezone.utc)
    count = 0
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temp_index)
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE segments (
                source_order INTEGER PRIMARY KEY,
                segment_id TEXT NOT NULL,
                street_name TEXT NOT NULL,
                normalized_street TEXT NOT NULL,
                codlog TEXT,
                number_initial_even INTEGER,
                number_final_even INTEGER,
                number_initial_odd INTEGER,
                number_final_odd INTEGER,
                geometry_wkb BLOB NOT NULL
            );
            """
        )
        insert = "INSERT INTO segments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        batch: list[tuple[Any, ...]] = []
        for row in _feature_rows(source):
            batch.append(row)
            if len(batch) >= 5000:
                connection.executemany(insert, batch)
                count += len(batch)
                batch.clear()
        if batch:
            connection.executemany(insert, batch)
            count += len(batch)
        connection.executescript(
            """
            CREATE INDEX segments_normalized_order_idx ON segments(normalized_street, source_order);
            CREATE INDEX segments_segment_id_idx ON segments(segment_id);
            """
        )
        connection.commit()
        connection.close()
        connection = None
        os.replace(temp_index, output)
        source_info = source_signature(root)
        metadata = {
            **source_info,
            "index_version": INDEX_VERSION,
            "schema_version": SCHEMA_VERSION,
            "normalization_version": NORMALIZATION_VERSION,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "record_count": count,
            "geometry_storage": "sqlite BLOB WKB; not loaded by text-index startup",
            "text_fields": ["segment_id", "street_name", "normalized_street", "codlog", "number_initial_even", "number_final_even", "number_initial_odd", "number_final_odd"],
            "source_sha256": sha256_file(source) if source_sha256 and source.exists() else None,
        }
        temp_metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_metadata, metadata_path)
        return {"output": str(output), "metadata": str(metadata_path), "record_count": count, "build_seconds": (datetime.now(timezone.utc) - started).total_seconds(), "size_bytes": output.stat().st_size}
    except Exception:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        for path in (temp_index, temp_metadata):
            try:
                path.unlink()
            except OSError:
                pass
        raise
