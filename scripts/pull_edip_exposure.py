"""DOI-wide (5-bureau) fire exposure — PULLED from EDIP Gold, not computed here.

WHY THIS EXISTS
    Every other script in this repo COMPUTES a hazard x land intersection locally against
    the NPS mask. This one does not, and deliberately so.

    Prometheus's DOI-LANDS vertical covered National Park Service land only: 422 units.
    That understated DOI exposure BY CONSTRUCTION, not by a bug. Measured against EDIP's
    wildfire Gold layer on 2026-08-17, with designation overlays filtered:

        BLM   79 units /  8,389 detections
        TRIB  45 units /  5,768 detections     <-- BIA land; PAD-US codes it TRIB
        NPS   17 units /  3,478 detections
        FWS   10 units /    113 detections
        USBR   2 units /     12 detections
        ----------------------------------
        TOTAL 153 units / 17,760 detections

    NPS alone is 17 of 153 units (11%) and 20% of detections. An NPS-only panel misses
    roughly nine tenths of the exposed DOI estate.

WHY WE PULL INSTEAD OF COMPUTING
    The obvious fix -- add BLM/FWS/USBR/TRIB to the boundary mask in
    prometheus-doi-lands-data -- is not viable. Measured 2026-08-17 against the PAD-US 4.1
    FeatureServer, four bureaus of polygon geometry come to:

        ~111 m generalization (what NPS uses today)   378 MB
        ~555 m                                        230 MB
        ~1.1 km                                       173 MB
        ~2.2 km                                       154 MB

    GitHub's hard per-file limit is 100 MB. There is no tolerance that both fits the CDN
    and stays precise enough to intersect a fire perimeter -- BLM units are too large and
    too intricate for generalization to help. EDIP already holds this geometry in Delta and
    already computes the intersection with real ST_Contains, so we pull the ANSWER (a few
    hundred KB) rather than the INPUT (378 MB).

    Direction is Prometheus-pulls, not EDIP-pushes, because EDIP's S3 bucket lives in a
    different AWS account and is not internet-readable (verified: anonymous GET -> 403), and
    a cross-account bucket policy is denied by a Resource Control Policy -- no principal-side
    change defeats an RCP, so it would need a ticket. A cron here needs nothing from anyone.

THREE TRAPS THIS QUERY HANDLES (all verified live, all silent if you get them wrong)
    1. BIA land is coded 'TRIB' in PAD-US, never 'BIA'. Filtering on 'BIA' returns ZERO
       rows -- a clean empty result that looks like "no tribal land is exposed".
    2. is_designation_overlay must be FILTERED, not merely present. PAD-US stacks
       designations on their parent unit (1,037 BLM ACECs, 490 WSAs), so the same ground
       appears twice. Unfiltered BLM reads 107 units; the honest count is 79.
    3. Marine national monuments. silver.padus_land reports FWS at 3.2 BILLION acres --
       2,868.9M of it from 21 'NM' polygons that are Pacific marine monuments
       (Papahaanaumokuaakea, Pacific Remote Islands, Marianas Trench). Des_Tp NOT IN
       ('CONE','MPA') excludes marine protected areas but NOT marine monuments. No such
       unit currently reaches gold.exposure (fires do not burn mid-Pacific, max unit is
       23.5M acres), so MAX_PLAUSIBLE_UNIT_ACRES is precautionary HERE -- but it becomes
       load-bearing the moment this pattern is reused for storm or alert exposure, where a
       Pacific system would otherwise publish "3.2 billion acres at risk". US total land
       area is 2.3 billion.

AUTH
    GHA:   DATABRICKS_HOST + DATABRICKS_TOKEN (service principal). Direct REST.
    Local: falls back to the `databricks` CLI with EDIP_PROFILE, so an operator already
           signed in needs no token. Note the CLI needs --profile passed EXPLICITLY; a
           dropped --profile silently uses [DEFAULT] and fails as an OAuth error.

Emits:
    data/doi-exposure-5bureau.json   PanelPayload + an `exec` block, per
                                     prometheus-sa/docs/PANELS_DATA_CONTRACT.md v1
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from common import DATA_DIR, fail, now_iso, write_json

# ---- config ------------------------------------------------------------------

# ⚠️ NO DEFAULTS FOR INFRASTRUCTURE IDENTIFIERS. This repo is PUBLIC, so anything hardcoded
# here is served from raw.githubusercontent.com. None of these is a credential and gitleaks
# passes them all, but a workspace hostname + warehouse id + catalog path together are free
# reconnaissance on a DOI Databricks workspace — a named endpoint to aim credential-stuffing
# at. Supply them by env (locally) or repo secret/var (CI); fail loudly if absent.
WAREHOUSE = os.environ.get("EDIP_WAREHOUSE", "").strip()
EDIP_PROFILE = os.environ.get("EDIP_PROFILE", "").strip()
CATALOG = os.environ.get("EDIP_CATALOG", "").strip()

OUT_PATH = str(DATA_DIR / "doi-exposure-5bureau.json")

# Guards. Each one turns a silent wrong answer into a loud failure.
MIN_BUREAUS = 2                       # the ENTIRE point of this feed is >1 bureau
MAX_PLAUSIBLE_UNIT_ACRES = 50_000_000  # see trap 3 -- marine-monument tripwire
TOP_N_ITEMS = 12

# DOI bureau display names. TRIB is PAD-US's code for BIA-administered tribal land; the
# panel must say BIA because that is what an operator recognises.
BUREAU_LABEL = {
    "BLM": "BLM",
    "TRIB": "BIA",
    "NPS": "NPS",
    "FWS": "FWS",
    "USBR": "BOR",
}

def _sql(catalog: str) -> str:
    # ⚠️ Column names read from DESCRIBE TABLE, not guessed.
    # Built here rather than at import so an unset EDIP_CATALOG cannot silently produce
    # "FROM .gold.exposure" — which the warehouse would reject with a parse error that
    # names SQL syntax rather than the missing configuration.
    return f"""
SELECT bureau, unit_code, unit_code_source, unit_name, unit_type, unit_state,
       acres, hotspot_cells, detections, max_frp_mw, multi_sensor_cells,
       incident_names, last_detection_at, built_at
FROM {catalog}.gold.exposure
WHERE NOT is_designation_overlay
  AND detections > 0
ORDER BY detections DESC
"""


def _require_config() -> None:
    """Fail with the fix, not a downstream symptom."""
    missing = [n for n, v in (("EDIP_WAREHOUSE", WAREHOUSE), ("EDIP_CATALOG", CATALOG)) if not v]
    if missing:
        fail("missing required env: " + ", ".join(missing)
             + ". These are deliberately not defaulted — this repo is public. "
             + "Locally: export them (see README). In CI: set repo secrets/vars.")
    # Authentication: any ONE of the four modes in query() must be fully configured.
    has_arn = bool(os.environ.get("EDIP_OAUTH_SECRET_ARN"))
    has_oauth = bool(os.environ.get("DATABRICKS_CLIENT_ID")
                     and os.environ.get("DATABRICKS_CLIENT_SECRET")
                     and os.environ.get("DATABRICKS_HOST"))
    has_bearer = bool(os.environ.get("DATABRICKS_HOST")
                      and os.environ.get("DATABRICKS_TOKEN"))
    if not (has_arn or has_oauth or has_bearer or EDIP_PROFILE):
        fail("no usable auth. Set ONE of: EDIP_OAUTH_SECRET_ARN (preferred); "
             "DATABRICKS_CLIENT_ID+DATABRICKS_CLIENT_SECRET+DATABRICKS_HOST; "
             "DATABRICKS_TOKEN+DATABRICKS_HOST; or EDIP_PROFILE for the local CLI.")


# ---- warehouse access --------------------------------------------------------

def _oauth_bearer(host: str, client_id: str, client_secret: str,
                  token_endpoint: str = "") -> str:
    """Exchange an OAuth M2M client-credentials pair for a short-lived bearer token.

    ⚠️ A Databricks service-principal OAuth SECRET IS NOT A BEARER TOKEN. Sending it
    straight to /api/2.0/sql/statements returns 401, which reads as "bad credential" and
    sends you back to the admin who issued a perfectly good one. It must be exchanged at
    the account token endpoint first, with HTTP Basic auth and scope=all-apis.
    """
    endpoint = token_endpoint or (host.rstrip("/") + "/oidc/v1/token")
    # Verified against the workspace's own /oidc/.well-known/oauth-authorization-server
    # 2026-08-18: token_endpoint is <host>/oidc/v1/token, client_credentials is supported,
    # client_secret_basic (HTTP Basic) is an accepted auth method, and all-apis is a valid
    # scope. Bad credentials return 401 invalid_client — an OAuth body, not a 404 — which
    # confirms the path rather than merely the host being alive.
    #
    # ⚠️ SCOPE IS DELIBERATELY OVERRIDABLE AND DEFAULTS WIDE. all-apis grants everything the
    # principal can do; this job only runs SELECT via the Statement API, and the workspace
    # also advertises a narrower `sql` scope. all-apis is the default only because it is the
    # documented, known-working value — narrowing it is untested here (no credential to try
    # it with). Once the feed is live, set EDIP_OAUTH_SCOPE=sql and confirm the pull still
    # succeeds; if it does, keep it narrow.
    scope = os.environ.get("EDIP_OAUTH_SCOPE", "").strip() or "all-apis"
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": scope,
    }).encode()
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        fail(f"OAuth exchange HTTP {e.code} at {endpoint}: "
             f"{e.read()[:300].decode(errors='replace')}")
    except Exception as e:  # noqa: BLE001
        fail(f"OAuth exchange failed at {endpoint}: {e}")
    tok = (d or {}).get("access_token", "")
    if not tok:
        fail(f"OAuth exchange returned no access_token (keys: {sorted((d or {}).keys())})")
    print(f"  auth: OAuth M2M exchange OK (expires_in={d.get('expires_in')}s)")
    return tok


def _from_secrets_manager(arn: str) -> dict:
    """Read the credential blob from AWS Secrets Manager.

    Preferred path: the secret never lands in a repo secret, a log or a transcript. Needs an
    AWS identity in the account that OWNS the secret. Note that account may NOT be the one
    a CI runner or a laptop is signed into — a cross-account read requires a resource policy
    ON the secret, and its absence fails as AccessDenied naming no resource-based policy.
    """
    try:
        import boto3  # noqa: PLC0415
    except ImportError:
        fail("EDIP_OAUTH_SECRET_ARN is set but boto3 is not installed.")
    region = arn.split(":")[3] if arn.startswith("arn:") else "us-west-2"
    try:
        sm = boto3.client("secretsmanager", region_name=region)
        raw = sm.get_secret_value(SecretId=arn)["SecretString"]
    except Exception as e:  # noqa: BLE001
        fail(f"could not read {arn}: {e}")
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        fail("secret is not JSON; expected client_id/client_secret/token_endpoint/...")
    # Surface an approaching expiry rather than discovering it as a 401 at 3am. The secret
    # has a fixed lifetime and nothing upstream monitors it.
    try:
        meta = sm.describe_secret(SecretId=arn)
        for k in ("NextRotationDate", "ExpirationDate"):
            if meta.get(k):
                days = (meta[k] - datetime.datetime.now(datetime.timezone.utc)).days
                if days <= 21:
                    print(f"  ⚠️ CREDENTIAL EXPIRES IN {days} DAY(S) ({meta[k]:%Y-%m-%d}) "
                          "— rotate before it lapses; this feed stops silently otherwise.")
                break
    except Exception:  # noqa: BLE001, S110
        pass  # advisory only; never block the pull on a metadata read
    return blob


def _run_sql_via_token(host: str, token: str, statement: str) -> dict:
    body = json.dumps({
        "warehouse_id": WAREHOUSE,
        "statement": statement,
        "wait_timeout": "50s",
    }).encode()
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/2.0/sql/statements",
        data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        fail(f"warehouse HTTP {e.code}: {e.read()[:400].decode(errors='replace')}")
    except Exception as e:  # noqa: BLE001
        fail(f"warehouse request failed: {e}")


def _run_sql_via_cli(statement: str) -> dict:
    payload = json.dumps({
        "warehouse_id": WAREHOUSE,
        "statement": statement,
        "wait_timeout": "50s",
    })
    # ⚠️ --profile is REQUIRED. Omitting it uses [DEFAULT] and fails with
    # "Unable to load OAuth Config", which reads as a credential problem rather than a
    # dropped argument. Same defect as scripts/edip-sql carried until 2026-08-17.
    p = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--profile", EDIP_PROFILE, "--json", payload],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        fail("databricks CLI failed (no DATABRICKS_TOKEN, and CLI auth unusable): "
             + p.stderr[:400])
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as e:
        fail(f"CLI returned non-JSON: {e}: {p.stdout[:200]}")


def query(statement: str) -> list[dict]:
    """Run read-only SQL and return a list of dicts. Fails loud on anything unexpected.

    Four auth modes, in preference order. The first that is fully configured wins.
      1. EDIP_OAUTH_SECRET_ARN  — read client_id/secret from AWS Secrets Manager, exchange.
                                  Best: the credential never enters a repo secret or a log.
                                  Requires an AWS identity in the secret's OWNING account.
      2. DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET — exchange, then Bearer.
                                  Use when CI has no AWS identity in that account.
      3. DATABRICKS_TOKEN       — already-a-bearer (PAT, or a token exchanged elsewhere).
      4. databricks CLI profile — local operator convenience, no secret handling at all.
    """
    host = os.environ.get("DATABRICKS_HOST", "").strip()
    token = os.environ.get("DATABRICKS_TOKEN", "").strip()
    cid = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
    csec = os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
    arn = os.environ.get("EDIP_OAUTH_SECRET_ARN", "").strip()
    endpoint = ""

    if arn:
        blob = _from_secrets_manager(arn)
        cid = cid or blob.get("client_id", "")
        csec = csec or blob.get("client_secret", "")
        endpoint = blob.get("token_endpoint", "")
        host = host or blob.get("workspace_host", "")
        print("  auth: credential from Secrets Manager")

    if cid and csec:
        if not host:
            fail("OAuth client credentials present but no workspace host "
                 "(set DATABRICKS_HOST, or workspace_host in the secret blob).")
        d = _run_sql_via_token(host, _oauth_bearer(host, cid, csec, endpoint), statement)
    elif host and token:
        print("  auth: DATABRICKS_TOKEN (pre-exchanged bearer)")
        d = _run_sql_via_token(host, token, statement)
    else:
        print(f"  auth: databricks CLI, profile={EDIP_PROFILE}")
        d = _run_sql_via_cli(statement)

    state = (d.get("status") or {}).get("state")
    if state != "SUCCEEDED":
        # A silent empty result is how a typo gets mistaken for "no rows" on this platform.
        fail(f"statement state={state}: {json.dumps(d.get('status') or {})[:400]}")

    cols = [c["name"] for c in d["manifest"]["schema"]["columns"]]
    rows = (d.get("result") or {}).get("data_array") or []
    return [dict(zip(cols, r)) for r in rows]


def _f(v) -> float:
    """The SQL Statement API returns EVERY value as a string, including numbers."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    return int(_f(v))


# ---- main --------------------------------------------------------------------

def main() -> None:
    _require_config()
    print(f"[{now_iso()}] Pulling DOI 5-bureau fire exposure from EDIP Gold…")
    rows = query(_sql(CATALOG))
    print(f"  {len(rows)} exposed units returned")

    if not rows:
        # Zero is a legitimate state (no fire on DOI land) but it is ALSO what a broken
        # query looks like. Refuse to publish an unqualified zero; a consumer cannot tell
        # "nothing is burning" from "our query is wrong".
        fail("gold.exposure returned zero exposed units. Refusing to publish. Check "
             f"{CATALOG}.ops.v_freshness before believing this is real.")

    # ---- guard: marine-monument tripwire (trap 3) ----------------------------
    implausible = [r for r in rows if _f(r["acres"]) > MAX_PLAUSIBLE_UNIT_ACRES]
    if implausible:
        names = ", ".join(f"{r['bureau']}:{r['unit_name']} "
                          f"({_f(r['acres'])/1e6:.0f}M ac)" for r in implausible[:5])
        fail(f"{len(implausible)} unit(s) exceed {MAX_PLAUSIBLE_UNIT_ACRES/1e6:.0f}M acres "
             f"— almost certainly a marine national monument leaking through the land "
             f"filter: {names}. Fix the upstream Des_Tp filter; do NOT raise this bound.")

    # ---- guard: the regression that created this feed ------------------------
    by_bureau: dict[str, dict] = {}
    for r in rows:
        b = r["bureau"]
        agg = by_bureau.setdefault(b, {"units": 0, "detections": 0, "acres": 0.0})
        agg["units"] += 1
        agg["detections"] += _i(r["detections"])
        agg["acres"] += _f(r["acres"])

    if len(by_bureau) < MIN_BUREAUS:
        fail(f"only {len(by_bureau)} bureau(s) present ({', '.join(by_bureau)}). This feed "
             "exists BECAUSE the NPS-only view was wrong; a single-bureau result means the "
             "PAD-US union regressed upstream. Refusing to publish.")

    total_units = len(rows)
    total_det = sum(_i(r["detections"]) for r in rows)
    nps_units = by_bureau.get("NPS", {}).get("units", 0)
    nps_det = by_bureau.get("NPS", {}).get("detections", 0)

    # ---- items ---------------------------------------------------------------
    items = []
    for r in rows[:TOP_N_ITEMS]:
        det = _i(r["detections"])
        blabel = BUREAU_LABEL.get(r["bureau"], r["bureau"])
        tone = "critical" if det >= 500 else "elevated" if det >= 100 else "info"
        incidents = (r.get("incident_names") or "").strip()
        sub = f"{blabel} · {_f(r['acres'])/1e3:,.0f}K ac · {_i(r['hotspot_cells'])} cells"
        if incidents:
            sub += f" · {incidents[:60]}"
        items.append({
            # unit_code is SYNTHETIC ('BUREAU:Unit Name') for every non-NPS row. Namespaced
            # so a consumer cannot mistake it for an agency identifier.
            "id": f"edip:{r['unit_code']}",
            "title": r["unit_name"] or r["unit_code"],
            "subtitle": sub,
            "tone": tone,
            "ts": r.get("last_detection_at"),
            "meta": {
                "bureau": blabel,
                "bureau_padus_code": r["bureau"],
                "unit_code": r["unit_code"],
                "unit_code_source": r["unit_code_source"],
                "unit_type": r["unit_type"],
                "state": r["unit_state"],
                "acres": round(_f(r["acres"]), 1),
                "detections": det,
                "hotspot_cells": _i(r["hotspot_cells"]),
                "multi_sensor_cells": _i(r["multi_sensor_cells"]),
                "max_frp_mw": round(_f(r["max_frp_mw"]), 1),
            },
        })

    # ---- metrics: bureau spread is the headline story ------------------------
    ordered = sorted(by_bureau.items(), key=lambda kv: -kv[1]["units"])
    metrics = [{
        "label": BUREAU_LABEL.get(b, b),
        "value": f"{v['units']}",
        "tone": "critical" if v["detections"] >= 5000
                else "elevated" if v["detections"] >= 500 else "info",
    } for b, v in ordered]

    built = rows[0].get("built_at")
    nps_share = (100.0 * nps_units / total_units) if total_units else 0.0

    payload = {
        "domain": "doiLands",
        "source": "edip-exposure-5bureau",
        "status": "ok",
        "generatedAt": now_iso(),
        "headline": {
            "label": "DOI UNITS UNDER FIRE",
            "value": f"{total_units}",
            "tone": "critical" if total_det >= 5000
                    else "elevated" if total_det >= 500 else "info",
        },
        "metrics": metrics,
        "items": items,
        # Stated plainly because the number this feed replaces was wrong by construction,
        # and an operator comparing the two deserves to know why they differ.
        "note": (
            f"{total_units} DOI land units across {len(by_bureau)} bureaus, "
            f"{total_det:,} active detections. NPS is {nps_units} units "
            f"({nps_share:.0f}%) and {nps_det:,} detections — the NPS-only view this "
            "replaces missed the rest. Designation overlays (ACEC/WSA/WA) excluded to "
            "avoid double-counting stacked PAD-US polygons. Computed in EDIP with "
            "ST_Contains against dissolved geometry, not bounding boxes."
        ),
        # Provenance without the internal catalog path — this string is published on a
        # public CDN. "EDIP Gold layer" is honest about where it came from; the exact
        # catalog.schema.table is internal topology a consumer does not need.
        "attribution": (
            "DOI EDIP Gold layer · PAD-US 4.1 Federal Management Agencies "
            "(BLM/FWS/BOR/BIA) + NPS Land Resources Division (NPS)"
        ),
        "version": "v1",
        # Consumed by the v0_019 exec-strip renderer in prometheus-sa/index.html.
        "exec": {
            "units_exposed": total_units,
            "detections": total_det,
            "bureaus_covered": len(by_bureau),
            "by_bureau": {
                BUREAU_LABEL.get(b, b): {
                    "units": v["units"],
                    "detections": v["detections"],
                    "acres": round(v["acres"], 1),
                } for b, v in ordered
            },
            "nps_share_pct": round(nps_share, 1),
            "designation_overlays_excluded": True,
            "source_built_at": built,
            "provenance": "pulled from EDIP Gold; not computed in this repo",
        },
    }

    write_json(OUT_PATH, payload)
    print(f"  DONE. {total_units} units / {total_det:,} detections across "
          f"{len(by_bureau)} bureaus: "
          + ", ".join(f"{BUREAU_LABEL.get(b,b)}={v['units']}" for b, v in ordered))
    if built:
        print(f"  EDIP gold.exposure built_at = {built}")


if __name__ == "__main__":
    main()
