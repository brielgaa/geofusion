# Local data boundary

The directories below are local operational inputs or generated artifacts and
are intentionally excluded from the public release:

- `data/raw/` — source workbooks, notification exports and other operational inputs;
- `data/processed/` — normalized records, geometry evidence, reports, logs and review artifacts;
- `data/cache/` — road-network caches, downloaded images and generated indexes.

Do not commit, upload or redistribute these directories without explicit data
ownership and publication approval. The operational SQLite lookup index is also
local-only; rebuild it locally from approved inputs when needed.

GeoFusion v1.0 does not include a public demo dataset. A future demo dataset is
a separate task requiring deliberate sanitization and approval.
