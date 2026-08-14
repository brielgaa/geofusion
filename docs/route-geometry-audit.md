# Geometry audit and quality shadow

`src/route_geometry_audit.py` evaluates recapes that do not have current official geometry. It loads the existing `RoadGraph` read-only and writes diagnostic artifacts only.

## First audit

```powershell
python src/transform.py --audit-route-geometries --resume
```

`--route-geometry-shadow` is an existing dispatch alias. For a quick check use `--sample 5`; for a failure class use `--only-failure SEM_INTERSECAO_DE`, `SEM_INTERSECAO_ATE`, or `SEM_CAMINHO`. `--reset-cache` resets only the audit checkpoint/cache selected by the implementation.

The first audit writes `route_geometry_audit.csv`, `route_geometry_report.json`, `route_geometry_review.csv`, and `route_geometry_audit_checkpoint.json`.

The recovery strategies are progressive and remain evidence-labelled: existing human decisions, topological limits/intersections, geometric intersections without a graph node, a small snap, one-boundary extension, whole-street text, coordinate/extension recovery, and nearby-segment fallback. The exact strategy and its evidence are persisted per row.

## Quality shadow

```powershell
python src/route_geometry_audit.py --quality-shadow --resume
```

This second pass evaluates `ESTIMATED` and `UNRESOLVED` records and writes `route_geometry_quality_shadow.csv`, `route_geometry_quality_review.csv`, `route_geometry_quality_report.json`, and its checkpoint. It can also be limited to `--only-same-transversal` or repair an already generated shadow file with `--quality-repair`.

The strategies and alternatives remain diagnostic. No geometry is applied to `recape_clean.csv`, and the audit does not alter `RoadGraph.route()`.

Only existing `CONFIRMED` geometry and high-confidence candidates are eligible for a future controlled application. `RECONSTRUCTED_MEDIUM` and `ESTIMATED` stay separate and are not applied automatically.

## Confidence semantics

`RECONSTRUCTED_HIGH` and `RECONSTRUCTED_MEDIUM` describe audit candidates, not an automatic mutation of official data. `ESTIMATED` is a partial-evidence hypothesis and must not be reported as confirmed geometry. `UNRESOLVED` remains explicit when no acceptable alternative exists.

For human decisions, use [Human review](route-geometry-review.md). For the ESTIMATED Pareto report, the existing utility is:

```powershell
python src/route_geometry_estimated_pareto.py
```
