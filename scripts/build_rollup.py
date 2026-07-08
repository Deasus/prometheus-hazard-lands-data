"""Aggregate the 3 hazard × NPS feeds into data/doi-lands-rollup.json.

This is the single source of truth for the exec view (Secretary's first question):
'how many DOI acres are under active hazard right now?' Reads the outputs of the
3 hazard fetchers (already written this run) and produces a compact rollup with
per-bureau breakdown + top-10 incidents merged across all hazards.

MUST run AFTER the 3 fetchers in the GHA job.
"""
from __future__ import annotations
import json
import csv
from pathlib import Path

from common import write_json, now_iso, DATA_DIR


HAZARD_FILES = {
    "fire": "fires-nps.json",
    "weather": "nws-alerts-nps.json",
    "seismic": "quakes-nps.json",
}

VISITATION_CSV = "data/nps_visitation_annual.csv"


def _load_hazard(path: Path) -> dict:
    if not path.exists():
        return {"status": "missing", "items": []}
    try:
        return json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"  WARN could not parse {path}: {e}")
        return {"status": "error", "items": []}


def _load_visitation() -> dict[str, int]:
    """Per-UNIT_CODE annual visitation (static proxy)."""
    p = Path(VISITATION_CSV)
    if not p.exists():
        return {}
    out: dict[str, int] = {}
    with p.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = (row.get("UNIT_CODE") or row.get("unit_code") or "").strip()
            visits = row.get("VISITATION_ANNUAL") or row.get("annual_visitation") or "0"
            try:
                out[code] = int(float(str(visits).replace(",", "")))
            except (TypeError, ValueError):
                out[code] = 0
    return out


def main() -> None:
    print(f"[{now_iso()}] Building rollup…")

    hazards: dict[str, dict] = {k: _load_hazard(DATA_DIR / v) for k, v in HAZARD_FILES.items()}
    visitation = _load_visitation()

    # Per-bureau + per-unit affected roll-up. Unit is affected if ANY hazard touches it.
    affected_units: dict[str, dict] = {}     # unit_code -> {acres_affected_max, hazards:set}
    per_hazard_counts: dict[str, int] = {}
    per_hazard_acres: dict[str, float] = {}
    top_pool: list[dict] = []

    for hazard_key, payload in hazards.items():
        items = payload.get("items") or []
        per_hazard_counts[hazard_key] = len(items)
        acc = 0.0
        for it in items:
            acres_hit = (it.get("meta") or {}).get("acres_affected_total") or 0
            acc += acres_hit
            for u in ((it.get("meta") or {}).get("affected_units") or []):
                code = u.get("unit_code")
                if not code:
                    continue
                rec = affected_units.setdefault(code, {
                    "unit_code": code,
                    "unit_name": u.get("unit_name"),
                    "region": u.get("region"),
                    "state": u.get("state"),
                    "acres_affected_max": 0.0,
                    "hazards": set(),
                })
                rec["acres_affected_max"] = max(rec["acres_affected_max"], float(u.get("acres") or 0))
                rec["hazards"].add(hazard_key)
            # Add to the merge pool for top-incidents.
            top_pool.append({
                "hazard": hazard_key,
                "id": it.get("id"),
                "title": it.get("title"),
                "subtitle": it.get("subtitle"),
                "tone": it.get("tone"),
                "ts": it.get("ts"),
                "acres_affected_total": acres_hit,
                "meta_summary": {
                    "severity": (it.get("meta") or {}).get("severity"),
                    "mag": (it.get("meta") or {}).get("mag"),
                    "contained_pct": (it.get("meta") or {}).get("contained_pct"),
                    "unit_count": len((it.get("meta") or {}).get("affected_units") or []),
                },
            })
        per_hazard_acres[hazard_key] = round(acc, 1)

    # Deduplicate top-incidents by (hazard,id) implicitly (we only add once).
    tone_rank = {"critical": 4, "elevated": 3, "moderate": 2, "info": 1, "good": 0, "neutral": 0}
    top_pool.sort(
        key=lambda x: (tone_rank.get(x.get("tone") or "info", 1),
                       x.get("acres_affected_total") or 0),
        reverse=True,
    )
    top_incidents = top_pool[:10]

    # Total acres at risk (union upper bound: sum of max-per-unit — avoids
    # double-counting a unit that's in multiple hazards, but doesn't
    # geometry-union the actual fragments; that would be a Phase-2 refinement).
    acres_at_risk = round(sum(u["acres_affected_max"] for u in affected_units.values()), 1)

    # Visitors at risk (static proxy): sum annual visitation of affected units.
    visitors_proxy = sum(visitation.get(code, 0) for code in affected_units.keys())

    rollup = {
        "generated": now_iso(),
        "version": "v1",
        "totals": {
            "acres_at_risk": acres_at_risk,
            "facilities_at_risk": 0,   # v1 placeholder — Phase 4 NID/HIFLD
            "visitors_at_risk_annual_proxy": visitors_proxy,
            "visitors_note": (
                "annual-average proxy, not live; static NPS annual visitation joined by UNIT_CODE"
            ),
            "active_alerts": {
                "fire": per_hazard_counts.get("fire", 0),
                "weather": per_hazard_counts.get("weather", 0),
                "seismic": per_hazard_counts.get("seismic", 0),
            },
            "affected_units_count": len(affected_units),
        },
        "by_bureau": {
            "NPS": {
                "acres_at_risk": acres_at_risk,
                "units_affected": len(affected_units),
                "visitors_annual_proxy": visitors_proxy,
                "affected_units": [
                    {
                        "unit_code": u["unit_code"],
                        "unit_name": u["unit_name"],
                        "region": u["region"],
                        "state": u["state"],
                        "acres_affected_max": round(u["acres_affected_max"], 1),
                        "hazards": sorted(u["hazards"]),
                    }
                    for u in sorted(
                        affected_units.values(),
                        key=lambda r: r["acres_affected_max"],
                        reverse=True,
                    )
                ],
            },
        },
        "by_hazard_acres": per_hazard_acres,
        "top_incidents": top_incidents,
        "sources": {
            "fire": "NIFC WFIGS Current Perimeters",
            "weather": "NOAA NWS active alerts (api.weather.gov)",
            "seismic": "USGS Earthquake Hazards Program",
            "lands_mask": (
                "prometheus-doi-lands-data / NPS Land Resources Division"
            ),
        },
        "attribution": (
            "PROMETHEUS · DOI Emergency Management · " + now_iso()
        ),
    }
    write_json(str(DATA_DIR / "doi-lands-rollup.json"), rollup)
    print(
        f"  DONE. acres_at_risk={acres_at_risk:,.0f} · units_affected={len(affected_units)} · "
        f"visitors_proxy={visitors_proxy:,} · top={len(top_incidents)}"
    )


if __name__ == "__main__":
    main()
