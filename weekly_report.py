#!/usr/bin/env python3
"""
weekly_report.py — AXISKEY account
Prints a table of deals marked WON this week, broken down by pipeline.
Read-only: makes GET requests only, no data is changed.

Run with:  python3 weekly_report.py
"""

import json
import http.client
import ssl
import urllib.parse
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path


# ─── 1. LOAD CREDENTIALS ─────────────────────────────────────────────────────
# We read the .env file in the same folder as this script.
# It holds the API token and location ID for the AXISKEY account.
# The token is never printed to the screen.

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
LOCATION_ID = env["GHL_LOCATION_ID_AXISKEY"]  # Which GHL sub-account to query


# ─── 2. API HELPER ───────────────────────────────────────────────────────────
# This function handles every GET request to the GHL API.
# It attaches the required headers automatically and returns the response
# as a Python dictionary so the rest of the script can work with normal data.

BASE_URL = "https://services.leadconnectorhq.com"
HEADERS  = {
    "Authorization": f"Bearer {TOKEN}",
    "Version":       "2021-07-28",   # GHL requires this header on every request
    "Accept":        "application/json",
}

def ghl_get(path, params=None):
    """Make one GET request to the GHL API and return parsed JSON."""
    # We use http.client directly so headers (including Authorization) are
    # never stripped — Python's urllib removes auth headers on HTTPS connections.
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
# GHL stores all timestamps in UTC, so we stay in UTC throughout.

today      = datetime.now(timezone.utc)
monday     = today - timedelta(days=today.weekday())  # weekday() returns 0 for Monday
week_start = monday.replace(hour=0,  minute=0,  second=0,  microsecond=0)
week_end   = today.replace( hour=23, minute=59, second=59, microsecond=0)

print(f"Week: {week_start.date()} → {week_end.date()}")
print()


# ─── 4. FETCH PIPELINES ──────────────────────────────────────────────────────
# We pull the pipeline list first so we can show "Sales Pipeline" in the
# output instead of a raw ID like "WgtI7n080WjimBpTnFW1".

print("Fetching pipelines...")
pipelines    = ghl_get("/opportunities/pipelines", {"locationId": LOCATION_ID}).get("pipelines", [])
pipeline_map = {p["id"]: p["name"] for p in pipelines}  # id → human name lookup

print(f"  Found: {', '.join(pipeline_map.values())}")
print()


# ─── 5. FETCH ALL WON OPPORTUNITIES ─────────────────────────────────────────
# GHL caps results at 100 records per page, so we loop through pages until
# we get a page with fewer than 100 results (that signals the last page).
# We fetch ALL won deals first; we narrow to this week in the next step.
# Note: the API's startDate/endDate params filter by creation date, not
# won-date, so we can't use them here — we filter manually below.

print("Fetching won opportunities...")
all_opps, page = [], 1
while True:
    batch = ghl_get("/opportunities/search", {
        "location_id": LOCATION_ID,
        "status":      "won",
        "limit":       100,
        "page":        page,
    }).get("opportunities", [])
    all_opps.extend(batch)
    print(f"  Page {page}: {len(batch)} records")
    if len(batch) < 100:
        break
    page += 1

print(f"  Total won (all time): {len(all_opps)}")
print()


# ─── 6. FILTER TO THIS WEEK ──────────────────────────────────────────────────
# lastStatusChangeAt is the exact timestamp GHL sets when a deal is marked
# won. We keep only the deals where that date falls inside our window.

this_week = []
for opp in all_opps:
    raw = opp.get("lastStatusChangeAt")
    if not raw:
        continue
    won_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if week_start <= won_at <= week_end:
        this_week.append(opp)

print(f"Won this week: {len(this_week)}")
print()


# ─── 7. GROUP BY PIPELINE ────────────────────────────────────────────────────
# For each deal won this week, look up its pipeline name and add its
# monetary value to that pipeline's running totals.

summary = defaultdict(lambda: {"count": 0, "value": 0.0})
for opp in this_week:
    pid   = opp.get("pipelineId", "unknown")
    pname = pipeline_map.get(pid, f"Unknown ({pid})")
    summary[pname]["count"] += 1
    summary[pname]["value"] += float(opp.get("monetaryValue") or 0)


# ─── 8. PRINT THE TABLE ──────────────────────────────────────────────────────
# Every pipeline is listed — even ones with zero wins — so nothing is hidden.
# Columns: pipeline name (left), count won (right), total value (right).

W1, W2, W3 = 24, 8, 14
SEP = "-" * (W1 + W2 + W3 + 2)

print(f"{'Pipeline':<{W1}} {'Won':>{W2}} {'Total Value':>{W3}}")
print(SEP)

grand_count, grand_value = 0, 0.0
for pname in sorted(pipeline_map.values()):
    c = summary[pname]["count"] if pname in summary else 0
    v = summary[pname]["value"] if pname in summary else 0.0
    print(f"{pname:<{W1}} {c:>{W2}} {'${:,.2f}'.format(v):>{W3}}")
    grand_count += c
    grand_value += v

print(SEP)
print(f"{'TOTAL':<{W1}} {grand_count:>{W2}} {'${:,.2f}'.format(grand_value):>{W3}}")
