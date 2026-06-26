#!/usr/bin/env python3
"""
build_dashboard.py — AXISKEY account
Generates axis-growth.html — a self-contained static sales dashboard.
Re-run at any time to pull fresh data and overwrite the file.

Read-only: GET requests only, no data is changed.

Run with:  python3 build_dashboard.py
"""

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
META_TOKEN  = env["META_TOKEN_AXISKEY"]       # Meta Ads token — never printed
META_ACCT   = "act_2367308470283644"          # AxisKey Meta ad account


# ─── 2. API HELPER ───────────────────────────────────────────────────────────
# One reusable GET function. Uses http.client so the Authorization header
# is never stripped (Python's urllib removes it on HTTPS connections).

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Version":       "2021-07-28",
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


def meta_get(path, params):
    """Make one GET request to the Meta Graph API and return parsed JSON."""
    params["access_token"] = META_TOKEN
    url_path = path + "?" + urllib.parse.urlencode(params)
    conn = http.client.HTTPSConnection("graph.facebook.com", context=ssl.create_default_context())
    conn.request("GET", url_path)
    resp = conn.getresponse()
    return json.loads(resp.read())


# ─── 3. DATE HELPERS ─────────────────────────────────────────────────────────
# All timestamps in GHL are UTC, so we work in UTC throughout.

today      = datetime.now(timezone.utc)
monday     = today - timedelta(days=today.weekday())  # weekday() == 0 on Monday
week_start = monday.replace(hour=0,  minute=0,  second=0,  microsecond=0)
week_end   = today.replace( hour=23, minute=59, second=59, microsecond=0)
day_14_ago = (today - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)


# ─── 4. FETCH PIPELINES ──────────────────────────────────────────────────────
# Pull pipeline and stage metadata first so every opportunity record can be
# labeled with human-readable names rather than raw IDs.

print("Fetching pipelines...")
pipelines = ghl_get("/opportunities/pipelines", {"locationId": LOCATION_ID}).get("pipelines", [])

pipeline_map = {}  # pipeline_id → pipeline_name
stage_map    = {}  # stage_id    → {name, position, pipeline_id}
for p in pipelines:
    pipeline_map[p["id"]] = p["name"]
    for s in p.get("stages", []):
        stage_map[s["id"]] = {
            "name":        s["name"],
            "position":    s["position"],
            "pipeline_id": p["id"],
        }

print(f"  {', '.join(pipeline_map.values())} — {len(stage_map)} stages total")
print()


# ─── 4b. FETCH TEAM MEMBERS ──────────────────────────────────────────────────
# Resolve assignedTo IDs to display names for the Stage Movement table.

print("Fetching team members...")
_users_resp = ghl_get("/users/", {"locationId": LOCATION_ID})
user_map = {}
for _u in _users_resp.get("users", []):
    _name = (_u.get("name") or f"{_u.get('firstName','')} {_u.get('lastName','')}").strip()
    user_map[_u["id"]] = _name or _u["id"]
print(f"  {len(user_map)} users loaded")
print()


# ─── 5. FETCH ALL OPPORTUNITIES ──────────────────────────────────────────────
# One paginated pull (no status filter) covers every metric in this dashboard.
# We compute all four metrics from this single result set.

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

print(f"  Total: {len(all_opps)}")
print()


# ─── 6. COMPUTE METRICS ──────────────────────────────────────────────────────

print("Computing metrics...")

# ── 6a. Daily new leads — last 14 days ───────────────────────────────────────
# Build 14 date buckets, count how many opportunities have createdAt in each.

date_range        = [day_14_ago + timedelta(days=i) for i in range(14)]
date_keys         = [d.strftime("%Y-%m-%d") for d in date_range]
date_labels_short = [f"{d.strftime('%b')} {d.day}" for d in date_range]

daily_counts = defaultdict(int)
for opp in all_opps:
    raw = opp.get("createdAt")
    if not raw:
        continue
    d_key = datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    if d_key in date_keys:
        daily_counts[d_key] += 1

daily_data = [daily_counts.get(d, 0) for d in date_keys]
total_14d  = sum(daily_data)

# ── 6b. Funnel — open opportunities per stage (Sales Pipeline) ───────────────
# Stage position controls top-to-bottom order on the chart.
# Stale Pipeline is tracked separately as a simple count shown below the chart.

SALES_ID = next(pid for pid, name in pipeline_map.items() if name == "Sales Pipeline")
STALE_ID = next(pid for pid, name in pipeline_map.items() if name == "Stale Pipeline")

stage_counts = defaultdict(int)
stale_count  = 0
for opp in all_opps:
    if opp.get("status") != "open":
        continue
    pid = opp.get("pipelineId")
    if pid == SALES_ID:
        stage_counts[opp.get("pipelineStageId", "")] += 1
    elif pid == STALE_ID:
        stale_count += 1

# Stages sorted by position; filter to active (non-zero) only for the table
sales_stages  = sorted(
    [(info["name"], info["position"], stage_counts.get(sid, 0))
     for sid, info in stage_map.items() if info["pipeline_id"] == SALES_ID],
    key=lambda x: x[1],
)
active_stages = [(name, pos, cnt) for name, pos, cnt in sales_stages if cnt > 0]
funnel_labels = [row[0] for row in active_stages]
funnel_data   = [row[2] for row in active_stages]

# All-time won opportunities in the Sales Pipeline
won_total = sum(
    1 for opp in all_opps
    if opp.get("status") == "won" and opp.get("pipelineId") == SALES_ID
)

# All opps ever in Sales Pipeline (all statuses) — accurate denominator for KPIs
total_sales_opps = sum(1 for opp in all_opps if opp.get("pipelineId") == SALES_ID)

# Opps currently open at/beyond Proposal Sent stage + won = "reached proposal"
_proposal_pos = next(
    (info["position"] for sid, info in stage_map.items()
     if info["pipeline_id"] == SALES_ID and info["name"] == "Proposal Sent"),
    999,
)
_proposal_ids = {
    sid for sid, info in stage_map.items()
    if info["pipeline_id"] == SALES_ID and info["position"] >= _proposal_pos
}
proposal_reached = (
    sum(1 for opp in all_opps
        if opp.get("pipelineId") == SALES_ID
        and opp.get("status") == "open"
        and opp.get("pipelineStageId") in _proposal_ids)
    + won_total
)

won_rate_pct      = round(won_total / total_sales_opps * 100) if total_sales_opps else 0
proposal_rate_pct = round(proposal_reached / total_sales_opps * 100) if total_sales_opps else 0

# Pre-render funnel table rows (no % column — snapshot only)
_fmax = max(funnel_data) if funnel_data else 1

_funnel_rows = ""
for _name, _pos, _cnt in active_stages:
    _bw = round(_cnt / _fmax * 100)
    _funnel_rows += (
        f'<div class="funnel-row">'
        f'<span class="funnel-stage">{_name}</span>'
        f'<span class="funnel-bar-wrap"><span class="funnel-bar" style="width:{_bw}%"></span></span>'
        f'<span class="funnel-count">{_cnt}</span>'
        f'</div>\n        '
    )

_won_bw = round(won_total / _fmax * 100) if _fmax else 0
_funnel_won = (
    f'<div class="funnel-won-sep"></div>'
    f'<div class="funnel-row won-row">'
    f'<span class="funnel-stage">Won</span>'
    f'<span class="funnel-bar-wrap"><span class="funnel-bar" style="width:{_won_bw}%"></span></span>'
    f'<span class="funnel-count">{won_total}</span>'
    f'</div>'
)

# ── 6c. Summary tiles ────────────────────────────────────────────────────────

new_this_week = 0
for opp in all_opps:
    raw = opp.get("createdAt")
    if not raw:
        continue
    if week_start <= datetime.fromisoformat(raw.replace("Z", "+00:00")) <= week_end:
        new_this_week += 1

won_count = 0
won_value = 0.0
for opp in all_opps:
    if opp.get("status") != "won":
        continue
    raw = opp.get("lastStatusChangeAt")
    if not raw:
        continue
    if week_start <= datetime.fromisoformat(raw.replace("Z", "+00:00")) <= week_end:
        won_count += 1
        won_value += float(opp.get("monetaryValue") or 0)

# Format won value compactly: $10k, $39.3k, $500
kv = won_value / 1000
if won_value >= 1000:
    won_value_str = f"${kv:.0f}k" if kv == int(kv) else f"${kv:.1f}k"
else:
    won_value_str = f"${won_value:,.0f}"

# Human-readable strings used in the HTML
week_range_str = f"{week_start.strftime('%b')} {week_start.day} – {today.strftime('%b')} {today.day}"
generated_at   = f"{today.strftime('%B')} {today.day}, {today.year} at {today.strftime('%H:%M')} UTC"

print(f"  14-day new leads: {total_14d}")
print(f"  Open in Sales Pipeline: {sum(funnel_data)} | Won all-time: {won_total} | Stale Pipeline: {stale_count}")
print(f"  New this week: {new_this_week} | Won: {won_count} / {won_value_str}")
print()


# ─── 6d. MGL leads — 14-day count + 8-week weekly breakdown ──────────────────
# MGL = opportunity source field == "MGL" (set on the opportunity record, not the contact)
print("Fetching MGL data...")

SCORE_FIELD  = "5PgTaqgm1MH0Z26KKVcl"
SCORE_NORM   = {"Green": "1", "Yellow": "2", "Red": "3"}
POST_HEADERS = {**HEADERS, "Content-Type": "application/json"}

MGL_SOURCES = {"MGL", "FORM", "Meta Survey - Capital Raising"}
mgl_opps = [opp for opp in all_opps if opp.get("source") in MGL_SOURCES]
mgl_ids  = {opp["contactId"] for opp in mgl_opps if opp.get("contactId")}

# Fetch quality scores from individual contact records
# GET /contacts/{id} returns {"contact": {...}} — must unwrap
mgl_scores = {}
for cid in mgl_ids:
    resp  = ghl_get(f"/contacts/{cid}")
    c     = resp.get("contact", resp)
    score = "—"
    for cf in (c.get("customFields") or []):
        if cf.get("id") == SCORE_FIELD:
            raw   = cf.get("value", "—")
            score = SCORE_NORM.get(raw, raw)
    mgl_scores[cid] = score

# Score buckets — Sales Pipeline MGL opportunities at Discovery Call or beyond
# (matches the "open opportunities" CRM view the team uses for scoring)
_discovery_pos = next(
    info["position"] for sid, info in stage_map.items()
    if info["pipeline_id"] == SALES_ID and info["name"] == "Discovery Call"
)
_discovery_stage_ids = {
    sid for sid, info in stage_map.items()
    if info["pipeline_id"] == SALES_ID and info["position"] >= _discovery_pos
}
mgl_open_sales_cids = {
    opp["contactId"] for opp in mgl_opps
    if opp.get("pipelineId") == SALES_ID
    and opp.get("pipelineStageId") in _discovery_stage_ids
    and opp.get("contactId")
}

mgl_buckets = {"1": 0, "2": 0, "3": 0}
for cid in mgl_open_sales_cids:
    score = mgl_scores.get(cid, "—")
    if score in mgl_buckets:
        mgl_buckets[score] += 1

mgl_total_scored   = mgl_buckets["1"] + mgl_buckets["2"] + mgl_buckets["3"]
mgl_dc_plus_total  = len(mgl_open_sales_cids)   # 17 — all MGL at DC+, scored or not
mgl_dc_unscored    = mgl_dc_plus_total - mgl_total_scored

# Count MGL opps in the 14-day window
mgl_14d = sum(
    1 for opp in mgl_opps
    if opp.get("createdAt")
    and datetime.fromisoformat(opp["createdAt"].replace("Z", "+00:00")).strftime("%Y-%m-%d") in date_keys
)
mgl_14d_pct = round(mgl_14d / total_14d * 100) if total_14d else 0

# Weekly MGL counts — last 8 weeks (Mon–Sun buckets)
NUM_WEEKS  = 8
week_buckets = []
for w in range(NUM_WEEKS - 1, -1, -1):
    wk_mon = (monday - timedelta(weeks=w)).replace(hour=0, minute=0, second=0, microsecond=0)
    wk_sun = wk_mon + timedelta(days=6, hours=23, minutes=59, seconds=59)
    label  = f"{wk_mon.strftime('%b')} {wk_mon.day}"
    week_buckets.append({"label": label, "start": wk_mon, "end": wk_sun, "count": 0})

for opp in mgl_opps:
    if not opp.get("createdAt"):
        continue
    opp_dt = datetime.fromisoformat(opp["createdAt"].replace("Z", "+00:00"))
    for bucket in week_buckets:
        if bucket["start"] <= opp_dt <= bucket["end"]:
            bucket["count"] += 1
            break

mgl_week_labels = [b["label"] for b in week_buckets]
mgl_week_data   = [b["count"] for b in week_buckets]

print(f"  MGL in last 14 days: {mgl_14d} of {total_14d} ({mgl_14d_pct}%)")
print(f"  Weekly MGL (last 8 wks): {mgl_week_data}")
print(f"  Score buckets: {mgl_buckets}")
print()

# Pre-render MGL weekly table rows (replaces bar chart in HTML)
_mgl_max = max(mgl_week_data) if any(mgl_week_data) else 1
_mgl_rows = ""
for _i, (_lbl, _cnt) in enumerate(zip(mgl_week_labels, mgl_week_data)):
    _cls = " current" if _i == len(mgl_week_labels) - 1 else ""
    _pct = round(_cnt / _mgl_max * 100)
    _mgl_rows += (
        f'<div class="mgl-tr{_cls}">'
        f'<span class="mgl-tw">{_lbl}</span>'
        f'<span class="mgl-tbar-wrap"><span class="mgl-tbar" style="width:{_pct}%"></span></span>'
        f'<span class="mgl-tc">{_cnt}</span>'
        f'</div>\n        '
    )


# ─── 6e. Stage Movement — weekly activity table ───────────────────────────────
# For each opp in the Sales Pipeline whose current stage is one of the 4 key
# stages, use lastStageChangeAt to determine which day it entered that stage.
# Counts are grouped by (owner, stage, day) starting from week_start (Mon Jun 22).

print("Computing stage movement...")

MOVE_STAGES = ["Discovery Call", "Strategy Call", "Proposal Sent", "Agreement Signed"]

# Build day columns from week_start through today
_move_days = []
_d = week_start
while _d.date() <= today.date():
    _move_days.append(_d)
    _d += timedelta(days=1)
move_day_keys   = [_d.strftime("%Y-%m-%d")              for _d in _move_days]
move_day_labels = [_d.strftime("%a %-m/%-d")             for _d in _move_days]

# stage_move[owner_id][stage][date_key] = count
stage_move = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

for opp in all_opps:
    if opp.get("pipelineId") != SALES_ID:
        continue
    stage_info = stage_map.get(opp.get("pipelineStageId", ""), {})
    stage = stage_info.get("name", "")
    if stage not in MOVE_STAGES:
        continue
    raw_ts = opp.get("lastStageChangeAt")
    if not raw_ts:
        continue
    dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
    if dt < week_start:
        continue
    d_key    = dt.strftime("%Y-%m-%d")
    owner_id = opp.get("assignedTo") or "unassigned"
    stage_move[owner_id][stage][d_key] += 1

# Sort owners by display name
move_owners = sorted(stage_move.keys(), key=lambda oid: user_map.get(oid, oid).lower())

print(f"  {len(move_owners)} owners · {len(move_day_keys)} days · {len(MOVE_STAGES)} stages")
print()


# ─── 6g. Meta Campaign Spending — last 7 days ────────────────────────────────
print("Fetching Meta spend data...")
meta_end   = today.date()
meta_start = meta_end - timedelta(days=6)

meta_resp  = meta_get(f"/v21.0/{META_ACCT}/insights", {
    "fields":         "date_start,spend",
    "time_range":     json.dumps({"since": str(meta_start), "until": str(meta_end)}),
    "time_increment": "1",
})

if "error" in meta_resp:
    print(f"  Meta API error: {meta_resp['error'].get('message')} — skipping section")
    meta_rows = []
else:
    meta_rows = sorted(meta_resp.get("data", []), key=lambda r: r["date_start"])

meta_labels  = [datetime.strptime(r["date_start"], "%Y-%m-%d").strftime("%-m/%-d") for r in meta_rows]
meta_spends  = [float(r.get("spend", 0)) for r in meta_rows]
meta_total   = sum(meta_spends)
meta_avg     = meta_total / len(meta_spends) if meta_spends else 0
meta_today_v = meta_spends[-1] if meta_spends else 0

meta_start_fmt = datetime.strptime(meta_rows[0]["date_start"], "%Y-%m-%d").strftime("%b %-d") if meta_rows else ""
meta_end_fmt   = datetime.strptime(meta_rows[-1]["date_start"], "%Y-%m-%d").strftime("%b %-d") if meta_rows else ""
meta_range_str = f"{meta_start_fmt} – {meta_end_fmt} · AxisKey"
meta_total_str = f"${meta_total:,.0f}"
meta_avg_str   = f"${meta_avg:,.0f}"
meta_today_str = f"${meta_today_v:,.0f}"

print(f"  7-day total: {meta_total_str} | avg/day: {meta_avg_str} | today: {meta_today_str}")
print()


# ─── 7. BUILD HTML ───────────────────────────────────────────────────────────
# The HTML is assembled in named sections.
#
# Sections with lots of CSS/JS curly braces use regular Python strings —
# no escaping needed. Sections that embed computed values use f-strings.
# They are joined at the end to produce the final file.

# ── 7a. Head + CSS (regular string — CSS has many { } chars) ─────────────────
HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Axis Growth</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Saira:wght@400;600;700;800&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    /* Design tokens from style/style.md */
    :root {
      --bg:        #101014;
      --surface:   #1C1C24;
      --surface-2: #262630;
      --hero:      #C8FF01;
      --text:      #F5F5F7;
      --text-mute: #9A9AA5;
      --won:       #C8FF01;
      --lost:      #FF5C5C;
      --line:      #2E2E38;
    }

    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: "Saira", "Eurostile", system-ui, sans-serif;
      min-height: 100vh;
      padding: 32px 40px 64px;
    }

    /* Header */
    .header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 40px;
    }
    .header-left  { display: flex; align-items: center; gap: 58px; }
    .header-logo  { height: 44px; width: auto; display: block; }
    .header-title {
      font-size: 1.55rem;
      font-weight: 800;
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }
    .header-meta {
      font-size: 0.7rem;
      color: var(--text-mute);
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    /* Cards */
    .card {
      background: var(--surface);
      border-radius: 18px;
      padding: 28px 32px;
      box-shadow: 0 4px 28px rgba(0,0,0,0.5);
    }
    .card-label {
      font-size: 0.67rem;
      font-weight: 600;
      letter-spacing: 0.15em;
      text-transform: uppercase;
      color: var(--text-mute);
      margin-bottom: 14px;
    }

    /* Hero section */
    .section-hero  { margin-bottom: 22px; }
    .hero-number {
      font-size: 4.2rem;
      font-weight: 800;
      color: var(--hero);
      line-height: 1;
      margin-bottom: 4px;
    }
    .hero-sub {
      font-size: 0.72rem;
      color: var(--text-mute);
      letter-spacing: 0.1em;
      text-transform: uppercase;
      margin-bottom: 22px;
    }
    .chart-wrap { position: relative; height: 200px; }

    /* Funnel KPI chips */
    .funnel-kpis { display: flex; gap: 16px; margin-bottom: 20px; }
    .funnel-kpi {
      background: var(--surface-2);
      border-radius: 12px;
      padding: 14px 20px;
      flex: 1;
    }
    .funnel-kpi-label {
      font-size: 0.6rem;
      font-weight: 600;
      letter-spacing: 0.15em;
      text-transform: uppercase;
      color: var(--text-mute);
      display: block;
      margin-bottom: 6px;
    }
    .funnel-kpi-value {
      font-size: 2rem;
      font-weight: 800;
      color: var(--hero);
      line-height: 1;
    }
    .funnel-kpi-sub {
      font-size: 0.68rem;
      color: var(--text-mute);
      display: block;
      margin-top: 5px;
    }

    /* Funnel table */
    .funnel-table { margin-top: 4px; }
    .funnel-row {
      display: grid;
      grid-template-columns: 148px 1fr 38px;
      align-items: center;
      gap: 14px;
      padding: 9px 0;
      border-bottom: 1px solid var(--line);
    }
    .funnel-row:last-child { border-bottom: none; }
    .funnel-stage { font-size: 0.85rem; font-weight: 600; color: var(--text); }
    .funnel-bar-wrap { display: block; height: 6px; background: var(--surface-2); border-radius: 3px; overflow: hidden; }
    .funnel-bar { display: block; height: 100%; background: rgba(200,255,1,0.45); border-radius: 3px; }
    .funnel-count { font-size: 0.95rem; font-weight: 800; color: var(--text); text-align: right; }
    .funnel-won-sep { height: 1px; background: var(--line); margin: 4px 0; }
    .funnel-row.won-row .funnel-stage { color: var(--hero); }
    .funnel-row.won-row .funnel-bar   { background: var(--hero); }
    .funnel-row.won-row .funnel-count { color: var(--hero); }
    .pipeline-note {
      font-size: 0.66rem;
      color: var(--text-mute);
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-top: 14px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
    }

    /* Summary tiles */
    .tiles { display: flex; flex-direction: column; gap: 16px; }
    .tile {
      background: var(--surface);
      border-radius: 18px;
      padding: 24px 26px;
      box-shadow: 0 4px 28px rgba(0,0,0,0.5);
      flex: 1;
    }
    .tile-label {
      font-size: 0.65rem;
      font-weight: 600;
      letter-spacing: 0.15em;
      text-transform: uppercase;
      color: var(--text-mute);
      margin-bottom: 10px;
    }
    .tile-value {
      font-size: 3.2rem;
      font-weight: 800;
      color: var(--text);
      line-height: 1;
    }
    .tile-value.accent { color: var(--hero); }
    .tile-sub {
      font-size: 0.72rem;
      color: var(--text-mute);
      margin-top: 8px;
    }

    /* MGL stat line inside hero */
    .mgl-stat {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 0.72rem;
      color: var(--text-mute);
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 22px;
    }
    .mgl-pill {
      background: rgba(200,255,1,0.12);
      border: 1px solid rgba(200,255,1,0.35);
      color: var(--hero);
      font-size: 0.7rem;
      font-weight: 700;
      border-radius: 20px;
      padding: 2px 10px;
      letter-spacing: 0.04em;
    }

    /* MGL score table */
    .mgl-row {
      display: grid;
      grid-template-columns: 1fr 200px;
      gap: 20px;
      align-items: start;
    }
    .score-table { display: flex; flex-direction: column; }
    .score-row {
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: center;
      padding: 9px 0;
      border-bottom: 1px solid var(--line);
      font-size: 0.88rem;
    }
    .score-row:last-child { border-bottom: none; }
    .score-row.hdr {
      font-size: 0.6rem;
      font-weight: 600;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--text-mute);
      padding-bottom: 6px;
    }
    .score-row.hdr .score-n { font-size: 0.6rem; font-weight: 600; }
    .score-n { font-weight: 700; font-size: 1.05rem; text-align: right; min-width: 28px; }
    .score-n.accent { color: var(--hero); }
    .score-n.dim    { color: var(--text-mute); }
    .score-row.total-row { border-top: 1px solid var(--line); margin-top: 2px; }
    .score-row.total-row .score-lbl { font-size: 0.72rem; color: var(--text-mute); }
    .score-row.total-row .score-n   { font-size: 0.9rem; color: var(--text-mute); }

    /* Hero weekly stat blocks */
    .hero-top {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 36px;
      align-items: start;
      margin-bottom: 18px;
    }
    .hero-stat-block {
      border-left: 1px solid var(--line);
      padding-left: 24px;
      min-width: 110px;
    }
    .hero-stat-lbl {
      font-size: 0.62rem;
      font-weight: 600;
      letter-spacing: 0.15em;
      text-transform: uppercase;
      color: var(--text-mute);
      margin-bottom: 8px;
    }
    .hero-stat-num {
      font-size: 3rem;
      font-weight: 800;
      color: var(--text);
      line-height: 1;
    }
    .hero-stat-num.accent { color: var(--hero); }
    .hero-stat-sub-s {
      font-size: 0.7rem;
      color: var(--text-mute);
      margin-top: 6px;
    }

    /* Meta Campaign Spending section */
    .meta-section { margin-top: 22px; }
    .meta-top {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 24px;
      align-items: start;
      margin-bottom: 22px;
    }
    .meta-tiles { display: flex; gap: 16px; }
    .meta-tiles .tile { min-width: 140px; }

    /* Granola Insights section */
    .granola-section { margin-top: 22px; }
    .granola-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-top: 16px;
    }
    .g-card {
      background: var(--surface-2);
      border-radius: 12px;
      padding: 20px 22px;
    }
    .g-card-label {
      font-size: 0.62rem;
      font-weight: 600;
      letter-spacing: 0.15em;
      text-transform: uppercase;
      color: var(--text-mute);
      margin-bottom: 14px;
    }
    .fund-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 6px 0;
      border-bottom: 1px solid var(--line);
      font-size: 0.95rem;
      font-weight: 600;
    }
    .fund-row:last-child { border-bottom: none; }
    .fund-badge {
      font-size: 0.7rem;
      font-weight: 700;
      background: rgba(200,255,1,0.12);
      color: var(--hero);
      border: 1px solid rgba(200,255,1,0.3);
      border-radius: 20px;
      padding: 2px 10px;
      letter-spacing: 0.05em;
    }
    .comp-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 6px 0;
      border-bottom: 1px solid var(--line);
      font-size: 0.9rem;
    }
    .comp-row:last-child { border-bottom: none; }
    .comp-count {
      font-size: 0.72rem;
      color: var(--text-mute);
    }
    .comp-none {
      font-size: 0.82rem;
      color: var(--text-mute);
      font-style: italic;
      padding: 8px 0;
    }
    .quote-text {
      font-size: 1.05rem;
      font-weight: 600;
      line-height: 1.55;
      color: var(--text);
      border-left: 3px solid var(--hero);
      padding-left: 14px;
      margin-bottom: 10px;
    }
    .quote-source {
      font-size: 0.68rem;
      color: var(--text-mute);
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .question-row {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      padding: 7px 0;
      border-bottom: 1px solid var(--line);
      font-size: 0.85rem;
      line-height: 1.4;
    }
    .question-row:last-child { border-bottom: none; }
    .q-count {
      min-width: 28px;
      font-size: 0.7rem;
      font-weight: 700;
      color: var(--hero);
      padding-top: 2px;
    }

    /* MGL weekly table */
    .mgl-table { display: flex; flex-direction: column; gap: 7px; }
    .mgl-tr { display: grid; grid-template-columns: 54px 1fr 26px; align-items: center; gap: 10px; }
    .mgl-tw { font-size: 0.68rem; color: var(--text-mute); letter-spacing: 0.04em; white-space: nowrap; }
    .mgl-tbar-wrap { display: block; height: 7px; background: var(--surface-2); border-radius: 4px; overflow: hidden; }
    .mgl-tbar { display: block; height: 100%; background: #C8FF01; border-radius: 4px; }
    .mgl-tc { font-size: 0.82rem; font-weight: 700; color: var(--text); text-align: right; }
    .mgl-tr.current .mgl-tbar { background: rgba(200,255,1,0.38); }
    .mgl-tr.current .mgl-tc  { color: var(--text-mute); }
    .mgl-tr.current .mgl-tw  { color: rgba(154,154,165,0.6); }

    /* Stage movement table */
    .smv-wrap { overflow-x: auto; }
    .smv-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
    }
    .smv-table th {
      font-size: 0.62rem;
      font-weight: 600;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--text-mute);
      padding: 0 10px 12px;
      text-align: center;
      white-space: nowrap;
      border-bottom: 1px solid var(--line);
    }
    .smv-table th.smv-th-owner { text-align: left; min-width: 130px; padding-left: 0; }
    .smv-table th.smv-th-stage { text-align: left; min-width: 142px; }
    .smv-table td {
      padding: 7px 10px;
      text-align: center;
      border-bottom: 1px solid var(--line);
      color: var(--text);
      font-weight: 600;
    }
    .smv-table td.smv-owner-cell {
      font-size: 0.82rem;
      font-weight: 700;
      color: var(--text);
      text-align: left;
      padding-left: 0;
      vertical-align: middle;
      border-right: 1px solid var(--line);
      padding-right: 14px;
    }
    .smv-table td.smv-stage-cell {
      text-align: left;
      font-weight: 400;
      font-size: 0.78rem;
      color: var(--text-mute);
      padding-left: 12px;
      white-space: nowrap;
    }
    .smv-table td.smv-val { font-size: 0.88rem; font-weight: 700; }
    .smv-table td.smv-val-pos { color: var(--hero); }
    .smv-table td.smv-val-zero { color: var(--surface-2); }
    .smv-table tr.smv-owner-last-row td { border-bottom: 2px solid var(--line); }
    .smv-table tr.smv-owner-last-row:last-child td { border-bottom: none; }
    .smv-note {
      font-size: 0.66rem;
      color: var(--text-mute);
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-top: 14px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
    }

    /* Coming soon placeholder */
    .section-coming {
      border: 2px dashed var(--line);
      border-radius: 18px;
      padding: 42px 32px;
      text-align: center;
    }
    .coming-label {
      font-size: 0.66rem;
      font-weight: 600;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--text-mute);
      margin-bottom: 10px;
    }
    .coming-title {
      font-size: 1.3rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--surface-2);
    }
  </style>
</head>
<body>
"""

# ── 7b. Header (f-string — embeds generated_at) ──────────────────────────────
HEADER = f"""
  <header class="header">
    <div class="header-left">
      <img src="assets/logo.svg" alt="Axis Growth" class="header-logo">
      <span class="header-title">Axis Growth</span>
    </div>
    <span class="header-meta">Generated {generated_at}</span>
  </header>
"""

# ── 7c. Hero metric (f-string — embeds total_14d and date range) ──────────────
HERO = f"""
  <section class="section-hero card">
    <div class="card-label">New Leads — Last 14 Days</div>
    <div class="hero-top">
      <div>
        <div class="hero-number">{total_14d}</div>
        <div class="hero-sub">opportunities created &nbsp;·&nbsp; {date_labels_short[0]} – {date_labels_short[-1]}</div>
      </div>
      <div class="hero-stat-block">
        <div class="hero-stat-lbl">This Week</div>
        <div class="hero-stat-num accent">{new_this_week}</div>
        <div class="hero-stat-sub-s">{week_range_str}</div>
      </div>
      <div class="hero-stat-block">
        <div class="hero-stat-lbl">Won This Week</div>
        <div class="hero-stat-num">{won_count}</div>
        <div class="hero-stat-sub-s">{won_value_str} closed</div>
      </div>
    </div>
    <div class="mgl-stat">
      <span class="mgl-pill">MGL {mgl_14d} &nbsp;/&nbsp; {mgl_14d_pct}%</span>
      of new leads are marketing generated
    </div>
    <div class="chart-wrap">
      <canvas id="chartLeads"></canvas>
    </div>
  </section>
"""

MGL_CHART = f"""
  <div class="card" style="margin-bottom:22px;">
    <div class="card-label">Marketing Generated Leads (MGL · FORM · Meta Survey)</div>
    <div class="mgl-row">
      <div>
        <div style="font-size:0.62rem;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:var(--text-mute);margin-bottom:14px;">Weekly Volume — Last 8 Weeks</div>
        <div class="mgl-table">
        {_mgl_rows}
        </div>
      </div>
      <div>
        <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:14px;">
          <span style="font-size:2.6rem;font-weight:800;color:var(--hero);line-height:1;">{mgl_dc_plus_total}</span>
          <span style="font-size:0.62rem;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:var(--text-mute);">Total Marketing<br>Generated Leads<br>Discovery Call+</span>
        </div>
        <div style="font-size:0.62rem;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:var(--text-mute);margin-bottom:8px;">Call Quality</div>
        <div class="score-table">
          <div class="score-row hdr">
            <span class="score-lbl">Bucket</span>
            <span class="score-n">#</span>
          </div>
          <div class="score-row">
            <span class="score-lbl">🟢 Great Fit</span>
            <span class="score-n accent">{mgl_buckets["1"]}</span>
          </div>
          <div class="score-row">
            <span class="score-lbl">🟡 Potential</span>
            <span class="score-n">{mgl_buckets["2"]}</span>
          </div>
          <div class="score-row">
            <span class="score-lbl">🔴 Poor Fit</span>
            <span class="score-n">{mgl_buckets["3"]}</span>
          </div>
          <div class="score-row">
            <span class="score-lbl">⬜ Unscored</span>
            <span class="score-n dim">{mgl_dc_unscored}</span>
          </div>
          <div class="score-row total-row">
            <span class="score-lbl">Scored</span>
            <span class="score-n">{mgl_total_scored} / {mgl_dc_plus_total}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
"""

# ── 7d. Middle row (f-string — embeds tile values and stale count) ────────────
MIDDLE = f"""
  <div style="margin-bottom:22px;">
    <div class="card">
      <div class="card-label">Pipeline Snapshot — Open Deals by Stage</div>
      <div class="funnel-kpis">
        <div class="funnel-kpi">
          <span class="funnel-kpi-label">Lead to Won</span>
          <span class="funnel-kpi-value">{won_rate_pct}%</span>
          <span class="funnel-kpi-sub">{won_total} won of {total_sales_opps} total leads</span>
        </div>
        <div class="funnel-kpi">
          <span class="funnel-kpi-label">Lead to Proposal</span>
          <span class="funnel-kpi-value">{proposal_rate_pct}%</span>
          <span class="funnel-kpi-sub">{proposal_reached} reached proposal stage</span>
        </div>
      </div>
      <div class="funnel-table">
        {_funnel_rows}
        {_funnel_won}
      </div>
      <div class="pipeline-note">Stale Pipeline &nbsp;—&nbsp; {stale_count} open</div>
    </div>
  </div>
"""

# ── 7e. Meta Campaign Spending section (f-string — embeds live values) ───────
META_SECTION = f"""
  <section class="card meta-section">
    <div class="card-label">Meta Campaign Spending — Last 7 Days</div>
    <div class="meta-top">
      <div>
        <div class="hero-number">{meta_total_str}</div>
        <div class="hero-sub">{meta_range_str}</div>
      </div>
      <div class="meta-tiles">
        <div class="tile">
          <div class="tile-label">Avg Daily Spend</div>
          <div class="tile-value accent">{meta_avg_str}</div>
        </div>
        <div class="tile">
          <div class="tile-label">Today's Spend</div>
          <div class="tile-value">{meta_today_str}</div>
          <div class="tile-sub">partial day</div>
        </div>
      </div>
    </div>
    <div class="chart-wrap">
      <canvas id="chartMetaSpend"></canvas>
    </div>
  </section>
"""

# ── 7h. Granola Insights — loaded from data/granola_intelligence.json ─────────
# Fund sizes, competitors, and questions accumulate over time (never reset).
# Quote of the week is replaced each week.
# To add a new week: ask Claude to analyze latest Granola meetings —
# it will update the JSON, then re-run build_dashboard.py.

_gi = json.loads((Path(__file__).parent / "data/granola_intelligence.json").read_text())

def _fund_rows(data):
    rows = sorted(data.items(), key=lambda x: x[1], reverse=True)
    return "\n        ".join(
        f'<div class="fund-row"><span>{k}</span><span class="fund-badge">×{v}</span></div>'
        for k, v in rows
    ) if rows else '<div class="comp-none">None recorded yet.</div>'

def _comp_rows(data):
    rows = sorted(data.items(), key=lambda x: x[1], reverse=True)
    return "\n        ".join(
        f'<div class="comp-row"><span>{k}</span><span class="comp-count">×{v}</span></div>'
        for k, v in rows
    ) if rows else '<div class="comp-none">No competitors named by prospects yet.</div>'

def _question_rows(data):
    rows = sorted(data.items(), key=lambda x: x[1], reverse=True)
    return "\n        ".join(
        f'<div class="question-row"><span class="q-count">×{v}</span><span>{k}</span></div>'
        for k, v in rows
    ) if rows else '<div class="comp-none">None recorded yet.</div>'

GRANOLA_SECTION = f"""
  <section class="card granola-section">
    <div class="card-label">Call Intelligence — All Time &nbsp;·&nbsp; Updated {_gi["last_updated"]}</div>
    <div class="granola-grid">

      <div class="g-card">
        <div class="g-card-label">Fund Sizes Mentioned</div>
        {_fund_rows(_gi["fund_sizes"])}
      </div>

      <div class="g-card">
        <div class="g-card-label">Top Competitors Mentioned</div>
        {_comp_rows(_gi["competitors"])}
      </div>

      <div class="g-card">
        <div class="g-card-label">Quote of the Week</div>
        <div class="quote-text">"{_gi["quote_of_week"]["text"]}"</div>
        <div class="quote-source">{_gi["quote_of_week"]["source"]}</div>
      </div>

      <div class="g-card">
        <div class="g-card-label">Questions Prospects Ask Most</div>
        {_question_rows(_gi["questions"])}
      </div>

    </div>
  </section>
"""

# ── 7i. Stage Movement table (f-string — embeds computed rows) ───────────────
def _build_stage_movement():
    if not move_owners:
        return '<div class="smv-note" style="text-align:center;padding:32px 0;">No stage movement data found for this period.</div>'

    # Header row
    day_ths = "".join(f'<th>{lbl}</th>' for lbl in move_day_labels)
    thead = f'<thead><tr><th class="smv-th-owner">Owner</th><th class="smv-th-stage">Stage</th>{day_ths}</tr></thead>'

    tbody_rows = ""
    for owner_id in move_owners:
        name = user_map.get(owner_id, owner_id)
        for s_idx, stage in enumerate(MOVE_STAGES):
            is_last = s_idx == len(MOVE_STAGES) - 1
            row_cls = ' class="smv-owner-last-row"' if is_last else ""

            # Owner cell only on first stage row (rowspan=4)
            if s_idx == 0:
                owner_td = f'<td class="smv-owner-cell" rowspan="{len(MOVE_STAGES)}">{name}</td>'
            else:
                owner_td = ""

            stage_td = f'<td class="smv-stage-cell">{stage}</td>'

            day_tds = ""
            for d_key in move_day_keys:
                val = stage_move[owner_id][stage].get(d_key, 0)
                if val > 0:
                    day_tds += f'<td class="smv-val smv-val-pos">{val}</td>'
                else:
                    day_tds += f'<td class="smv-val smv-val-zero">—</td>'

            tbody_rows += f"<tr{row_cls}>{owner_td}{stage_td}{day_tds}</tr>\n"

    tbody = f"<tbody>{tbody_rows}</tbody>"
    week_label = _move_days[0].strftime("%-m/%-d") if _move_days else ""
    today_label = _move_days[-1].strftime("%-m/%-d") if _move_days else ""
    note = f'<div class="smv-note">Counts reflect opportunities currently in each stage · last stage change date used as entry date · {week_label}–{today_label}</div>'
    return f'<div class="smv-wrap"><table class="smv-table">{thead}{tbody}</table></div>{note}'

STAGE_MOVEMENT = f"""
  <section class="card" style="margin-top:22px;">
    <div class="card-label">Stage Movement — Week of {_move_days[0].strftime("%b %-d") if _move_days else ""}</div>
    {_build_stage_movement()}
  </section>
"""

# ── 7g. Data injection (f-string — embeds computed JSON arrays) ───────────────
# Chart.js reads these constants from the next <script> block.
DATA_SCRIPT = f"""
  <script>
    // All values injected by build_dashboard.py — re-run the script to refresh.
    const DAILY_LABELS  = {json.dumps(date_labels_short)};
    const DAILY_DATA    = {json.dumps(daily_data)};
    const META_LABELS   = {json.dumps(meta_labels)};
    const META_SPEND    = {json.dumps(meta_spends)};
  </script>
"""

# ── 7g. Chart initialization (regular string — lots of JS {{ }} chars) ────────
CHARTS_SCRIPT = """
  <script>
    // Shared defaults applied to every chart on the page
    Chart.defaults.color       = "#9A9AA5";
    Chart.defaults.font.family = '"Saira", "Eurostile", system-ui, sans-serif';
    Chart.defaults.font.size   = 12;

    // ── Daily new leads (vertical bar) ────────────────────────────────────
    // Lime bars on days with activity; dark on zero days so busy days pop.
    new Chart(document.getElementById("chartLeads"), {
      type: "bar",
      data: {
        labels: DAILY_LABELS,
        datasets: [{
          data:            DAILY_DATA,
          backgroundColor: DAILY_DATA.map(v => v > 0 ? "#C8FF01" : "#262630"),
          borderRadius:    6,
          borderSkipped:   false,
        }],
      },
      options: {
        responsive:          true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            callbacks: {
              label: ctx => ` ${ctx.parsed.y} new lead${ctx.parsed.y !== 1 ? "s" : ""}`,
            },
          },
        },
        scales: {
          x: { grid: { color: "#2E2E38" }, ticks: { color: "#9A9AA5" } },
          y: {
            grid:        { color: "#2E2E38" },
            ticks:       { color: "#9A9AA5", stepSize: 1, precision: 0 },
            beginAtZero: true,
          },
        },
      },
    });

    // ── Meta Campaign Spending (vertical bar) ─────────────────────────────
    new Chart(document.getElementById("chartMetaSpend"), {
      type: "bar",
      data: {
        labels: META_LABELS,
        datasets: [{
          data:            META_SPEND,
          backgroundColor: META_SPEND.map((v, i) => i === META_SPEND.length - 1 ? "rgba(200,255,1,0.45)" : "#C8FF01"),
          borderRadius:    6,
          borderSkipped:   false,
        }],
      },
      options: {
        responsive:          true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            callbacks: {
              label: ctx => ` $${ctx.parsed.y.toFixed(2)} spent`,
            },
          },
        },
        scales: {
          x: { grid: { color: "#2E2E38" }, ticks: { color: "#9A9AA5" } },
          y: {
            grid:        { color: "#2E2E38" },
            ticks:       { color: "#9A9AA5", callback: v => `$${v}` },
            beginAtZero: true,
          },
        },
      },
    });

  </script>
</body>
</html>
"""

# ─── 8. ASSEMBLE AND WRITE ───────────────────────────────────────────────────
# Join all sections into one string and write axis-growth.html.
# Running this script again will overwrite the file with fresh data.

html     = HEAD + HEADER + HERO + MGL_CHART + MIDDLE + STAGE_MOVEMENT + META_SECTION + GRANOLA_SECTION + DATA_SCRIPT + CHARTS_SCRIPT
out_path = Path(__file__).parent / "axis-growth.html"
out_path.write_text(html, encoding="utf-8")

print(f"Dashboard written → {out_path}")
print("  Open with:  open axis-growth.html")
