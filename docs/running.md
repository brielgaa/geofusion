# Running GeoFusion

All commands below assume the repository root as the working directory and a prepared `.venv`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Put local source files in `data/raw/`. Do not commit those files or generated artifacts.

## Official pipeline and dashboard

```powershell
python src/transform.py
python -m streamlit run dashboard/app.py
```

The dashboard reads `cruzamento.csv`, `recape_clean.csv`, `notificacoes.csv`, `recapes_sem_cobertura.csv`, `geosampa_coverage_report.json`, and `pipeline_run.json`.

## Street audit

```powershell
python src/transform.py --audit-streets --resume
python src/transform.py --audit-streets --sample 100 --resume
python -m streamlit run src/street_resolution_review_app.py
python src/street_resolution_review.py --export-high-divergences --export-approved
```

The audit also accepts `--street-only`, `--skip-route-context`, `--reset-cache`, `--checkpoint-every`, and `--output-dir`.

## Geometry audit and review

```powershell
python src/transform.py --audit-route-geometries --resume
python src/route_geometry_audit.py --quality-shadow --resume
python src/route_geometry_audit.py --only-same-transversal --resume
python src/route_geometry_estimated_pareto.py
python -m streamlit run src/route_geometry_review_app.py
```

Use `--sample`, `--only-failure`, `--reset-cache`, and the review utility's report/export flags as described in [Geometry audit](route-geometry-audit.md) and [Human review](route-geometry-review.md).

## Validation

```powershell
python -m compileall -q src dashboard tests
python -m pytest -q
git diff --check
```
