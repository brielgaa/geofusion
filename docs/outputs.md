# Outputs

Generated files belong to one of four scopes. The scope is more important than the filename: do not promote a diagnostic or shadow result merely because it contains a geometry.

## Official

| File | Scope |
| --- | --- |
| `recape_clean.csv` | normalized resurfacing records and current official route fields |
| `notificacoes.csv` | normalized notification sources |
| `cruzamento.csv` | official notification/resurfacing match output |
| `recapes_sem_cobertura.csv` | official route-failure evidence |
| `geosampa_coverage_report.json` | official coverage summary |
| `pipeline_run.json` | current successful-run metadata |

The dashboard reads this group only.

## Diagnostic

`street_resolution_audit.csv`, `street_resolution_report.json`, `route_geometry_audit.csv`, `route_geometry_report.json`, their review queues, and failure reports explain current behavior and candidate evidence. They do not change official rows.

## Shadow

`route_geometry_quality_shadow.csv`, `route_geometry_quality_report.json`, `route_geometry_same_transversal_*`, and `street_resolution_override_shadow.csv` represent alternatives or projections. The important distinction is:

```text
projected shadow coverage != confirmed official geometry
```

## Human review

`*_human_review.csv` stores decisions. `*_approved.csv`, `*_rejected.csv`, alias exports, and review reports are downstream review artifacts. They require explicit operational governance before being used to modify official data.

All of these files are generated under ignored local directories. Inspect for addresses, coordinates, IDs, and absolute paths before sharing.
