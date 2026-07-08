"""USGS significant earthquakes (past week) × NPS lands.

Source: https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson
Keyless. Points; we buffer the epicenter with a coarse ShakeMap-informed radius
and intersect the buffer vs the NPS mask.

Buffer radii (rough ShakeMap MMI-VI reach — first-pass; refine in Phase 2):
  M<5   : 20 km
  M5-6  : 50 km
  M6-7  : 150 km
  M7+   : 300 km

Emits:
  data/quakes-nps.json
  data/quakes-nps-fragments.geojson
"""
from __future__ import annotations
import datetime
from pathlib import Path

from common import (
    get_json, write_json, write_geojson, fail, now_iso,
    load_nps_mask, load_geo_stack, intersect_hazard, DATA_DIR,
)

URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson"


def _buffer_km_for_mag(m: float) -> float:
    if m >= 7.0:
        return 300.0
    if m >= 6.0:
        return 150.0
    if m >= 5.0:
        return 50.0
    return 20.0


def _tone_for_mag(m: float) -> str:
    if m >= 7.0:
        return "critical"
    if m >= 6.0:
        return "elevated"
    if m >= 5.0:
        return "moderate"
    return "info"


def _buffer_wgs84(pt, radius_km: float, g):
    """Buffer a WGS84 point by N km via a metric projection round-trip.

    We use a per-point azimuthal-equidistant projection centered on the epicenter —
    accurate for the local buffer regardless of latitude/hemisphere. shapely's
    native `.buffer()` on WGS84 would be in degrees, which we don't want.

    Antimeridian safety: back-transform to WGS84 can yield vertices spanning
    ±180° for buffers near the dateline (e.g. Aleutian M7 → 300km buffer).
    Without care, shapely stitches a polygon that crosses the whole world and
    spuriously intersects unrelated NPS units. Fix: walk polygon vertices with
    an unwrap heuristic (any hop > 180° gets ±360-corrected), then split at
    ±180° and translate the two halves back into the canonical (-180, 180] frame.
    """
    from shapely.geometry import Polygon, box
    from shapely.affinity import translate
    lon, lat = pt.x, pt.y
    pyproj = g["pyproj"]
    aeqd = pyproj.CRS.from_proj4(
        f"+proj=aeqd +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs"
    )
    fwd = pyproj.Transformer.from_crs(4326, aeqd, always_xy=True).transform
    back = pyproj.Transformer.from_crs(aeqd, 4326, always_xy=True).transform
    pt_metric = g["transform"](fwd, pt)
    circle_metric = pt_metric.buffer(radius_km * 1000.0, resolution=64)
    circle_wgs84 = g["transform"](back, circle_metric)

    # Fast path: no antimeridian involvement.
    minx, _, maxx, _ = circle_wgs84.bounds
    if (maxx - minx) <= 180 and -180 <= minx and maxx <= 180:
        return circle_wgs84

    # Unwrap coords into a continuous longitude frame (may extend beyond ±180).
    coords = list(circle_wgs84.exterior.coords)
    prev = coords[0][0]
    unwrapped = []
    for x, y in coords:
        while x - prev > 180:
            x -= 360
        while x - prev < -180:
            x += 360
        unwrapped.append((x, y))
        prev = x
    if all(-180 <= x <= 180 for x, _ in unwrapped):
        return circle_wgs84  # didn't really wrap
    unwrapped_poly = Polygon(unwrapped)
    # make_valid on the unwrapped ring — the coordinate shift can introduce a
    # self-intersection in rare polar-buffer cases.
    if not unwrapped_poly.is_valid:
        from shapely.validation import make_valid
        unwrapped_poly = make_valid(unwrapped_poly)

    parts = []
    # West replica (shift +360): the "west" half seen at high longitudes.
    for xmin, xmax, shift in [(-540, -180, +360), (-180, 180, 0), (180, 540, -360)]:
        clip = box(xmin, -90, xmax, 90)
        piece = unwrapped_poly.intersection(clip)
        if piece.is_empty:
            continue
        if shift:
            piece = translate(piece, xoff=shift)
        parts.append(piece)
    if not parts:
        return circle_wgs84
    if len(parts) == 1:
        return parts[0]
    return g["unary_union"](parts)


def main() -> None:
    g = load_geo_stack()
    unit_records, tree = load_nps_mask()

    print(f"[{now_iso()}] Fetching USGS significant quakes…")
    try:
        d = get_json(URL, timeout=45)
    except Exception as e:  # noqa: BLE001
        fail(f"USGS fetch failed: {e}")

    feats = d.get("features") or []
    print(f"  {len(feats)} significant quakes (past 7 days)")

    items: list[dict] = []
    fragments: list[dict] = []
    max_mag = 0.0
    total_acres = 0.0

    for f in feats:
        p = f.get("properties") or {}
        try:
            mag = float(p.get("mag") or 0)
        except (TypeError, ValueError):
            mag = 0.0
        max_mag = max(max_mag, mag)
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        lng, lat = coords[0], coords[1]
        pt = g["Point"](lng, lat)
        try:
            buffer_geom = _buffer_wgs84(pt, _buffer_km_for_mag(mag), g)
        except Exception as e:  # noqa: BLE001
            print(f"  WARN buffer failed for {f.get('id')}: {e}")
            continue

        hits = intersect_hazard(buffer_geom, unit_records, tree)
        if not hits:
            continue

        acres_this = sum(h["acres_affected"] for h in hits)
        total_acres += acres_this
        ts = None
        if p.get("time"):
            try:
                ts = datetime.datetime.fromtimestamp(
                    p["time"] / 1000, datetime.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:  # noqa: BLE001
                pass

        tone = _tone_for_mag(mag)
        items.append({
            "id": f.get("id"),
            "title": f"M{mag} — " + (p.get("place") or "unknown"),
            "subtitle": (
                f"{len(hits)} NPS unit(s) within shake buffer · "
                f"{acres_this:,.0f} ac"
            ),
            "tone": tone,
            "ts": ts,
            "meta": {
                "mag": mag,
                "alert": p.get("alert"),
                "tsunami": p.get("tsunami"),
                "url": p.get("url"),
                "buffer_km": _buffer_km_for_mag(mag),
                "lat": lat,
                "lng": lng,
                "depth_km": coords[2] if len(coords) >= 3 else None,
                "affected_units": [
                    {
                        "unit_code": h["props"].get("unit_code"),
                        "unit_name": h["props"].get("unit_name"),
                        "region": h["props"].get("region"),
                        "state": h["props"].get("state"),
                        "acres": round(h["acres_affected"], 1),
                    }
                    for h in sorted(hits, key=lambda h: h["acres_affected"], reverse=True)
                ],
                "acres_affected_total": round(acres_this, 1),
            },
        })

        for h in hits:
            fragments.append({
                "type": "Feature",
                "properties": {
                    "hazard": "quake-buffer",
                    "severity": tone.upper(),
                    "tone": tone,
                    "mag": mag,
                    "buffer_km": _buffer_km_for_mag(mag),
                    "unit_code": h["props"].get("unit_code"),
                    "unit_name": h["props"].get("unit_name"),
                    "quake_id": f.get("id"),
                    "acres_affected": round(h["acres_affected"], 1),
                },
                "geometry": g["mapping"](h["fragment"]),
            })

    items.sort(key=lambda it: it["meta"].get("mag") or 0, reverse=True)
    n = len(items)
    headline_tone = _tone_for_mag(max_mag) if n else "good"

    payload = {
        "domain": "doiLands",
        "source": "quakes-nps",
        "status": "ok" if n else "empty",
        "generatedAt": now_iso(),
        "headline": {
            "label": "QUAKES × NPS (7d)",
            "value": str(n),
            "tone": headline_tone,
        },
        "metrics": [
            {"label": "AFFECTED UNITS", "value": str(len({
                u["unit_code"]
                for it in items
                for u in (it["meta"].get("affected_units") or [])
            })), "tone": "info"},
            {"label": "MAX MAG", "value": (f"{max_mag:.1f}" if max_mag else "—"),
             "tone": "elevated" if max_mag >= 6 else "info"},
            {"label": "ACRES AFFECTED", "value": f"{total_acres:,.0f}", "tone": "info"},
        ],
        "items": items,
        "note": None if n else "no significant quakes on NPS lands (7d)",
        "attribution": "USGS Earthquake Hazards Program · NPS boundaries",
    }
    DATA_DIR.mkdir(exist_ok=True)
    write_json(str(DATA_DIR / "quakes-nps.json"), payload)
    write_geojson(str(DATA_DIR / "quakes-nps-fragments.geojson"), {
        "type": "FeatureCollection",
        "features": fragments,
        "hazard": "quake-buffer",
        "attribution": "USGS Earthquake Hazards Program · NPS boundaries",
    })
    print(f"  DONE. {n} quakes affecting NPS · max mag {max_mag} · {total_acres:,.0f} acres · {len(fragments)} fragments")


if __name__ == "__main__":
    main()
