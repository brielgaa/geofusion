# GeoFusion v1.0 release notes

GeoFusion v1.0 packages the completed operational geospatial reconstruction and resurfacing intelligence workflow for technical review and portfolio presentation.

## Included

- Context-aware street resolution and route reconstruction over the GeoSampa road network.
- Official geometry kept separate from route-quality shadow reconstruction.
- Independent geometry validation, boundary contradiction audit and boundary name recovery.
- Shadow-only Consensus Evidence with explicit conflict and insufficient-evidence states.
- Operational lookup services for streets, number ranges, coordinates, resurfacing records, surfaces and protection windows.
- Streamlit dashboard with Home, street lookup, protection, map, audit, quality, pipeline and about views.
- Persisted SQLite street lookup index with lazy geometry materialization.
- Regression, semantic-equivalence and protected-core validation artifacts.

## Validated state

- 375 tests passing locally.
- 5,022 resurfacing records in the current operational artifact set.
- 1,577 official geometries, or 31.4% of the resurfacing population.
- 1,000 / 1,000 persisted-index equivalence checks with zero mismatches.
- 217,212 indexed road segments in an approximately 53.4 MB SQLite index.
- Cold street-plus-number lookup improved from approximately 20.01 seconds to 2.20 milliseconds in the recorded benchmark.

## Known limitations

- Operational source data is not distributed.
- Shadow and estimated geometry remain explicitly non-official.
- Notification receipt dates are not execution dates.
- Coordinate lookup uses a heavier spatial path.
- An experimental image-geometry branch was evaluated through controlled feasibility studies and archived outside the production workflow.
- Public release authority and data redistribution still require owner review.

## Release boundary

This handoff does not create a Git tag, GitHub release or public deployment. It also does not change the geospatial core, lookup semantics, validators, aliases, Consensus rules or dashboard behavior.
