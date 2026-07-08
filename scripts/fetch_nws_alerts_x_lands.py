"""NWS active alerts × NPS lands intersection.

Source (keyless, UA-required): https://api.weather.gov/alerts/active
Only alerts with polygon geometry can intersect (some alerts reference zones
by code with no inline polygon — we skip those for Phase 1; zone-code lookup
is a Phase-2 nice-to-have that requires fetching the NWS zones layer).

Emits:
  data/nws-alerts-nps.json         (PanelPayload)
  data/nws-alerts-nps-fragments.geojson

Severity → tone map (NWS CAP severity field):
  Extreme  -> critical
  Severe   -> elevated
  Moderate -> moderate
  Minor    -> info
  Unknown  -> neutral
"""
from __future__ import annotations
from pathlib import Path
from common import (
    get_json, write_json, write_geojson, fail, now_iso,
    load_nps_mask, load_geo_stack, intersect_hazard, DATA_DIR,
)

NWS_URL = "https://api.weather.gov/alerts/active"
NWS_HEADERS = {
    "User-Agent": "prometheus-doi-ehsd (contact: duppal@ios.doi.gov)",
    "Accept": "application/geo+json",
}

SEV_TONE = {
    "Extreme": "critical",
    "Severe": "elevated",
    "Moderate": "moderate",
    "Minor": "info",
    "Unknown": "neutral",
}
SEV_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1, "Unknown": 0}


def main() -> None:
    g = load_geo_stack()
    unit_records, tree = load_nps_mask()

    print(f"[{now_iso()}] Fetching NWS active alerts…")
    try:
        raw = get_json(NWS_URL, timeout=60, extra_headers=NWS_HEADERS)
    except Exception as e:  # noqa: BLE001
        fail(f"NWS fetch failed: {e}")

    features = raw.get("features") or []
    print(f"  {len(features)} active alerts total")

    items: list[dict] = []
    fragment_features: list[dict] = []
    skipped_no_geom = 0
    max_sev_rank = -1

    for f in features:
        p = f.get("properties") or {}
        geom_gj = f.get("geometry")
        if not geom_gj:
            skipped_no_geom += 1
            continue
        try:
            hazard_geom = g["shape"](geom_gj)
            if not hazard_geom.is_valid:
                hazard_geom = g["make_valid"](hazard_geom)
            if hazard_geom.is_empty:
                continue
        except Exception:  # noqa: BLE001
            continue

        hits = intersect_hazard(hazard_geom, unit_records, tree)
        if not hits:
            continue

        severity = p.get("severity") or "Unknown"
        tone = SEV_TONE.get(severity, "neutral")
        max_sev_rank = max(max_sev_rank, SEV_RANK.get(severity, 0))

        # One item per alert, but summarize affected units in subtitle.
        affected = sorted(
            hits, key=lambda h: h["acres_affected"], reverse=True
        )
        unit_names = [h["props"].get("unit_name") or h["props"].get("unit_code") for h in affected]
        total_acres_hit = sum(h["acres_affected"] for h in affected)

        # Effective end time — some alerts use `expires`, some `ends`.
        ts_end = p.get("ends") or p.get("expires")

        items.append({
            "id": p.get("id") or f.get("id"),
            "title": f"{p.get('event') or 'Alert'} — {p.get('areaDesc') or ''}",
            "subtitle": (
                f"{len(affected)} unit(s) · {total_acres_hit:,.0f} ac affected · "
                f"{severity}"
            ),
            "tone": tone,
            "ts": p.get("sent") or p.get("effective"),
            "meta": {
                "severity": severity,
                "event": p.get("event"),
                "urgency": p.get("urgency"),
                "certainty": p.get("certainty"),
                "sender": p.get("senderName"),
                "headline": p.get("headline"),
                "ends": ts_end,
                "acres_affected_total": round(total_acres_hit, 1),
                "affected_units": [
                    {
                        "unit_code": h["props"].get("unit_code"),
                        "unit_name": h["props"].get("unit_name"),
                        "region": h["props"].get("region"),
                        "state": h["props"].get("state"),
                        "acres": round(h["acres_affected"], 1),
                    }
                    for h in affected
                ],
                "lat": (hazard_geom.representative_point().y if not hazard_geom.is_empty else None),
                "lng": (hazard_geom.representative_point().x if not hazard_geom.is_empty else None),
            },
        })

        # Fragments = intersected slivers with severity for the map layer.
        for h in affected:
            frag = h["fragment"]
            fragment_features.append({
                "type": "Feature",
                "properties": {
                    "hazard": "nws-alert",
                    "severity": severity,
                    "tone": tone,
                    "event": p.get("event"),
                    "unit_code": h["props"].get("unit_code"),
                    "unit_name": h["props"].get("unit_name"),
                    "alert_id": p.get("id") or f.get("id"),
                    "acres_affected": round(h["acres_affected"], 1),
                },
                "geometry": g["mapping"](frag),
            })

    # Sort items by severity rank desc, then by acres affected.
    items.sort(
        key=lambda it: (
            SEV_RANK.get(it["meta"].get("severity") or "Unknown", 0),
            it["meta"].get("acres_affected_total") or 0,
        ),
        reverse=True,
    )

    n = len(items)
    total_acres = sum((it["meta"].get("acres_affected_total") or 0) for it in items)
    headline_tone = "critical" if max_sev_rank >= 4 else "elevated" if max_sev_rank >= 3 else \
                    "moderate" if max_sev_rank >= 2 else "info" if n else "good"

    payload = {
        "domain": "doiLands",
        "source": "nws-alerts-nps",
        "status": "ok" if n else "empty",
        "generatedAt": now_iso(),
        "headline": {
            "label": "NWS ALERTS ON NPS",
            "value": str(n),
            "tone": headline_tone,
        },
        "metrics": [
            {"label": "AFFECTED UNITS", "value": str(len({
                u["unit_code"]
                for it in items
                for u in (it["meta"].get("affected_units") or [])
            })), "tone": "info"},
            {"label": "ACRES AFFECTED", "value": f"{total_acres:,.0f}", "tone": "info"},
            {"label": "EXTREME/SEVERE", "value": str(sum(
                1 for it in items if it["meta"].get("severity") in ("Extreme", "Severe")
            )), "tone": "elevated" if max_sev_rank >= 3 else "info"},
        ],
        "items": items[:100],  # keep panel payload bounded
        "note": None if n else "no NWS alerts intersect NPS lands right now",
        "attribution": "NOAA NWS · api.weather.gov (public)",
    }
    if skipped_no_geom:
        payload["note"] = (
            (payload.get("note") or "")
            + f" · skipped {skipped_no_geom} alerts with zone-code-only geometry"
        ).strip(" ·")

    DATA_DIR.mkdir(exist_ok=True)
    write_json(str(DATA_DIR / "nws-alerts-nps.json"), payload)
    write_geojson(str(DATA_DIR / "nws-alerts-nps-fragments.geojson"), {
        "type": "FeatureCollection",
        "features": fragment_features,
        "hazard": "nws-alert",
        "attribution": "NOAA NWS · NPS boundaries",
    })
    print(f"  DONE. {n} intersecting alerts · {total_acres:,.0f} acres affected · {len(fragment_features)} fragments")


if __name__ == "__main__":
    main()
