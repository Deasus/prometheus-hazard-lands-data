"""Aggregate the 3 hazard × NPS feeds into data/doi-lands-rollup.json.

This is the single source of truth for the exec view (Secretary's first question):
'how many DOI acres are under active hazard right now?' Reads the outputs of the
3 hazard fetchers (already written this run) and produces a compact rollup with
per-bureau breakdown + top-10 incidents merged across all hazards.

MUST run AFTER the 3 fetchers in the GHA job.

Failure semantics:
  - If ALL 3 hazard feeds are missing/errored, refuse to overwrite the previous
    rollup — set status="stale" on the on-disk copy and exit with a warning. A
    "0 hazards" rollup published when everything failed reads as "all clear" to
    the exec view, which is a false all-clear.
  - Deduplicate per-hazard by unit_code (Red Flag + Fire Weather Watch on the
    same unit → count once) so per-hazard acres aren't inflated.
  - acres_at_risk_lower_bound = sum(max acres per affected unit) — labeled as
    a lower bound (true value is unary_union of fragments, which we defer to
    Phase 2 for cost reasons).
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
ROLLUP_PATH = DATA_DIR / "doi-lands-rollup.json"


def _load_hazard(path: Path) -> dict:
    if not path.exists():
        return {"status": "missing", "items": []}
    try:
        return json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"  WARN could not parse {path}: {e}")
        return {"status": "error", "items": []}


def _load_visitation() -> dict[str, int]:
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


def _refuse_all_missing(hazards: dict[str, dict]) -> bool:
    """Return True if every hazard feed is missing/errored — do NOT publish."""
    return all(h.get("status") in ("missing", "error") for h in hazards.values())


def _mark_stale_and_exit():
    """Preserve the previous rollup; add a stale marker in-place."""
    if not ROLLUP_PATH.exists():
        print("  ERROR: no prior rollup to mark stale; skipping publish.")
        return
    try:
        prev = json.loads(ROLLUP_PATH.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: could not read prior rollup for stale-mark: {e}")
        return
    prev["status"] = "stale"
    prev.setdefault("stale_notes", []).append({
        "detected_at": now_iso(),
        "reason": "all three hazard feeds missing or errored on this run — preserving prior data",
    })
    write_json(str(ROLLUP_PATH), prev)
    print("  STALE: preserved prior rollup with status=stale.")


def main() -> None:
    print(f"[{now_iso()}] Building rollup…")

    hazards: dict[str, dict] = {k: _load_hazard(DATA_DIR / v) for k, v in HAZARD_FILES.items()}

    if _refuse_all_missing(hazards):
        print("  REFUSING to publish: all three hazard feeds missing/errored.")
        _mark_stale_and_exit()
        return

    visitation = _load_visitation()

    # Per-hazard dedup by unit_code first: an alert AND a duplicate alert on the
    # same unit should only count that unit once for that hazard's roll-up.
    per_hazard_counts: dict[str, int] = {}
    per_hazard_acres: dict[str, float] = {}
    per_hazard_units: dict[str, set[str]] = {}
    affected_units: dict[str, dict] = {}     # unit_code -> summary
    top_pool: list[dict] = []

    for hazard_key, payload in hazards.items():
        items = payload.get("items") or []
        per_hazard_counts[hazard_key] = len(items)
        per_hazard_units[hazard_key] = set()
        # unit_code -> max acres_affected within THIS hazard
        this_hazard_by_unit: dict[str, dict] = {}
        for it in items:
            for u in ((it.get("meta") or {}).get("affected_units") or []):
                code = u.get("unit_code")
                if not code:
                    continue
                per_hazard_units[hazard_key].add(code)
                acres_this = float(u.get("acres") or 0)
                cur = this_hazard_by_unit.get(code)
                if cur is None or acres_this > cur["acres"]:
                    this_hazard_by_unit[code] = {
                        "unit_code": code,
                        "unit_name": u.get("unit_name"),
                        "region": u.get("region"),
                        "state": u.get("state"),
                        "acres": acres_this,
                    }
                # Cross-hazard rollup: track max acres affecting each unit
                rec = affected_units.setdefault(code, {
                    "unit_code": code,
                    "unit_name": u.get("unit_name"),
                    "region": u.get("region"),
                    "state": u.get("state"),
                    "acres_affected_max": 0.0,
                    "hazards": set(),
                })
                rec["acres_affected_max"] = max(rec["acres_affected_max"], acres_this)
                rec["hazards"].add(hazard_key)
            # Top-incident merge pool (per-item, not per-unit).
            top_pool.append({
                "hazard": hazard_key,
                "id": it.get("id"),
                "title": it.get("title"),
                "subtitle": it.get("subtitle"),
                "tone": it.get("tone"),
                "ts": it.get("ts"),
                "acres_affected_total": (it.get("meta") or {}).get("acres_affected_total") or 0,
                "meta_summary": {
                    "severity": (it.get("meta") or {}).get("severity"),
                    "mag": (it.get("meta") or {}).get("mag"),
                    "contained_pct": (it.get("meta") or {}).get("contained_pct"),
                    "unit_count": len((it.get("meta") or {}).get("affected_units") or []),
                },
            })
        per_hazard_acres[hazard_key] = round(sum(u["acres"] for u in this_hazard_by_unit.values()), 1)

    tone_rank = {"critical": 4, "elevated": 3, "moderate": 2, "info": 1, "good": 0, "neutral": 0}
    top_pool.sort(
        key=lambda x: (tone_rank.get(x.get("tone") or "info", 1),
                       x.get("acres_affected_total") or 0),
        reverse=True,
    )
    top_incidents = top_pool[:10]

    # Cross-hazard acres_at_risk: sum of max-per-unit across all affected units.
    # This is a LOWER BOUND on the true geometric union (a hazard could touch
    # part of a unit that another hazard doesn't, and the max-per-unit hides
    # the disjoint contribution). Labeled as such.
    acres_at_risk_lb = round(sum(u["acres_affected_max"] for u in affected_units.values()), 1)

    # Visitor proxy honesty: how many affected units are actually in the CSV.
    affected_codes = set(affected_units.keys())
    covered_codes = affected_codes & set(visitation.keys())
    uncovered_codes = affected_codes - set(visitation.keys())
    visitors_proxy = sum(visitation.get(code, 0) for code in affected_codes)
    coverage_pct = (
        round(100 * len(covered_codes) / max(len(affected_codes), 1), 1)
        if affected_codes else 100.0
    )
    visitor_note_parts = [
        "annual-average proxy, not live; static NPS annual visitation joined by UNIT_CODE",
        f"coverage: {len(covered_codes)}/{len(affected_codes)} affected units in CSV ({coverage_pct}%)",
    ]
    if uncovered_codes:
        visitor_note_parts.append(
            f"missing visitation for: {', '.join(sorted(uncovered_codes)[:10])}"
            + (" ..." if len(uncovered_codes) > 10 else "")
        )

    # Freshness: any hazard file older than 30 min triggers a stale marker on
    # this rollup (the pipeline itself might still be healthy; we're being
    # honest about the feeds).
    from datetime import datetime, timezone
    stale_hazards = []
    for k, payload in hazards.items():
        ga = payload.get("generatedAt") or payload.get("generated")
        if not ga:
            continue
        try:
            t = datetime.strptime(ga, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - t).total_seconds() / 60
            if age_min > 30:
                stale_hazards.append({"hazard": k, "age_min": round(age_min, 1)})
        except Exception:  # noqa: BLE001
            pass

    rollup = {
        "generated": now_iso(),
        "generatedAt": now_iso(),  # contract-facing alias
        "version": "v1",
        "status": "ok" if not stale_hazards else "stale",
        "totals": {
            # Lower-bound label matches implementation semantics.
            "acres_at_risk_lower_bound": acres_at_risk_lb,
            # Alias kept for the exec view; identical value, honest name is the LB.
            "acres_at_risk": acres_at_risk_lb,
            "facilities_at_risk": 0,   # v1 placeholder — Phase 4 NID/HIFLD
            "visitors_at_risk_annual_proxy": visitors_proxy,
            "visitors_coverage_pct": coverage_pct,
            "visitors_units_covered": len(covered_codes),
            "visitors_units_missing": len(uncovered_codes),
            "visitors_note": " · ".join(visitor_note_parts),
            "active_alerts": {
                "fire": per_hazard_counts.get("fire", 0),
                "weather": per_hazard_counts.get("weather", 0),
                "seismic": per_hazard_counts.get("seismic", 0),
            },
            "affected_units_count": len(affected_units),
        },
        "by_bureau": {
            "NPS": {
                "acres_at_risk_lower_bound": acres_at_risk_lb,
                "acres_at_risk": acres_at_risk_lb,
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
        "by_hazard_units_dedup": {k: len(v) for k, v in per_hazard_units.items()},
        "top_incidents": top_incidents,
        "stale_hazards": stale_hazards,
        "sources": {
            "fire": "NIFC WFIGS Current Perimeters",
            "weather": "NOAA NWS active alerts (api.weather.gov)",
            "seismic": "USGS Earthquake Hazards Program",
            "lands_mask": "prometheus-doi-lands-data / NPS Land Resources Division",
        },
        "notes": {
            "acres_at_risk_semantics": (
                "acres_at_risk = sum(max acres per unit) — a LOWER BOUND on the "
                "true geometric union of hazard fragments. True union is a Phase-2 "
                "refinement; the lower bound never over-counts."
            ),
            "hazard_dedup": "per-hazard acres deduplicated by unit_code within a hazard",
        },
        "attribution": (
            "PROMETHEUS · DOI Emergency Management · " + now_iso()
        ),
    }
    write_json(str(ROLLUP_PATH), rollup)
    print(
        f"  DONE. acres_at_risk_lb={acres_at_risk_lb:,.0f} · units_affected={len(affected_units)} · "
        f"visitors_proxy={visitors_proxy:,} ({coverage_pct}% coverage) · top={len(top_incidents)}"
    )
    if stale_hazards:
        print(f"  STALE hazards: {stale_hazards}")


if __name__ == "__main__":
    main()
