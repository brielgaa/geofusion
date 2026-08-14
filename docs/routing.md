# Routing and geometry

`src/road_graph.py` contains `RoadGraph`, the shared graph implementation used by the official ETL and diagnostics. It builds a graph from real GeoSampa line segments, indexes spatial candidates, and caches route-related lookups.

## Route construction

The route call receives a principal street, `De` and `Até` context, optional reference coordinates, expected length, and optional CODLOG. It resolves the street and boundaries, checks intersections and connected components, selects a valid path, and returns the source segment geometry plus metadata.

The official ETL calls the existing `RoadGraph.route()` contract. Audits load the graph read-only and do not replace the returned geometry in official records.

## Failure modes

The pipeline preserves explicit failures such as unresolved streets, missing intersections, disconnected components, invalid geometry, and no valid path. These are written to the official failure report and are also used by diagnostic audits to prioritize investigation.

## Geometry classes

The geometry audit separates current official geometry from alternatives:

- `CONFIRMED`: geometry already present in the official data;
- `RECONSTRUCTED_HIGH`: reconstructed with stronger evidence;
- `RECONSTRUCTED_MEDIUM`: reconstructed but requiring validation;
- `ESTIMATED`: inferred from partial or weaker evidence;
- `UNRESOLVED`: no acceptable geometry was produced.

`SAME_TRANSVERSAL` is handled as a distinct diagnostic category when `De` and `Até` refer to the same transversal. Its output remains in the audit/shadow layer.
