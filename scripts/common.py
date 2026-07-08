"""Shared helpers for prometheus-hazard-lands-data.

Intersects hazard polygons (fire perimeters, weather alerts, quake shake buffers)
against the DOI-lands mask published by prometheus-doi-lands-data and emits:
  - data/<hazard>-nps.json                (PanelPayload — the panel/analytics feed)
  - data/<hazard>-nps-fragments.geojson   (the intersected polygon slivers for the map layer)

The lands mask is pulled from raw.githubusercontent every run — decouples us from
the mask pipeline's cadence. If the mask CDN 404s (fresh publish window), fail
loud so we don't ship a hazard payload that quietly ignored the mask entirely.
"""
from __future__ import annotations
import json, os, sys, urllib.request, urllib.error, urllib.parse, datetime, time
from pathlib import Path
from typing import Any

UA = "prometheus-hazard-lands/1.0 (+https://github.com/Deasus/prometheus-hazard-lands-data)"

# NPS mask CDN. Override with PROMETHEUS_MASK_URL for local dev (e.g. a file://
# URL or a locally-hosted preview of a pending mask commit).
MASK_URL = os.environ.get(
    "PROMETHEUS_MASK_URL",
    "https://raw.githubusercontent.com/Deasus/prometheus-doi-lands-data/"
    "main/data/lands-nps.geojson",
)

# EPSG:5070 NAD83 CONUS Albers for equal-area acreage. Same choice as lands pipeline.
ACRES_PER_M2 = 1.0 / 4046.8564224
DATA_DIR = Path("data")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url: str, timeout: int = 60, accept: str | None = None,
        extra_headers: dict[str, str] | None = None, retries: int = 3) -> bytes:
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    if extra_headers:
        headers.update(extra_headers)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_err}")


def get_json(url: str, timeout: int = 60, extra_headers: dict[str, str] | None = None):
    return json.loads(get(url, timeout, accept="application/json", extra_headers=extra_headers))


def fail(msg: str):
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def write_json(path: str, payload: dict) -> None:
    payload.setdefault("generated", now_iso())
    payload.setdefault("version", "v1")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"WROTE {path}  ({len(payload.get('items', []))} items)")


def write_geojson(path: str, fc: dict) -> None:
    fc.setdefault("type", "FeatureCollection")
    fc.setdefault("generated", now_iso())
    fc.setdefault("version", "v1")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False, separators=(",", ":"))
    print(f"WROTE {path}  ({len(fc.get('features', []))} features)")


# ---- geometry ----------------------------------------------------------------

_GEO_CACHE: dict[str, Any] = {}


def load_geo_stack():
    """Lazy-import shapely + pyproj so scripts that don't need them don't pay the cost."""
    if "loaded" in _GEO_CACHE:
        return _GEO_CACHE
    try:
        from shapely.geometry import shape, mapping, Point
        from shapely.ops import transform, unary_union
        from shapely.strtree import STRtree
        from shapely.validation import make_valid
        import pyproj
    except ImportError as e:
        fail(f"Missing geometry deps: {e}. Install shapely + pyproj.")
    _GEO_CACHE.update(
        shape=shape, mapping=mapping, Point=Point,
        transform=transform, unary_union=unary_union,
        STRtree=STRtree, make_valid=make_valid,
        pyproj=pyproj,
        to_albers=pyproj.Transformer.from_crs(4326, 5070, always_xy=True).transform,
        loaded=True,
    )
    return _GEO_CACHE


def area_acres(geom_wgs84) -> float:
    """Area in acres via EPSG:5070 equal-area projection."""
    g = load_geo_stack()
    projected = g["transform"](g["to_albers"], geom_wgs84)
    return projected.area * ACRES_PER_M2


def load_nps_mask() -> tuple[list[dict], Any]:
    """Fetch the published NPS mask and return (unit_records, STRtree).

    unit_records is a list of {props, geom} dicts. STRtree indexes the geoms for
    fast bbox pre-filter before doing the expensive intersection.
    """
    g = load_geo_stack()
    print(f"[{now_iso()}] Loading NPS mask from {MASK_URL}")
    try:
        fc = get_json(MASK_URL, timeout=90)
    except Exception as e:  # noqa: BLE001
        fail(f"NPS mask fetch failed: {e}")
    feats = fc.get("features") or []
    if not feats:
        fail("NPS mask has no features — publish step 1 first")
    unit_records: list[dict] = []
    for f in feats:
        geom_gj = f.get("geometry")
        if not geom_gj:
            continue
        try:
            geom = g["shape"](geom_gj)
            if not geom.is_valid:
                geom = g["make_valid"](geom)
            if geom.is_empty:
                continue
        except Exception:  # noqa: BLE001
            continue
        unit_records.append({"props": f.get("properties") or {}, "geom": geom})
    tree = g["STRtree"]([r["geom"] for r in unit_records])
    print(f"  loaded {len(unit_records)} NPS units into STRtree")
    return unit_records, tree


def intersect_hazard(
    hazard_geom, unit_records: list[dict], tree
) -> list[dict]:
    """Intersect a hazard polygon against all NPS units. Returns list of
    {unit_code, unit_name, unit_type, region, state, acres_affected, fragment}
    for units where the hazard actually overlaps."""
    g = load_geo_stack()
    hits = []
    # STRtree.query returns indices in shapely>=2, geoms in shapely<2 — handle both.
    result = tree.query(hazard_geom)
    if len(result) == 0:
        return hits
    try:
        # shapely >= 2 returns indices as numpy array of ints
        candidate_indices = [int(i) for i in result]
        candidates = [unit_records[i] for i in candidate_indices]
    except (TypeError, ValueError):
        # shapely 1.x returns geoms; rebuild the mapping.
        # Fallback: linear scan. This branch should be rare.
        return _intersect_hazard_linear(hazard_geom, unit_records)
    for rec in candidates:
        try:
            frag = hazard_geom.intersection(rec["geom"])
        except Exception:  # noqa: BLE001
            continue
        if frag.is_empty:
            continue
        acres = area_acres(frag)
        if acres < 0.1:  # below noise floor from geometry-precision jitter
            continue
        hits.append({
            "props": rec["props"],
            "fragment": frag,
            "acres_affected": acres,
        })
    return hits


def _intersect_hazard_linear(hazard_geom, unit_records):
    hits = []
    for rec in unit_records:
        if not hazard_geom.envelope.intersects(rec["geom"].envelope):
            continue
        try:
            frag = hazard_geom.intersection(rec["geom"])
        except Exception:  # noqa: BLE001
            continue
        if frag.is_empty:
            continue
        acres = area_acres(frag)
        if acres < 0.1:
            continue
        hits.append({"props": rec["props"], "fragment": frag, "acres_affected": acres})
    return hits
