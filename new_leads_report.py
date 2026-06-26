#!/usr/bin/env python3
"""
new_leads_report.py — AXISKEY account
Answers: how many NEW opportunities were created this week, by pipeline?
"New" means createdAt (when the opportunity record was first added to the CRM),
NOT when a stage changed or a deal was marked won.

Read-only: GET requests only, no data is changed.

Run with:  python3 new_leads_report.py
"""

import csv
import http.client
import json
import ssl
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
# One reusable function for every GET request.
# Uses http.client directly so the Authorization header is never stripped
# (Python's urllib removes it on HTTPS connections).

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


# ─── 3. DATE WINDOW ──────────────────────────────────────────────────────────
# "This week" = Monday 00:00 UTC through today 23:59 UTC.
# GHL stores createdAt in UTC, so we match that.

today      = datetime.now(timezone.utc)
monday     = today - timedelta(days=today.weekday())  # weekday() returns 0 for Monday
week_start = monday.replace(hour=0,  minute=0,  second=0,  microsecond=0)
week_end   = today.replace( hour=23, minute=59, second=59, microsecond=0)

print(f"Week: {week_start.date()} → {week_end.date()}")
print()


# ─── 4. FETCH PIPELINES ──────────────────────────────────────────────────────
# We pull pipeline names first so the report shows "Sales Pipeline"
# instead of a raw ID like "WgtI7n080WjimBpTnFW1".

print("Fetching pipelines...")
pipelines    = ghl_get("/opportunities/pipelines", {"locationId": LOCATION_ID}).get("pipelines", [])
pipeline_map = {p["id"]: p["name"] for p in pipelines}  # id → human name

print(f"  Found: {', '.join(pipeline_map.values())}")
print()


# ─── 5. FETCH ALL OPPORTUNITIES (ALL STATUSES) ───────────────────────────────
# We want NEW opportunities regardless of whether they're open, won, or lost.
# So we don't filter by status — we fetch everything and check the date below.
#
# GHL caps at 100 records per page, so we loop until a page has < 100 results.
# Note: the opportunities/search endpoint does NOT support startDate/endDate
# params (those are contacts-only). We filter by createdAt client-side instead.

print("Fetching all opportunities...")
all_opps, page = [], 1
while True:
    batch = ghl_get("/opportunities/search", {
        "location_id": LOCATION_ID,
        "limit":       100,
        "page":        page,
    }).get("opportunities", [])
    all_opps.extend(batch)
    print(f"  Page {page}: {len(batch)} records")
    if len(batch) < 100:
        break
    page += 1

print(f"  Total returned by server: {len(all_opps)}")
print()


# ─── 6. FILTER BY createdAt ──────────────────────────────────────────────────
# createdAt is the exact timestamp GHL sets when the opportunity was first
# added. We confirm every record falls inside Monday → today to be safe.

this_week = []
for opp in all_opps:
    raw = opp.get("createdAt")
    if not raw:
        continue
    created_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if week_start <= created_at <= week_end:
        this_week.append(opp)

print(f"New opportunities this week (verified): {len(this_week)}")
print()


# ─── 7. GROUP BY PIPELINE ────────────────────────────────────────────────────
# For each new opportunity, look up its pipeline name and increment the count.

counts = defaultdict(int)
for opp in this_week:
    pid   = opp.get("pipelineId", "unknown")
    pname = pipeline_map.get(pid, f"Unknown ({pid})")
    counts[pname] += 1


# ─── 8. PRINT THE TABLE ──────────────────────────────────────────────────────
# Every pipeline is shown, even those with zero new opportunities.

W1, W2 = 24, 14
SEP    = "-" * (W1 + W2 + 2)

print(f"{'Pipeline':<{W1}} {'New Opps':>{W2}}")
print(SEP)

grand_total = 0
rows = []  # also collect rows for the CSV export below
for pname in sorted(pipeline_map.values()):
    c = counts.get(pname, 0)
    print(f"{pname:<{W1}} {c:>{W2}}")
    grand_total += c
    rows.append({"pipeline": pname, "new_opportunities": c})

print(SEP)
print(f"{'TOTAL':<{W1}} {grand_total:>{W2}}")
print()


# ─── 9. SAVE CSV TO data/exports/ ────────────────────────────────────────────
# We write a simple two-column CSV so the result can be opened in Excel
# or imported elsewhere. The file name includes today's date so runs
# from different weeks don't overwrite each other.

exports_dir = Path(__file__).parent / "data" / "exports"
exports_dir.mkdir(parents=True, exist_ok=True)

filename    = f"axiskey_new_leads_{today.strftime('%Y-%m-%d')}.csv"
output_path = exports_dir / filename

with open(output_path, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=["pipeline", "new_opportunities"])
    writer.writeheader()
    writer.writerows(rows)
    writer.writerow({"pipeline": "TOTAL", "new_opportunities": grand_total})

print(f"Saved to {output_path}")
