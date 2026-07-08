# prometheus-hazard-lands-data

Hazard × DOI-Lands intersection feeds for **PROMETHEUS** (DOI Emergency Management / EHSD). Reads the NPS boundary mask published by `prometheus-doi-lands-data` and intersects with three keyless federal hazard feeds.

**Phase 1: NPS only.** BLM / FWS / BIA / BOR fan-out in later phases.

## Files

| File | What |
|---|---|
| `data/fires-nps.json` | PanelPayload — NIFC WFIGS active wildfire perimeters that intersect NPS lands |
| `data/nws-alerts-nps.json` | PanelPayload — NOAA NWS active alerts (polygon geometry) that intersect NPS lands |
| `data/quakes-nps.json` | PanelPayload — USGS significant quakes (7d) whose ShakeMap-informed buffer intersects NPS lands |
| `data/fires-nps-fragments.geojson` | Intersected on-park polygon slivers for the map overlay |
| `data/nws-alerts-nps-fragments.geojson` | " |
| `data/quakes-nps-fragments.geojson` | " |
| `data/doi-lands-rollup.json` | **Single source of truth for the exec view** — totals + per-bureau + top-10 incidents across all hazards |
| `data/nps_visitation_annual.csv` | Static NPS annual visitation (public, ~50 largest units). v1 proxy for "visitors at risk"; live IRMA gated on EHSD sign-off |

## Cadence

Every 15 min (weather alerts drive the freshness need; quakes/fires re-run cheaply on the same schedule).

## Sources (keyless, verified 2026-07-08)

- **Fires:** NIFC WFIGS Interagency Perimeters Current — `services3.arcgis.com/T4QMspbfLg3qTGWY/.../WFIGS_Interagency_Perimeters_Current/FeatureServer/0`
- **Weather:** NOAA NWS `api.weather.gov/alerts/active` (requires User-Agent per NWS policy — we use `prometheus-doi-ehsd (contact: duppal@ios.doi.gov)`)
- **Quakes:** USGS `earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson`
- **Lands mask:** `prometheus-doi-lands-data` CDN

## Design decisions

- **Pipeline pre-intersects, not the browser.** PAD-US is ~655k polygons and can't live in a frontend. The mask + intersection stay server-side; the app fetches slim already-intersected results.
- **Area in EPSG:5070** (CONUS Albers equal-area) — good enough for CONUS + acceptable elsewhere for our use.
- **Quakes are buffered by mag-informed radii** (M<5→20km, M5-6→50km, M6-7→150km, M7+→300km). Coarse first-pass; refine in Phase 2 with ShakeMap MMI-VI polygons.
- **Fires read directly from NIFC** — not from any FIRESTORM data repo. Source-of-truth decoupling.
- **NWS alerts with zone-code-only geometry are skipped** in Phase 1 (they have no inline polygon). Zone-code resolution against NWS zone layers is Phase 2.

## Contract shape

`data/*-nps.json` conform to `PANELS_DATA_CONTRACT.md` in the prometheus-sa repo (v1) with `domain: "doiLands"`. Items carry `meta.affected_units` (list of NPS units hit) + `meta.acres_affected_total`. Fragment GeoJSON carries `severity` + `tone` per feature for INFERNO color mapping.

## Attribution

- **Fires:** NIFC WFIGS
- **Weather:** NOAA / NWS · api.weather.gov (public)
- **Quakes:** USGS Earthquake Hazards Program
- **Boundaries:** National Park Service — Land Resources Division

All sources are public-domain / open-government data.
