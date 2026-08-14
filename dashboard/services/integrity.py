"""Integrity helpers used by the operational-dashboard verification report."""
from __future__ import annotations

import hashlib
from pathlib import Path


PROTECTED_PATHS = (
    "src/road_graph.py",
    "src/street_resolver.py",
    "src/geometry_validator.py",
    "src/boundary_contradiction_audit.py",
    "src/boundary_name_recovery.py",
    "src/consensus_evidence.py",
    "data/config/street_aliases.csv",
    "data/processed/recape_clean.csv",
    "data/processed/cruzamento.csv",
    "data/processed/notificacoes.csv",
    "data/processed/recapes_sem_cobertura.csv",
    "data/processed/route_geometry_quality_shadow.csv",
    "data/processed/geometry_validation_shadow.csv",
    "data/processed/consensus_evidence_shadow.csv",
)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def protected_hashes(project_dir: Path) -> dict[str, str]:
    return {
        relative: hash_file(project_dir / relative)
        for relative in PROTECTED_PATHS
        if (project_dir / relative).exists()
    }
