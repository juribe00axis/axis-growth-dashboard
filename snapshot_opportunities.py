#!/usr/bin/env python3
"""
snapshot_opportunities.py — AXISKEY account
Fetches every opportunity across all pipelines and saves a point-in-time
snapshot to data/snapshots/. Run this daily to build a history you can
diff later to track pipeline movement (see CLAUDE.md snapshot strategy).

Read-only: GET requests only, no data is changed.

Run with:  python3 snapshot_opportunities.py
"""

import http.client
import json
import ssl
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path


# ─── 1. LOAD CREDENTIALS ─────────────────────────────────────────────────────
# Reads the .env file in the same folder as this script.
# Pulls only the AXISKEY variables — no other account is touched.

def load_env(path):
    """Parse a .env file and return a dict of key=value pairs."""
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            result[key.strip()] = val.strip()
    return result

env         = load_env(Path(__file__).parent / ".env")
TOKEN       = env["GHL_TOKEN_AXISKEY"]        # API access token — never printed
LOCATION_ID = env["GHL_LOCATION_ID_AXISKEY"]  # AXISKEY sub-account only


# ─── 2. API HELPER ───────────────────────────────────────────────────────────
# One reusable GET function. Uses http.client directly so the Authorization
# header is never stripped (Python's urllib removes it on HTTPS connections).

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Version":       "2021-07-28",   # Required by GHL on every request
    "Accept":        "application/json",
}

def ghl_get(path, params=None):
    """Make one GET request to the GHL API and return parsed JSON."""
    url_path = path
    if params:
        url_path += "?" + urllib.parse.urlencode(params)
    conn = http.client.HTTPSConnection(
        "services.leadconnectorhq.com",
        context=ssl.create_default_context(),
    )
    conn.request("GET", url_path, headers=HEADERS)
    resp = conn.getresponse()
    if resp.status != 200:
        raise Exception(f"HTTP {resp.status} on {url_path}: {resp.read().decode()}")
    return json.loads(resp.read())


# ─── 3. FETCH PIPELINES ──────────────────────────────────────────────────────
# We pull pipeline and stage names before fetching opportunities so we can
# embed human-readable labels directly in the snapshot (not just raw IDs).
# That way the JSON file is self-contained and readable without the API.

print("Fetching pipelines...")
pipelines = ghl_get("/opportunities/pipelines", {"locationId": LOCATION_ID}).get("pipelines", [])

# Build two lookups:
#   pipeline_map: pipeline_id → pipeline_name
#   stage_map:    stage_id    → stage_name  (across all pipelines)
pipeline_map = {}
stage_map    = {}
for p in pipelines:
    pipeline_map[p["id"]] = p["name"]
    for stage in p.get("stages", []):
        stage_map[stage["id"]] = stage["name"]

print(f"  Pipelines: {', '.join(pipeline_map.values())}")
print(f"  Stages indexed: {len(stage_map)}")
print()


# ─── 4. PAGINATE ALL OPPORTUNITIES ───────────────────────────────────────────
# We don't filter by status — we want every opportunity in every state
# (open, won, lost, abandoned) so the snapshot is a complete picture.
# GHL returns at most 100 per page; we loop until a page comes back short.

print("Fetching all opportunities...")
raw_opps, page = [], 1
while True:
    batch = ghl_get("/opportunities/search", {
        "location_id": LOCATION_ID,
        "limit":       100,
        "page":        page,
    }).get("opportunities", [])
    raw_opps.extend(batch)
    print(f"  Page {page}: {len(batch)} records")
    if len(batch) < 100:
        break
    page += 1

print(f"  Total fetched: {len(raw_opps)}")
print()


# ─── 5. SHAPE EACH RECORD ────────────────────────────────────────────────────
# We keep only the fields that matter for snapshot comparisons and reports.
# Adding human-readable pipeline/stage names alongside the IDs so the file
# is easy to read in a text editor or spreadsheet without needing the API.

snapshot_opps = []
for o in raw_opps:
    pid = o.get("pipelineId", "")
    sid = o.get("pipelineStageId", "")
    snapshot_opps.append({
        "id":                 o.get("id"),
        "name":               o.get("name"),
        "status":             o.get("status"),          # open / won / lost / abandoned
        "monetaryValue":      o.get("monetaryValue"),
        "pipelineId":         pid,
        "pipelineName":       pipeline_map.get(pid, "Unknown"),
        "stageId":            sid,
        "stageName":          stage_map.get(sid, "Unknown"),
        "contactId":          o.get("contactId"),
        "assignedTo":         o.get("assignedTo"),
        "createdAt":          o.get("createdAt"),
        "lastStageChangeAt":  o.get("lastStageChangeAt"),
        "lastStatusChangeAt": o.get("lastStatusChangeAt"),
        "updatedAt":          o.get("updatedAt"),
    })


# ─── 6. SAVE SNAPSHOT ────────────────────────────────────────────────────────
# The filename includes today's date so each day's run creates a new file.
# The metadata block at the top records when the snapshot was taken and
# how many records it contains — useful when diffing two snapshots later.

snapshots_dir = Path(__file__).parent / "data" / "snapshots"
snapshots_dir.mkdir(parents=True, exist_ok=True)

today    = datetime.now(timezone.utc)
date_str = today.strftime("%Y-%m-%d")
out_path = snapshots_dir / f"opportunities-AXISKEY-{date_str}.json"

snapshot = {
    "meta": {
        "account":       "AXISKEY",
        "location_id":   LOCATION_ID,
        "captured_at":   today.isoformat(),
        "total_records": len(snapshot_opps),
        "pipelines":     list(pipeline_map.values()),
    },
    "opportunities": snapshot_opps,
}

with open(out_path, "w") as f:
    json.dump(snapshot, f, indent=2)

print(f"Snapshot saved: {out_path}")
print(f"  Records: {len(snapshot_opps)}")
print(f"  Captured at: {today.strftime('%Y-%m-%d %H:%M UTC')}")
