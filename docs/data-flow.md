# Data flow

GeoFusion uses the filesystem as a lightweight contract between stages. The normal path is:

```text
raw inputs
  -> transform.py loaders and normalization
  -> street and route resolution
  -> official CSV/JSON outputs
  -> dashboard
```

Audits branch from the official artifacts:

```text
official artifacts
  -> street_resolution_audit.csv       diagnostic
  -> route_geometry_audit.csv          diagnostic
  -> route_geometry_quality_shadow.csv shadow
  -> *_human_review.csv                human decisions
```

## Inputs

The ETL looks under `data/raw/` for a local resurfacing workbook/CSV and
local notification source files. The source-specific filenames are deployment
configuration and are intentionally not distributed with this repository.

The street audit also uses the GeoSampa segment source/cache and the tracked alias configuration at `data/config/street_aliases.csv`. The exact loader behavior is implemented in `src/transform.py` and `src/street_resolver.py`.

## Official artifacts

| Artifact | Produced by | Meaning |
| --- | --- | --- |
| `recape_clean.csv` | official ETL | normalized resurfacing records and current route fields |
| `notificacoes.csv` | official ETL | unified notification sources |
| `cruzamento.csv` | official ETL | notification/resurfacing matches and status |
| `recapes_sem_cobertura.csv` | official ETL | detailed official route failures |
| `geosampa_coverage_report.json` | official ETL | current coverage and failure aggregates |
| `pipeline_run.json` | official ETL | current run status, durations, counts, and cache state |

## Diagnostic artifacts

The street audit writes `street_resolution_audit.csv`, `street_resolution_review.csv`, `street_resolution_report.json`, and related normalization/checkpoint artifacts. The route audit writes `route_geometry_audit.csv`, `route_geometry_report.json`, `route_geometry_review.csv`, and checkpoints.

These files explain evidence and failure causes; they do not replace official outputs.

## Shadow artifacts

The quality pass writes `route_geometry_quality_shadow.csv`, `route_geometry_quality_review.csv`, `route_geometry_quality_report.json`, and its checkpoint. Same-transversal analysis has its own `route_geometry_same_transversal_*` files. The street-review integration can also write `street_resolution_override_shadow.csv`.

Shadow artifacts are projections or alternatives. They are intentionally separate from the official pipeline.

## Human-review artifacts

Street review uses `street_resolution_human_review.csv`, `street_resolution_approved.csv`, and its report/alias exports. Geometry review uses `route_geometry_human_review.csv`, `route_geometry_approved.csv`, `route_geometry_rejected.csv`, and its report. These files preserve decisions and provenance; they are not dashboard inputs by default.
