"""NIFC WFIGS active wildfire perimeters × NPS lands.

Source (keyless, live): NIFC WFIGS Interagency Perimeters Current
  services3.arcgis.com/T4QMspbfLg3qTGWY/.../WFIGS_Interagency_Perimeters_Current/FeatureServer/0

Read directly from NIFC — do NOT depend on any FIRESTORM data repo (per session
handoff: FIRESTORM is off-limits, and source is more decoupled anyway).

Emits:
  data/fires-nps.json
  data/fires-nps-fragments.geojson

Severity from IncidentSize × PercentContained:
  large + <25% contained -> critical
  large + <75% contained -> elevated
  medium/large + 75-99%  -> moderate
  else -> info
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

from common import (
    get_json, write_json, write_geojson, fail, now_iso,
    load_nps_mask, load_geo_stack, intersect_hazard, DATA_DIR,
)

NIFC_LAYER = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
)


def _fetch_all_fires() -> list[dict]:
    """Paginate WFIGS. 2000/query, current dataset is ~100-1500 fires."""
    from urllib.parse import urlencode
    out: list[dict] = []
    offset = 0
    while True:
        params = {
            "where": "1=1",
            "outFields": ",".join([
                "poly_IncidentName", "poly_GISAcres", "poly_DateCurrent",
                "poly_IRWINID", "poly_MapMethod", "poly_FeatureStatus",
                "attr_IncidentSize", "attr_PercentContained",
                "attr_FireBehaviorGeneral", "attr_FireCause",
                "attr_POOState", "attr_POOCounty", "attr_UniqueFireIdentifier",
                "attr_IncidentTypeCategory",
            ]),
            "outSR": 4326,
            "f": "geojson",
            "resultRecordCount": 2000,
            "resultOffset": offset,
            "returnGeometry": "true",
        }
        url = f"{NIFC_LAYER}?{urlencode(params)}"
        page = get_json(url, timeout=120)
        feats = page.get("features") or []
        if not feats:
            break
        out.extend(feats)
        if len(feats) < 2000:
            break
        offset += len(feats)
    return out


def _severity(acres: float | None, contained: float | None) -> tuple[str, int]:
    """Return (tone, rank)."""
    ac = acres or 0
    pc = contained if contained is not None else 0
    if ac >= 5000 and pc < 25:
        return ("critical", 4)
    if ac >= 1000 and pc < 75:
        return ("elevated", 3)
    if ac >= 100 and pc < 100:
        return ("moderate", 2)
    return ("info", 1)


def main() -> None:
    g = load_geo_stack()
    unit_records, tree = load_nps_mask()

    print(f"[{now_iso()}] Fetching NIFC WFIGS current perimeters…")
    try:
        raw = _fetch_all_fires()
    except Exception as e:  # noqa: BLE001
        fail(f"NIFC fetch failed: {e}")
    print(f"  {len(raw)} active fire perimeters")

    # WFIGS commonly holds multiple polygons per fire during morphology updates
    # (superseded + current; agency-owner splits). Group by IRWIN_ID (falls back
    # to poly_IncidentName) and union polygons — keep the newest DateCurrent's
    # properties.
    from collections import defaultdict
    grouped: dict[str, list[dict]] = defaultdict(list)
    for feat in raw:
        p = feat.get("properties") or {}
        key = (
            p.get("poly_IRWINID")
            or p.get("attr_UniqueFireIdentifier")
            or p.get("poly_IncidentName")
            or f"__anon_{id(feat)}"
        )
        grouped[key].append(feat)
    print(f"  grouped into {len(grouped)} unique fires (dedup by IRWIN_ID)")

    items: list[dict] = []
    fragments: list[dict] = []
    max_rank = 0
    total_acres = 0.0
    fires_on_nps = 0

    for key, features in grouped.items():
        # Pick newest by poly_DateCurrent for authoritative props; union all polys.
        features.sort(key=lambda f: (f.get("properties") or {}).get("poly_DateCurrent") or 0, reverse=True)
        newest = features[0]
        p = newest.get("properties") or {}
        polys = []
        for feat in features:
            geom_gj = feat.get("geometry")
            if not geom_gj:
                continue
            try:
                geom = g["shape"](geom_gj)
                if not geom.is_valid:
                    geom = g["make_valid"](geom)
                if not geom.is_empty:
                    polys.append(geom)
            except Exception:  # noqa: BLE001
                continue
        if not polys:
            continue
        try:
            fire_geom = g["unary_union"](polys) if len(polys) > 1 else polys[0]
        except Exception:  # noqa: BLE001
            continue

        hits = intersect_hazard(fire_geom, unit_records, tree)
        if not hits:
            continue
        fires_on_nps += 1

        # Acres from NIFC's authoritative field; fall back to computed geometry
        # area (EPSG:5070) when both NIFC fields are null. Prevents "severity=info"
        # for an early-stage megafire that has geometry but no reported IncidentSize.
        try:
            fire_acres = float(p.get("poly_GISAcres") or p.get("attr_IncidentSize") or 0)
        except (TypeError, ValueError):
            fire_acres = 0.0
        if fire_acres <= 0:
            from common import area_acres
            try:
                fire_acres = area_acres(fire_geom)
            except Exception:  # noqa: BLE001
                pass
        try:
            contained = float(p.get("attr_PercentContained") or 0)
        except (TypeError, ValueError):
            contained = 0.0

        tone, rank = _severity(fire_acres, contained)
        max_rank = max(max_rank, rank)

        acres_on_nps = sum(h["acres_affected"] for h in hits)
        total_acres += acres_on_nps

        # NIFC DateCurrent is epoch ms.
        ts = None
        dc = p.get("poly_DateCurrent")
        if isinstance(dc, (int, float)):
            try:
                ts = datetime.fromtimestamp(dc / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:  # noqa: BLE001
                pass

        name = p.get("poly_IncidentName") or "Unnamed fire"
        state = p.get("attr_POOState") or ""
        rep = fire_geom.representative_point() if not fire_geom.is_empty else None
        items.append({
            "id": p.get("poly_IRWINID") or p.get("attr_UniqueFireIdentifier"),
            "title": f"{name} ({state})" if state else name,
            "subtitle": (
                f"{fire_acres:,.0f} ac · {contained:.0f}% contained · "
                f"{len(hits)} NPS unit(s) · {acres_on_nps:,.0f} ac on-park"
            ),
            "tone": tone,
            "ts": ts,
            "meta": {
                "acres": fire_acres,
                "contained_pct": contained,
                "cause": p.get("attr_FireCause"),
                "behavior": p.get("attr_FireBehaviorGeneral"),
                "state": state,
                "county": p.get("attr_POOCounty"),
                "type": p.get("attr_IncidentTypeCategory"),
                "irwin_id": p.get("poly_IRWINID"),
                "lat": (rep.y if rep is not None else None),
                "lng": (rep.x if rep is not None else None),
                "acres_affected_total": round(acres_on_nps, 1),
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
            },
        })

        for h in hits:
            fragments.append({
                "type": "Feature",
                "properties": {
                    "hazard": "fire-perimeter",
                    "severity": tone.upper(),
                    "tone": tone,
                    "fire_name": name,
                    "acres": fire_acres,
                    "contained_pct": contained,
                    "irwin_id": p.get("poly_IRWINID"),
                    "unit_code": h["props"].get("unit_code"),
                    "unit_name": h["props"].get("unit_name"),
                    "acres_affected": round(h["acres_affected"], 1),
                },
                "geometry": g["mapping"](h["fragment"]),
            })

    items.sort(
        key=lambda it: (it["meta"].get("acres_affected_total") or 0),
        reverse=True,
    )
    n = len(items)
    headline_tone = (
        "critical" if max_rank >= 4 else "elevated" if max_rank >= 3 else
        "moderate" if max_rank >= 2 else "info" if n else "good"
    )

    payload = {
        "domain": "doiLands",
        "source": "fires-nps",
        "status": "ok" if n else "empty",
        "generatedAt": now_iso(),
        "headline": {
            "label": "FIRES ON NPS",
            "value": str(n),
            "tone": headline_tone,
        },
        "metrics": [
            {"label": "AFFECTED UNITS", "value": str(len({
                u["unit_code"]
                for it in items
                for u in (it["meta"].get("affected_units") or [])
            })), "tone": "info"},
            {"label": "ACRES ON-PARK", "value": f"{total_acres:,.0f}", "tone": "info"},
            {"label": "LARGE UNCONT.", "value": str(sum(
                1 for it in items
                if (it["meta"].get("acres") or 0) >= 5000
                and (it["meta"].get("contained_pct") or 0) < 25
            )), "tone": "critical" if max_rank >= 4 else "info"},
        ],
        "items": items,
        "note": None if n else "no active wildfire perimeters intersect NPS lands",
        "attribution": "NIFC WFIGS · NPS boundaries",
    }
    DATA_DIR.mkdir(exist_ok=True)
    write_json(str(DATA_DIR / "fires-nps.json"), payload)
    write_geojson(str(DATA_DIR / "fires-nps-fragments.geojson"), {
        "type": "FeatureCollection",
        "features": fragments,
        "hazard": "fire-perimeter",
        "attribution": "NIFC WFIGS · NPS boundaries",
    })
    print(f"  DONE. {n} fires on NPS · {total_acres:,.0f} on-park acres · {len(fragments)} fragments")


if __name__ == "__main__":
    main()
