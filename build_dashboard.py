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
import os
import ssl
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ─── 1. LOAD CREDENTIALS ─────────────────────────────────────────────────────
# Reads from .env file when running locally; falls back to environment
# variables when running in CI (GitHub Actions passes secrets as env vars).

def load_env(path):
    result = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                result[key.strip()] = val.strip()
    except FileNotFoundError:
        pass
    # CI fallback: use environment variables for any missing/empty keys
    for key in ["GHL_TOKEN_AXISKEY", "GHL_LOCATION_ID_AXISKEY", "META_TOKEN_AXISKEY"]:
        if not result.get(key):
            result[key] = os.environ.get(key, "")
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
_HIDE_PIPELINE_STAGES = {"Not a fit"}
active_stages = [(name, pos, cnt) for name, pos, cnt in sales_stages if cnt > 0 and name not in _HIDE_PIPELINE_STAGES]
funnel_labels = [row[0] for row in active_stages]
funnel_data   = [row[2] for row in active_stages]

# Save today's pipeline distribution snapshot (used by the date-select pie chart)
_today_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
_snap_dir   = Path(__file__).parent / "data/snapshots"
_snap_dir.mkdir(parents=True, exist_ok=True)
_dist_path  = _snap_dir / f"pipeline-dist-{_today_str}.json"
_dist_path.write_text(json.dumps({
    "date":       _today_str,
    "stages":     [{"name": n, "count": c} for n, c in zip(funnel_labels, funnel_data)],
    "total_open": sum(funnel_data),
}, indent=2))

# Save today's stage snapshot — used to diff against tomorrow's build for accurate movement tracking
(_snap_dir / f"stage-snap-{_today_str}.json").write_text(json.dumps({
    "date": _today_str,
    "opps": {
        opp["id"]: {
            "stage": stage_map.get(opp.get("pipelineStageId", ""), {}).get("name", ""),
            "owner": opp.get("assignedTo") or "unassigned",
        }
        for opp in all_opps
        if opp.get("pipelineId") == SALES_ID
        and opp.get("status") == "open"
        and opp.get("id")
    }
}, indent=2))

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

# ── 6c-ii. Comparison periods ────────────────────────────────────────────────

# Previous 14-day window (days 15–28 ago)
prev_14_start = (today - timedelta(days=27)).replace(hour=0,  minute=0,  second=0,  microsecond=0)
prev_14_end   = day_14_ago  # exclusive upper bound

prev_14d = sum(
    1 for opp in all_opps
    if opp.get("createdAt")
    and prev_14_start
    <= datetime.fromisoformat(opp["createdAt"].replace("Z", "+00:00"))
    < prev_14_end
)
delta_14d     = total_14d - prev_14d
delta_14d_pct = round(delta_14d / prev_14d * 100) if prev_14d else 0
delta_14d_str = f"+{delta_14d_pct}%" if delta_14d >= 0 else f"{delta_14d_pct}%"
delta_14d_dir = "↑" if delta_14d > 0 else ("↓" if delta_14d < 0 else "→")

# Last week (Mon–Sun before this week)
last_week_start = week_start - timedelta(weeks=1)
last_week_end   = week_start - timedelta(seconds=1)
last_week_new   = sum(
    1 for opp in all_opps
    if opp.get("createdAt")
    and last_week_start
    <= datetime.fromisoformat(opp["createdAt"].replace("Z", "+00:00"))
    <= last_week_end
)

# Last 2 complete calendar months dynamically
def _month_count(opps, year, month):
    return sum(
        1 for opp in opps
        if opp.get("createdAt")
        and (dt := datetime.fromisoformat(opp["createdAt"].replace("Z", "+00:00")))
        and dt.year == year and dt.month == month
    )

_this_month  = today.month
_this_year   = today.year
_m1_month    = (_this_month - 1) or 12
_m1_year     = _this_year if _this_month > 1 else _this_year - 1
_m2_month    = (_this_month - 2) or 12
_m2_year     = _this_year if _this_month > 2 else (_m1_year if _this_month == 2 else _this_year - 1)

month1_count   = _month_count(all_opps, _m1_year, _m1_month)
month2_count   = _month_count(all_opps, _m2_year, _m2_month)
cur_month_count = _month_count(all_opps, _this_year, _this_month)
month1_label   = datetime(_m1_year, _m1_month, 1).strftime("%b")
month2_label   = datetime(_m2_year, _m2_month, 1).strftime("%b")
cur_month_label = today.strftime("%b")

# Last 7 days slice from the 14-day arrays (already computed)
day_7_labels = date_labels_short[-7:]
day_7_data   = daily_data[-7:]

# Week-over-week delta
week_delta      = new_this_week - last_week_new
week_delta_pct  = round(week_delta / last_week_new * 100) if last_week_new else 0
week_delta_str  = f"+{week_delta_pct}%" if week_delta >= 0 else f"{week_delta_pct}%"
week_delta_dir  = "↑" if week_delta > 0 else ("↓" if week_delta < 0 else "→")
week_delta_color = "var(--hero)" if week_delta >= 0 else "#FF5C5C"

# Human-readable strings used in the HTML
week_range_str      = f"{week_start.strftime('%b')} {week_start.day} – {today.strftime('%b')} {today.day}"
last_week_range_str = f"{last_week_start.strftime('%b')} {last_week_start.day} – {(last_week_end).strftime('%b')} {last_week_end.day}"
generated_at        = f"{today.strftime('%B')} {today.day}, {today.year} at {today.strftime('%H:%M')} UTC"

print(f"  14-day new leads: {total_14d} (prev: {prev_14d}, {delta_14d_dir})")
print(f"  This week: {new_this_week} | Last week: {last_week_new} ({week_delta_str})")
print(f"  {month2_label}: {month2_count} | {month1_label}: {month1_count} | {cur_month_label}: {cur_month_count}")
print(f"  Open in Sales Pipeline: {sum(funnel_data)} | Won all-time: {won_total} | Stale Pipeline: {stale_count}")
print(f"  Won this week: {won_count} / {won_value_str}")
print()


# ─── 6d. MGL leads + Lead Source Breakdown ───────────────────────────────────
print("Fetching MGL + source data...")

SCORE_FIELD  = "5PgTaqgm1MH0Z26KKVcl"
SCORE_NORM   = {"Green": "1", "Yellow": "2", "Red": "3"}
POST_HEADERS = {**HEADERS, "Content-Type": "application/json"}

MGL_SOURCES = {"MGL", "FORM", "Meta Survey - Capital Raising"}
SGL_SOURCES = {"SGL", "Stormer Santana's Calendar", "Fundraising Discussion"}

mgl_opps   = [opp for opp in all_opps if opp.get("source") in MGL_SOURCES]
mgl_ids    = {opp["contactId"] for opp in mgl_opps if opp.get("contactId")}
sgl_opps   = [opp for opp in all_opps if opp.get("source") in SGL_SOURCES]
other_opps = [opp for opp in all_opps if opp.get("source") not in MGL_SOURCES and opp.get("source") not in SGL_SOURCES]

# Opportunities at "New Lead" stage or beyond in Sales Pipeline (all statuses)
_new_lead_pos = next(
    (info["position"] for sid, info in stage_map.items()
     if info["pipeline_id"] == SALES_ID and "new lead" in info["name"].lower()),
    0,
)
source_opps = [
    opp for opp in all_opps
    if opp.get("pipelineId") == SALES_ID
    and (
        opp.get("status") in ("won", "lost")
        or stage_map.get(opp.get("pipelineStageId", ""), {}).get("position", -1) >= _new_lead_pos
    )
]
# Fetch contacts for MGL quality scores only
contact_cache = {}  # cid → {"score": str}
for cid in mgl_ids:
    resp  = ghl_get(f"/contacts/{cid}")
    c     = resp.get("contact", resp)
    score = "—"
    for cf in (c.get("customFields") or []):
        if cf.get("id") == SCORE_FIELD:
            raw   = cf.get("value", "—")
            score = SCORE_NORM.get(raw, raw)
    contact_cache[cid] = {"score": score}
    time.sleep(0.05)

mgl_scores = {cid: contact_cache[cid]["score"] for cid in mgl_ids if cid in contact_cache}

# Source breakdown — opportunity source field, 3 buckets
_src_mgl   = sum(1 for opp in source_opps if (opp.get("source") or "") in MGL_SOURCES)
_src_sgl   = sum(1 for opp in source_opps if (opp.get("source") or "") in SGL_SOURCES)
_src_other = len(source_opps) - _src_mgl - _src_sgl

source_chart_labels = ["MGL", "SGL", "Other - Referrals"]
source_chart_data   = [_src_mgl, _src_sgl, _src_other]
total_source_opps   = len(source_opps)

print(f"  Source breakdown ({total_source_opps} opps): MGL={_src_mgl} SGL={_src_sgl} Other={_src_other}")

# Append today's source breakdown into the same daily snapshot file written in 6b,
# so the shared date selector can look up both pipeline stages and source mix per day.
_dist_data = json.loads(_dist_path.read_text())
_dist_data["source"] = {
    "labels": source_chart_labels,
    "data":   source_chart_data,
    "total":  total_source_opps,
}
_dist_path.write_text(json.dumps(_dist_data, indent=2))

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

# All-time Discovery → Proposal conversion (all statuses, Sales Pipeline)
disc_all_time     = sum(1 for opp in all_opps if opp.get("pipelineId") == SALES_ID and opp.get("pipelineStageId") in _discovery_stage_ids)
prop_all_time     = sum(1 for opp in all_opps if opp.get("pipelineId") == SALES_ID and opp.get("pipelineStageId") in _proposal_ids)
disc_to_prop_pct  = round(prop_all_time / disc_all_time * 100) if disc_all_time else 0

# All-time Proposal → Agreement Signed conversion (all statuses, Sales Pipeline)
_signed_pos = next(
    info["position"] for sid, info in stage_map.items()
    if info["pipeline_id"] == SALES_ID and info["name"] == "Agreement Signed"
)
_signed_stage_ids = {
    sid for sid, info in stage_map.items()
    if info["pipeline_id"] == SALES_ID and info["position"] >= _signed_pos
}
signed_all_time     = sum(1 for opp in all_opps if opp.get("pipelineId") == SALES_ID and opp.get("pipelineStageId") in _signed_stage_ids)
prop_to_signed_pct  = round(signed_all_time / prop_all_time * 100) if prop_all_time else 0

# "Won" = opportunities currently sitting in the Onboarding stage (not GHL's
# won/lost status field -- this pipeline treats reaching Onboarding as won).
_onboarding_id = next(
    sid for sid, info in stage_map.items()
    if info["pipeline_id"] == SALES_ID and info["name"] == "Onboarding"
)
onboarding_opps = [
    opp for opp in all_opps
    if opp.get("pipelineId") == SALES_ID and opp.get("pipelineStageId") == _onboarding_id
]
won_onboarding_total = len(onboarding_opps)

# Bucket onboarding entries by the month they entered that stage (lastStageChangeAt)
_won_by_month = defaultdict(int)
for opp in onboarding_opps:
    _ts = opp.get("lastStageChangeAt") or opp.get("createdAt")
    if _ts:
        _dt = datetime.fromisoformat(_ts.replace("Z", "+00:00"))
        _won_by_month[_dt.strftime("%Y-%m")] += 1

_won_month_keys = sorted(_won_by_month.keys())
_current_month_key = today.strftime("%Y-%m")

_won_month_rows = ""
_prev_count = None
for _mk in _won_month_keys:
    _cnt   = _won_by_month[_mk]
    _label = datetime.strptime(_mk, "%Y-%m").strftime("%B %Y")
    _is_current = (_mk == _current_month_key)

    if _is_current:
        _delta_html = '<span style="font-size:0.62rem;color:var(--text-mute);">month in progress — not compared yet</span>'
    elif _prev_count is None:
        _delta_html = '<span style="font-size:0.62rem;color:var(--text-mute);">first month on record</span>'
    elif _prev_count == 0:
        _delta_html = '<span style="font-size:0.62rem;color:var(--text-mute);">—</span>'
    else:
        _delta_pct = round((_cnt - _prev_count) / _prev_count * 100)
        _d_color = "var(--hero)" if _delta_pct > 0 else ("#FF5C5C" if _delta_pct < 0 else "var(--text-mute)")
        _d_dir   = "↑" if _delta_pct > 0 else ("↓" if _delta_pct < 0 else "→")
        _delta_html = (
            f'<span style="display:inline-flex;align-items:center;gap:4px;background:var(--surface-2);'
            f'border-radius:6px;padding:2px 8px;font-size:0.68rem;font-weight:800;color:{_d_color};">'
            f'{_d_dir}&thinsp;{abs(_delta_pct)}% <span style="color:var(--text-mute);font-weight:600;">MoM</span></span>'
        )

    _won_month_rows += (
        f'<div style="display:grid;grid-template-columns:160px 60px 1fr;align-items:center;gap:14px;'
        f'padding:10px 0;border-bottom:1px solid var(--line);">'
        f'<span style="font-size:0.8rem;color:var(--text);">{_label}{" (in progress)" if _is_current else ""}</span>'
        f'<span style="font-size:1.1rem;font-weight:800;color:var(--hero);">{_cnt}</span>'
        f'<span>{_delta_html}</span>'
        f'</div>'
    )
    if not _is_current:
        _prev_count = _cnt

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
mgl_dc_plus_total  = len(mgl_open_sales_cids)
mgl_dc_unscored    = mgl_dc_plus_total - mgl_total_scored

# SGL and Other contacts at Discovery Call or beyond
sgl_open_sales_cids = {
    opp["contactId"] for opp in sgl_opps
    if opp.get("pipelineId") == SALES_ID
    and opp.get("pipelineStageId") in _discovery_stage_ids
    and opp.get("contactId")
}
other_open_sales_cids = {
    opp["contactId"] for opp in other_opps
    if opp.get("pipelineId") == SALES_ID
    and opp.get("pipelineStageId") in _discovery_stage_ids
    and opp.get("contactId")
}

# Fetch scores for any SGL/Other DC+ contacts not already cached
for cid in (sgl_open_sales_cids | other_open_sales_cids) - set(contact_cache.keys()):
    resp  = ghl_get(f"/contacts/{cid}")
    c     = resp.get("contact", resp)
    score = "—"
    for cf in (c.get("customFields") or []):
        if cf.get("id") == SCORE_FIELD:
            raw   = cf.get("value", "—")
            score = SCORE_NORM.get(raw, raw)
    contact_cache[cid] = {"score": score}
    time.sleep(0.05)

sgl_buckets = {"1": 0, "2": 0, "3": 0}
for cid in sgl_open_sales_cids:
    score = contact_cache.get(cid, {}).get("score", "—")
    if score in sgl_buckets:
        sgl_buckets[score] += 1
sgl_total_scored  = sum(sgl_buckets.values())
sgl_dc_plus_total = len(sgl_open_sales_cids)
sgl_dc_unscored   = sgl_dc_plus_total - sgl_total_scored

other_buckets = {"1": 0, "2": 0, "3": 0}
for cid in other_open_sales_cids:
    score = contact_cache.get(cid, {}).get("score", "—")
    if score in other_buckets:
        other_buckets[score] += 1
other_total_scored  = sum(other_buckets.values())
other_dc_plus_total = len(other_open_sales_cids)
other_dc_unscored   = other_dc_plus_total - other_total_scored

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


# ─── 6e. Weekly Rocks — weekly stage-entry table ─────────────────────────────
# For each of 4 key stages, count how many DISTINCT opportunities ENTERED that
# stage during each week (i.e. their stage on some day that week differs from
# their stage on the previous snapshotted day, or they're newly seen). Week 1
# (W1) is Jul 1-7, 2026 -- the first week we have snapshot data for. Note: Jul 1
# itself has no prior-day snapshot to diff against, so entries already sitting
# in a stage as of Jul 1 can't be counted (unknowable whether they arrived that
# day or earlier) -- W1 only reflects entries detected Jul 2 onward.

print("Computing weekly rocks...")

STAGE_LABELS = ["Discovery Call", "Strategy Call", "Proposal Sent", "Agreement Signed"]
MOVE_LABELS  = ["Discovery Calls", "Strategy Calls", "Proposals Sent", "Agreements Signed"]
_stage_display = dict(zip(STAGE_LABELS, MOVE_LABELS))

WEEK1_START = datetime(2026, 7, 1, tzinfo=timezone.utc).date()

def _week_index(date_obj):
    return (date_obj - WEEK1_START).days // 7 + 1

# Diff consecutive daily stage snapshots: an opp "enters" a stage on the day its
# current stage differs from its previous day's stage (or it's newly created).
_stage_snaps = sorted(_snap_dir.glob("stage-snap-*.json"))
weekly_movement = defaultdict(lambda: {lbl: 0 for lbl in MOVE_LABELS})

for i in range(1, len(_stage_snaps)):
    prev_data = json.loads(_stage_snaps[i - 1].read_text())
    curr_data = json.loads(_stage_snaps[i].read_text())
    _d = datetime.strptime(curr_data["date"], "%Y-%m-%d").date()
    if _d < WEEK1_START:
        continue
    _wk = _week_index(_d)
    for opp_id, curr_opp in curr_data["opps"].items():
        curr_stage = curr_opp.get("stage")
        if curr_stage not in STAGE_LABELS:
            continue
        prev_opp   = prev_data["opps"].get(opp_id)
        prev_stage = prev_opp["stage"] if prev_opp else None
        if prev_stage != curr_stage:
            weekly_movement[_wk][_stage_display[curr_stage]] += 1

move_display_weeks = sorted(weekly_movement.keys())
_current_week_idx  = _week_index(today.date())

def _week_range_str(wk):
    _start = WEEK1_START + timedelta(days=(wk - 1) * 7)
    _end   = _start + timedelta(days=6)
    return f"{_start.strftime('%b %-d')}–{_end.strftime('%-d')}"

move_display_labels = [
    f"W{wk}" + (" *" if wk == _current_week_idx else "")
    for wk in move_display_weeks
]

print(f"  {len(_stage_snaps)} snapshots · {len(weekly_movement)} week(s) computed")
print()


# ─── 6g. Meta Campaign Spending — last 7 days ────────────────────────────────
print("Fetching Meta spend data...")
meta_end   = today.date()
meta_start = meta_end - timedelta(days=89)   # 90 days for Marketing section date-range picker

meta_resp  = meta_get(f"/v21.0/{META_ACCT}/insights", {
    "fields":         "date_start,spend,inline_link_clicks",
    "time_range":     json.dumps({"since": str(meta_start), "until": str(meta_end)}),
    "time_increment": "1",
    "limit":          200,  # default page size (25) truncates a 90-day daily breakdown
})

if "error" in meta_resp:
    print(f"  Meta API error: {meta_resp['error'].get('message')} — skipping section")
    meta_rows = []
else:
    meta_rows = sorted(meta_resp.get("data", []), key=lambda r: r["date_start"])

# ── MGL leads per day (for marketing table + CPL) ────────────────────────────
mgl_by_date = defaultdict(int)
for opp in mgl_opps:
    if opp.get("createdAt"):
        _d = datetime.fromisoformat(opp["createdAt"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
        mgl_by_date[_d] += 1

# Combined 90-day daily data for the Marketing & Leads metrics table
# Excludes today -- Meta spend/clicks are incomplete until the day closes out.
_mktg_end   = meta_end - timedelta(days=1)
_mktg_start = _mktg_end - timedelta(days=89)
_meta_by_date = {r["date_start"]: r for r in meta_rows}
mktg_daily = []
for i in range(90):
    _ds     = (_mktg_start + timedelta(days=i)).strftime("%Y-%m-%d")
    _r      = _meta_by_date.get(_ds, {})
    _spend  = float(_r.get("spend", 0))
    _clicks = int(_r.get("inline_link_clicks", 0))
    _cpc    = round(_spend / _clicks, 2) if _clicks else 0
    _leads  = mgl_by_date.get(_ds, 0)
    _conv   = round(_leads / _clicks * 100, 1) if _clicks else 0
    mktg_daily.append({
        "date": _ds,
        "label": datetime.strptime(_ds, "%Y-%m-%d").strftime("%-m/%-d"),
        "spend": round(_spend, 2),
        "clicks": _clicks,
        "cpc": _cpc,
        "leads": _leads,
        "conv_pct": _conv,
    })

mktg_min_date = mktg_daily[0]["date"]
mktg_max_date = mktg_daily[-1]["date"]

# ── CPL: last full week vs previous full week ─────────────────────────────────
_lw_dates   = {(last_week_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)}
_prev_dates = {(last_week_start - timedelta(days=7-i)).strftime("%Y-%m-%d") for i in range(7)}

_lw_spend   = sum(float(r.get("spend", 0)) for r in meta_rows if r["date_start"] in _lw_dates)
_lw_leads   = sum(mgl_by_date.get(d, 0) for d in _lw_dates)
_prev_spend = sum(float(r.get("spend", 0)) for r in meta_rows if r["date_start"] in _prev_dates)
_prev_leads = sum(mgl_by_date.get(d, 0) for d in _prev_dates)

cpl_lw   = _lw_spend   / _lw_leads   if _lw_leads   else None
cpl_prev = _prev_spend / _prev_leads if _prev_leads else None
cpl_lw_str = f"${cpl_lw:.0f}" if cpl_lw else "—"

if cpl_lw and cpl_prev:
    _cpl_delta     = round((cpl_lw - cpl_prev) / cpl_prev * 100)
    cpl_wow_str    = f"+{_cpl_delta}%" if _cpl_delta >= 0 else f"{_cpl_delta}%"
    cpl_wow_dir    = "↑" if _cpl_delta > 0 else ("↓" if _cpl_delta < 0 else "→")
    cpl_wow_color  = "#FF5C5C" if _cpl_delta > 0 else "var(--hero)"  # lower CPL = better
else:
    cpl_wow_str = "—"; cpl_wow_dir = ""; cpl_wow_color = "var(--text-mute)"

_cpl_range_str = f"{last_week_start.strftime('%b %-d')} – {(last_week_start + timedelta(days=6)).strftime('%b %-d')}"

# Last 7-day window for the Meta spending section (bottom of dashboard)
meta_rows_7  = meta_rows[-7:]
meta_labels  = [datetime.strptime(r["date_start"], "%Y-%m-%d").strftime("%-m/%-d") for r in meta_rows_7]
meta_spends  = [float(r.get("spend", 0)) for r in meta_rows_7]
meta_total   = sum(meta_spends)
meta_avg     = meta_total / len(meta_spends) if meta_spends else 0
meta_today_v = meta_spends[-1] if meta_spends else 0

meta_start_fmt = datetime.strptime(meta_rows_7[0]["date_start"], "%Y-%m-%d").strftime("%b %-d") if meta_rows_7 else ""
meta_end_fmt   = datetime.strptime(meta_rows_7[-1]["date_start"], "%Y-%m-%d").strftime("%b %-d") if meta_rows_7 else ""
meta_range_str = f"{meta_start_fmt} – {meta_end_fmt} · AxisKey"
meta_total_str = f"${meta_total:,.0f}"
meta_avg_str   = f"${meta_avg:,.0f}"
meta_today_str = f"${meta_today_v:,.0f}"

print(f"  90-day fetch | last-7: {meta_total_str} total | CPL last week: {cpl_lw_str}")
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
  <link rel="icon" type="image/svg+xml" href="assets/favicon.svg">
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
    .smv-table th.smv-th-total { border-left: 1px solid var(--line); color: var(--text); }
    .smv-table td.smv-total { border-left: 1px solid var(--line); color: var(--text); font-size: 0.88rem; font-weight: 800; }
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

    /* Marketing & Leads — period toggle buttons */
    .mktg-btn {
      background: var(--surface-2);
      color: var(--text-mute);
      border: none;
      border-radius: 5px;
      padding: 3px 8px;
      font-family: inherit;
      font-size: 0.62rem;
      font-weight: 600;
      cursor: pointer;
      letter-spacing: 0.06em;
    }
    .mktg-btn-active {
      background: var(--hero);
      color: #000;
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

# ── 7c. Marketing & Leads hero section ───────────────────────────────────────
HERO = f"""
  <section class="section-hero card">

    <!-- Title row + CPL hero -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;">
      <div class="card-label" style="margin-bottom:0;">Marketing &amp; Leads</div>
      <div style="display:flex;align-items:baseline;gap:12px;">
        <div style="font-size:2.2rem;font-weight:800;color:var(--hero);line-height:1;">{cpl_lw_str}</div>
        <div style="font-size:0.56rem;font-weight:600;letter-spacing:0.16em;text-transform:uppercase;color:var(--text-mute);">CPL</div>
        <div style="display:inline-flex;align-items:center;gap:5px;background:var(--surface-2);border-radius:6px;padding:3px 9px;">
          <span style="font-size:0.8rem;font-weight:800;color:{cpl_wow_color};">{cpl_wow_dir}&thinsp;{cpl_wow_str}</span>
          <span style="font-size:0.48rem;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:var(--text-mute);">WoW</span>
        </div>
        <div style="font-size:0.58rem;color:var(--text-mute);">{_cpl_range_str}</div>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:220px 200px 1fr;gap:0;align-items:start;">

      <!-- ── Col 1: New Leads KPI ── -->
      <div style="border-right:1px solid var(--line);padding-right:24px;padding-top:2px;">
        <div style="margin-bottom:14px;">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
            <div style="font-size:0.56rem;font-weight:600;letter-spacing:0.18em;text-transform:uppercase;color:var(--text-mute);">This Week</div>
            <div style="display:inline-flex;align-items:center;gap:4px;background:var(--surface-2);border-radius:6px;padding:2px 7px;">
              <span style="font-size:0.75rem;font-weight:800;color:{week_delta_color};">{week_delta_dir}&thinsp;{week_delta_str}</span>
              <span style="font-size:0.46rem;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:var(--text-mute);">WoW</span>
            </div>
          </div>
          <div style="font-size:3.4rem;font-weight:800;color:var(--hero);line-height:1;">{new_this_week}</div>
          <div style="font-size:0.6rem;color:var(--text-mute);margin-top:5px;">{week_range_str}</div>
        </div>
        <div style="height:1px;background:var(--line);margin-bottom:13px;"></div>
        <div>
          <div style="font-size:0.56rem;font-weight:600;letter-spacing:0.18em;text-transform:uppercase;color:var(--text-mute);margin-bottom:5px;">Last Week</div>
          <div style="font-size:2.0rem;font-weight:800;color:var(--text);line-height:1;">{last_week_new}</div>
          <div style="font-size:0.6rem;color:var(--text-mute);margin-top:4px;">{last_week_range_str}</div>
        </div>
      </div>

      <!-- ── Col 2: Monthly Volume + MGL badge ── -->
      <div style="border-right:1px solid var(--line);padding:0 24px;padding-top:2px;">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
          <div style="font-size:0.56rem;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:var(--text-mute);">Monthly Volume</div>
          <span class="mgl-pill" style="font-size:0.56rem;padding:2px 7px;white-space:nowrap;">MGL&nbsp;{mgl_14d}&nbsp;·&nbsp;{mgl_14d_pct}%</span>
        </div>
        <div style="position:relative;height:168px;"><canvas id="chartMonthly"></canvas></div>
      </div>

      <!-- ── Col 3: Daily Performance metrics table ── -->
      <div style="padding-left:24px;">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:8px;">
          <div style="font-size:0.56rem;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:var(--text-mute);">Daily Performance · MGL</div>
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
            <div style="display:flex;gap:4px;">
              <button onclick="setMktgPeriod(7,this)" class="mktg-btn mktg-btn-active">7d</button>
              <button onclick="setMktgPeriod(14,this)" class="mktg-btn">14d</button>
              <button onclick="setMktgPeriod(28,this)" class="mktg-btn">28d</button>
            </div>
            <div style="display:flex;gap:5px;align-items:center;">
              <input type="date" id="mktgFrom" min="{mktg_min_date}" max="{mktg_max_date}" value="{mktg_min_date}"
                style="background:var(--surface-2);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-family:inherit;font-size:0.68rem;outline:none;color-scheme:dark;">
              <span style="font-size:0.62rem;color:var(--text-mute);">–</span>
              <input type="date" id="mktgTo" min="{mktg_min_date}" max="{mktg_max_date}" value="{mktg_max_date}"
                style="background:var(--surface-2);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-family:inherit;font-size:0.68rem;outline:none;color-scheme:dark;">
              <button onclick="applyMktgRange()" class="mktg-btn">Go</button>
            </div>
          </div>
        </div>
        <div id="mktgTable" style="max-height:190px;overflow-y:auto;"></div>
      </div>

    </div>
  </section>
"""

SHARED_DATE_HEADER = f"""
  <div style="display:flex;align-items:center;justify-content:flex-end;gap:10px;margin-bottom:10px;">
    <span style="font-size:0.62rem;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:var(--text-mute);">Viewing Snapshot</span>
    <select id="sharedDateSelect" style="background:var(--surface-2);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:4px 12px;font-family:inherit;font-size:0.78rem;font-weight:600;cursor:pointer;outline:none;"></select>
  </div>
"""

MGL_CHART = f"""
  <div class="card" style="margin-bottom:22px;">
    <div class="card-label">Lead Sources — New Lead &amp; Beyond &nbsp;·&nbsp; <span id="sourceTotalLabel">{total_source_opps} Opportunities</span></div>
    <div style="display:grid;grid-template-columns:3fr 2fr;gap:28px;align-items:stretch;">

      <!-- Left: source bar chart — constrained width so bars cluster together -->
      <div style="display:flex;align-items:center;justify-content:center;">
        <div style="position:relative;width:300px;height:100%;">
          <canvas id="chartSource"></canvas>
          <div id="sourceNoData" style="display:none;position:absolute;inset:0;align-items:center;justify-content:center;text-align:center;font-size:0.7rem;color:var(--text-mute);padding:0 20px;">Source breakdown wasn't tracked for this date yet.</div>
        </div>
      </div>

      <!-- Right: 3-column call quality table -->
      <div>
        <div style="font-size:0.56rem;font-weight:600;letter-spacing:0.16em;text-transform:uppercase;color:var(--text-mute);margin-bottom:12px;">Call Quality &nbsp;·&nbsp; Discovery Call+</div>

        <!-- Header row: labels + totals -->
        <div style="display:grid;grid-template-columns:90px 1fr 1fr 1fr;gap:14px;align-items:end;border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:2px;">
          <span></span>
          <div style="text-align:center;">
            <div style="font-size:0.56rem;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:#C8FF01;margin-bottom:4px;">MGL</div>
            <div style="font-size:1.5rem;font-weight:800;color:#C8FF01;line-height:1;">{mgl_dc_plus_total}</div>
          </div>
          <div style="text-align:center;">
            <div style="font-size:0.56rem;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:#5B8FFF;margin-bottom:4px;">SGL</div>
            <div style="font-size:1.5rem;font-weight:800;color:#5B8FFF;line-height:1;">{sgl_dc_plus_total}</div>
          </div>
          <div style="text-align:center;">
            <div style="font-size:0.56rem;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:#FF9F45;margin-bottom:4px;">Other</div>
            <div style="font-size:1.5rem;font-weight:800;color:#FF9F45;line-height:1;">{other_dc_plus_total}</div>
          </div>
        </div>

        <!-- Score rows -->
        <div style="display:grid;grid-template-columns:90px 1fr 1fr 1fr;gap:14px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line);">
          <span style="font-size:0.75rem;color:var(--text);">🟢 Great Fit</span>
          <span style="font-size:0.95rem;font-weight:800;color:var(--hero);text-align:center;">{mgl_buckets["1"]}</span>
          <span style="font-size:0.95rem;font-weight:800;color:var(--hero);text-align:center;">{sgl_buckets["1"]}</span>
          <span style="font-size:0.95rem;font-weight:800;color:var(--hero);text-align:center;">{other_buckets["1"]}</span>
        </div>
        <div style="display:grid;grid-template-columns:90px 1fr 1fr 1fr;gap:14px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line);">
          <span style="font-size:0.75rem;color:var(--text);">🟡 Potential</span>
          <span style="font-size:0.95rem;font-weight:800;color:var(--text);text-align:center;">{mgl_buckets["2"]}</span>
          <span style="font-size:0.95rem;font-weight:800;color:var(--text);text-align:center;">{sgl_buckets["2"]}</span>
          <span style="font-size:0.95rem;font-weight:800;color:var(--text);text-align:center;">{other_buckets["2"]}</span>
        </div>
        <div style="display:grid;grid-template-columns:90px 1fr 1fr 1fr;gap:14px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line);">
          <span style="font-size:0.75rem;color:var(--text);">🔴 Poor Fit</span>
          <span style="font-size:0.95rem;font-weight:800;color:var(--text);text-align:center;">{mgl_buckets["3"]}</span>
          <span style="font-size:0.95rem;font-weight:800;color:var(--text);text-align:center;">{sgl_buckets["3"]}</span>
          <span style="font-size:0.95rem;font-weight:800;color:var(--text);text-align:center;">{other_buckets["3"]}</span>
        </div>
        <div style="display:grid;grid-template-columns:90px 1fr 1fr 1fr;gap:14px;align-items:center;padding:10px 0;">
          <span style="font-size:0.75rem;color:var(--text-mute);">⬜ Unscored</span>
          <span style="font-size:0.95rem;font-weight:800;color:var(--text-mute);text-align:center;">{mgl_dc_unscored}</span>
          <span style="font-size:0.95rem;font-weight:800;color:var(--text-mute);text-align:center;">{sgl_dc_unscored}</span>
          <span style="font-size:0.95rem;font-weight:800;color:var(--text-mute);text-align:center;">{other_dc_unscored}</span>
        </div>
      </div>

    </div>
  </div>
"""

# ── 7d. Pipeline Snapshot — segmented bar with date selector ─────────────────
MIDDLE = f"""
  <div style="margin-bottom:22px;">
    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;">
        <div class="card-label" style="margin-bottom:0;">Pipeline Snapshot — Open Deals by Stage</div>
        <span id="pipelineTotal" style="font-size:0.72rem;font-weight:600;color:var(--text-mute);"></span>
      </div>

      <!-- Segmented bar -->
      <div id="pipelineBar" style="display:flex;border-radius:10px;overflow:hidden;height:52px;gap:2px;background:#0F0F14;margin-bottom:16px;"></div>

      <!-- Legend -->
      <div id="pipelineLegend" style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px 0;margin-bottom:20px;"></div>

      <div class="funnel-kpis" style="margin-top:4px;">
        <div class="funnel-kpi">
          <span class="funnel-kpi-label">Discovery to Proposal</span>
          <span class="funnel-kpi-value">{disc_to_prop_pct}%</span>
          <span class="funnel-kpi-sub">{prop_all_time} of {disc_all_time} discoveries</span>
        </div>
        <div class="funnel-kpi">
          <span class="funnel-kpi-label">Proposal to Signed</span>
          <span class="funnel-kpi-value">{prop_to_signed_pct}%</span>
          <span class="funnel-kpi-sub">{signed_all_time} of {prop_all_time} proposals</span>
        </div>
        <div class="funnel-kpi">
          <span class="funnel-kpi-label">Lead to Won</span>
          <span class="funnel-kpi-value">{won_rate_pct}%</span>
          <span class="funnel-kpi-sub">{won_total} won of {total_sales_opps} total</span>
        </div>
        <div class="funnel-kpi">
          <span class="funnel-kpi-label">Won</span>
          <span class="funnel-kpi-value">{won_onboarding_total}</span>
          <span class="funnel-kpi-sub">currently in Onboarding</span>
        </div>
      </div>

      <div style="margin-top:22px;padding-top:20px;border-top:1px solid var(--line);">
        <div class="card-label" style="margin-bottom:4px;">Won Deals — Entered Onboarding by Month</div>
        <div style="font-size:0.68rem;color:var(--text-mute);margin-bottom:14px;">Based on lastStageChangeAt · month-over-month vs. prior completed month</div>
        {_won_month_rows if _won_month_rows else '<div class="comp-none">None recorded yet.</div>'}
      </div>
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

# ── 7i. Weekly Rocks matrix (snapshot-diff based — accurate forward movements)
def _build_stage_movement():
    if not weekly_movement:
        return (
            '<div class="smv-note" style="text-align:center;padding:32px 0;">'
            'First snapshot captured today — movement data will appear on the next build.'
            '</div>'
        )

    week_ths = "".join(
        f'<th title="{_week_range_str(wk)}">{lbl}</th>'
        for wk, lbl in zip(move_display_weeks, move_display_labels)
    )
    thead    = (f'<thead><tr>'
                f'<th class="smv-th-stage">Stage</th>'
                f'{week_ths}'
                f'<th class="smv-th-total">Total</th>'
                f'</tr></thead>')

    tbody_rows = ""
    for label in MOVE_LABELS:
        week_tds  = ""
        row_total = 0
        for wk in move_display_weeks:
            val = weekly_movement[wk].get(label, 0)
            if wk != _current_week_idx:
                row_total += val
            if val > 0:
                week_tds += f'<td class="smv-val smv-val-pos">{val}</td>'
            else:
                week_tds += f'<td class="smv-val smv-val-zero">—</td>'
        total_td    = f'<td class="smv-val smv-total">{row_total if row_total else "—"}</td>'
        tbody_rows += f'<tr><td class="smv-stage-cell">{label}</td>{week_tds}{total_td}</tr>\n'

    tbody = f"<tbody>{tbody_rows}</tbody>"
    note  = (
        '<div class="smv-note">Distinct opportunities that entered each stage that week · Sales Pipeline · based on daily snapshots'
        + ' · W1 undercounts slightly: Jul 1 was our first snapshot, so opps already sitting in a stage that day can\'t be counted as entering it'
        + (' · * current week in progress, excluded from Total' if _current_week_idx in move_display_weeks else '')
        + '</div>'
    )
    return f'<div class="smv-wrap"><table class="smv-table">{thead}{tbody}</table></div>{note}'

STAGE_MOVEMENT = f"""
  <section class="card" style="margin-top:22px;">
    <div class="card-label">Weekly Rocks</div>
    {_build_stage_movement()}
  </section>
"""

# ── 7g. Data injection (f-string — embeds computed JSON arrays) ───────────────
# Load all saved pipeline distribution snapshots for the date selector dropdown
pipeline_history = {}
for _sp in sorted((Path(__file__).parent / "data/snapshots").glob("pipeline-dist-*.json")):
    _sd = json.loads(_sp.read_text())
    pipeline_history[_sd["date"]] = _sd
pipeline_dates = sorted(pipeline_history.keys(), reverse=True)

# Chart.js reads these constants from the next <script> block.
DATA_SCRIPT = f"""
  <script>
    // All values injected by build_dashboard.py — re-run the script to refresh.
    const DAY7_LABELS     = {json.dumps(day_7_labels)};
    const DAY7_DATA       = {json.dumps(day_7_data)};
    const MONTH_LABELS    = {json.dumps([month2_label, month1_label, cur_month_label])};
    const MONTH_DATA      = {json.dumps([month2_count, month1_count, cur_month_count])};
    const META_LABELS     = {json.dumps(meta_labels)};
    const META_SPEND      = {json.dumps(meta_spends)};
    const SOURCE_LABELS      = {json.dumps(source_chart_labels)};
    const SOURCE_DATA        = {json.dumps(source_chart_data)};
    const PIPELINE_HISTORY   = {json.dumps(pipeline_history)};
    const PIPELINE_DATES     = {json.dumps(pipeline_dates)};
    const MKTG_DAILY         = {json.dumps(mktg_daily)};
  </script>
"""

# ── 7g. Chart initialization (regular string — lots of JS {{ }} chars) ────────
CHARTS_SCRIPT = """
  <script>
    // Shared defaults applied to every chart on the page
    Chart.defaults.color       = "#9A9AA5";
    Chart.defaults.font.family = '"Saira", "Eurostile", system-ui, sans-serif';
    Chart.defaults.font.size   = 12;

    // ── Marketing & Leads — daily metrics table ──────────────────────────
    function renderMktgTable(days) {
      renderMktgRows(MKTG_DAILY.slice(-days));
    }
    function applyMktgRange() {
      document.querySelectorAll(".mktg-btn").forEach(b => b.classList.remove("mktg-btn-active"));
      const from = document.getElementById("mktgFrom").value;
      const to   = document.getElementById("mktgTo").value;
      renderMktgRows(MKTG_DAILY.filter(r => r.date >= from && r.date <= to));
    }
    function renderMktgRows(rows) {
      const totSpend  = rows.reduce((s, r) => s + r.spend,  0);
      const totClicks = rows.reduce((s, r) => s + r.clicks, 0);
      const totLeads  = rows.reduce((s, r) => s + r.leads,  0);
      const totCpc    = totClicks ? totSpend / totClicks : 0;
      const totConv   = totClicks ? (totLeads / totClicks * 100) : 0;
      const fS  = v => v >= 1000 ? `$${(v/1000).toFixed(1)}k` : `$${v.toFixed(0)}`;
      const fC  = v => `$${v.toFixed(2)}`;
      const th  = (t, a) => `<th style="text-align:${a||'right'};padding:3px 5px 5px ${a==='left'?'0':'5px'};color:var(--text-mute);font-size:0.65rem;font-weight:600;white-space:nowrap;">${t}</th>`;
      const td  = (t, col, fw) => `<td style="padding:4px 5px;text-align:right;color:${col||'var(--text)'};font-weight:${fw||400};font-size:0.72rem;">${t}</td>`;
      const td0 = (t) => `<td style="padding:4px 5px 4px 0;color:var(--text-mute);font-size:0.72rem;">${t}</td>`;
      let body = "";
      [...rows].reverse().forEach(r => {
        body += `<tr style="border-bottom:1px solid #1A1A22;">
          ${td0(r.label)}
          ${td(fS(r.spend))}
          ${td(r.clicks || "—", r.clicks ? undefined : "var(--text-mute)")}
          ${td(r.cpc > 0 ? fC(r.cpc) : "—", r.cpc > 0 ? undefined : "var(--text-mute)")}
          ${td(r.leads > 0 ? r.leads : "—", r.leads > 0 ? "var(--hero)" : "var(--text-mute)")}
          ${td(r.conv_pct > 0 ? r.conv_pct + "%" : "—", r.conv_pct > 0 ? undefined : "var(--text-mute)")}
        </tr>`;
      });
      const foot = `<tr style="border-top:1px solid var(--line);">
        <td style="padding:5px 5px 3px 0;color:var(--text-mute);font-size:0.72rem;font-weight:700;">Total</td>
        ${td(fS(totSpend), undefined, 700)}
        ${td(totClicks || "—", totClicks ? undefined : "var(--text-mute)", 700)}
        ${td(totClicks ? fC(totCpc) : "—", totClicks ? undefined : "var(--text-mute)", 700)}
        ${td(totLeads > 0 ? totLeads : "—", totLeads > 0 ? "var(--hero)" : "var(--text-mute)", 700)}
        ${td(totConv > 0 ? totConv.toFixed(1) + "%" : "—", totConv > 0 ? undefined : "var(--text-mute)", 700)}
      </tr>`;
      document.getElementById("mktgTable").innerHTML =
        `<table style="width:100%;border-collapse:collapse;">
          <thead><tr style="border-bottom:1px solid var(--line);">
            ${th("Date","left")}${th("Spend")}${th("Clicks")}${th("CPC")}${th("Leads")}${th("Conv%")}
          </tr></thead>
          <tbody>${body}</tbody>
          <tfoot>${foot}</tfoot>
        </table>`;
    }
    function setMktgPeriod(days, btn) {
      document.querySelectorAll(".mktg-btn").forEach(b => b.classList.remove("mktg-btn-active"));
      btn.classList.add("mktg-btn-active");
      renderMktgTable(days);
    }
    renderMktgTable(7);

    // ── Monthly volume — with inline data labels ───────────────────────────
    const monthlyDataLabels = {
      id: "monthlyLabels",
      afterDatasetsDraw(chart) {
        const ctx = chart.ctx;
        chart.data.datasets.forEach((ds, i) => {
          chart.getDatasetMeta(i).data.forEach((bar, idx) => {
            const v = ds.data[idx];
            if (!v) return;
            ctx.save();
            ctx.fillStyle   = "#F5F5F7";
            ctx.font        = '700 12px "Saira", system-ui, sans-serif';
            ctx.textAlign   = "center";
            ctx.textBaseline = "bottom";
            ctx.fillText(v, bar.x, bar.y - 4);
            ctx.restore();
          });
        });
      }
    };
    new Chart(document.getElementById("chartMonthly"), {
      type: "bar",
      data: {
        labels: MONTH_LABELS,
        datasets: [{
          data:               MONTH_DATA,
          backgroundColor:    ["#C8FF01", "#5B8FFF", "rgba(200,255,1,0.30)"],
          borderRadius:       5,
          borderSkipped:      false,
          barPercentage:      0.70,
          categoryPercentage: 0.68,
        }],
      },
      options: {
        responsive:          true,
        maintainAspectRatio: false,
        layout: { padding: { top: 20 } },
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            callbacks: { label: ctx => ` ${ctx.parsed.y} new leads` },
          },
        },
        scales: {
          x: { grid: { display: false }, ticks: { color: "#9A9AA5", font: { size: 11 } } },
          y: { display: false, beginAtZero: true },
        },
      },
      plugins: [monthlyDataLabels],
    });

    // ── Lead Source Breakdown (vertical bar) ──────────────────────────────
    const sourceDataLabels = {
      id: "sourceDataLabels",
      afterDatasetsDraw(chart) {
        const {ctx} = chart;
        const data  = chart.data.datasets[0].data;
        const total = data.reduce((a, b) => a + b, 0) || 1;
        chart.getDatasetMeta(0).data.forEach((bar, i) => {
          const v       = data[i];
          const pct     = Math.round(v / total * 100);
          const barH    = bar.base - bar.y;
          const inside  = barH > 32;
          ctx.save();
          ctx.textAlign    = "center";
          ctx.textBaseline = "bottom";
          // Count — just above bar when pct is inside; shifted higher when both go above
          ctx.font      = '700 12px "Saira", system-ui, sans-serif';
          ctx.fillStyle = "#F5F5F7";
          ctx.fillText(v, bar.x, inside ? bar.y - 4 : bar.y - 17);
          // Percentage
          if (inside) {
            ctx.font         = `600 ${barH > 60 ? 11 : 10}px "Saira", system-ui, sans-serif`;
            ctx.fillStyle    = barH > 60 ? "rgba(0,0,0,0.65)" : "rgba(255,255,255,0.82)";
            ctx.textBaseline = "middle";
            ctx.fillText(`${pct}%`, bar.x, bar.y + barH / 2);
          } else {
            // Short bar: pct sits just above bar top, below the count
            ctx.font      = '600 10px "Saira", system-ui, sans-serif';
            ctx.fillStyle = "#9A9AA5";
            ctx.fillText(`${pct}%`, bar.x, bar.y - 3);
          }
          ctx.restore();
        });
      }
    };
    const sourceChart = new Chart(document.getElementById("chartSource"), {
      type: "bar",
      data: {
        labels: SOURCE_LABELS,
        datasets: [{
          data:               SOURCE_DATA,
          backgroundColor:    ["#C8FF01", "#5B8FFF", "#FF9F45"],
          borderRadius:       8,
          borderSkipped:      false,
          barPercentage:      1.0,
          categoryPercentage: 0.95,
        }],
      },
      options: {
        responsive:          true,
        maintainAspectRatio: false,
        layout: { padding: { top: 28 } },
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            callbacks: {
              label: ctx => {
                const data  = ctx.chart.data.datasets[0].data;
                const total = data.reduce((a, b) => a + b, 0) || 1;
                const pct   = Math.round(ctx.parsed.y / total * 100);
                return ` ${ctx.parsed.y} opportunities · ${pct}%`;
              },
            },
          },
        },
        scales: {
          x: { grid: { display: false }, ticks: { color: "#9A9AA5", font: { size: 11 } } },
          y: { display: false, beginAtZero: true },
        },
      },
      plugins: [sourceDataLabels],
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


    // ── Pipeline segmented bar — date-selectable ─────────────────────────────
    const PIPE_COLORS = ["#C8FF01","#5B8FFF","#FF9F45","#A78BFA","#34D399","#FB923C","#F472B6"];

    function fmtPipeDate(s) {
      return new Date(s + "T12:00:00").toLocaleDateString("en-US",{month:"short",day:"numeric",year:"numeric"});
    }

    const pipeSel = document.getElementById("sharedDateSelect");
    PIPELINE_DATES.forEach(d => {
      const opt = document.createElement("option");
      opt.value = d; opt.textContent = fmtPipeDate(d);
      pipeSel.appendChild(opt);
    });

    function renderPipeline(dateKey) {
      const snap  = PIPELINE_HISTORY[dateKey];
      const total = snap.total_open;

      // Total label
      document.getElementById("pipelineTotal").textContent = `${total} open deals`;

      // Segmented bar
      document.getElementById("pipelineBar").innerHTML = snap.stages.map((s, i) => {
        const w   = (s.count / total * 100).toFixed(2);
        const pct = Math.round(s.count / total * 100);
        const col = PIPE_COLORS[i % PIPE_COLORS.length];
        return `<div style="width:${w}%;background:${col};flex-shrink:0;position:relative;cursor:default;"
                     title="${s.name}: ${s.count} deals (${pct}%)"></div>`;
      }).join("");

      // Legend
      document.getElementById("pipelineLegend").innerHTML = snap.stages.map((s, i) => {
        const pct = Math.round(s.count / total * 100);
        const col = PIPE_COLORS[i % PIPE_COLORS.length];
        return `<div style="display:flex;align-items:center;gap:5px;">
          <span style="width:10px;height:10px;border-radius:3px;background:${col};flex-shrink:0;"></span>
          <span style="font-size:0.74rem;color:var(--text-mute);white-space:nowrap;">${s.name}</span>
          <span style="font-size:0.78rem;font-weight:700;color:var(--text);">·&thinsp;${s.count}</span>
          <span style="font-size:0.66rem;color:var(--text-mute);">(${pct}%)</span>
        </div>`;
      }).join("");
    }

    // ── Lead Source bar — driven by the same shared date selector ─────────
    function renderSource(dateKey) {
      const snap = PIPELINE_HISTORY[dateKey];
      const src  = snap.source;
      const noDataEl = document.getElementById("sourceNoData");

      if (!src) {
        noDataEl.style.display = "flex";
        sourceChart.data.datasets[0].data = [0, 0, 0];
        sourceChart.update();
        document.getElementById("sourceTotalLabel").textContent = "No data for this date";
        return;
      }

      noDataEl.style.display = "none";
      sourceChart.data.labels             = src.labels;
      sourceChart.data.datasets[0].data   = src.data;
      sourceChart.update();
      document.getElementById("sourceTotalLabel").textContent = `${src.total} Opportunities`;
    }

    function renderShared(dateKey) {
      renderPipeline(dateKey);
      renderSource(dateKey);
    }

    renderShared(PIPELINE_DATES[0]);
    pipeSel.addEventListener("change", () => renderShared(pipeSel.value));

  </script>
</body>
</html>
"""

# ─── 8. ASSEMBLE AND WRITE ───────────────────────────────────────────────────
# Join all sections into one string and write axis-growth.html.
# Running this script again will overwrite the file with fresh data.

html     = HEAD + HEADER + HERO + SHARED_DATE_HEADER + MGL_CHART + MIDDLE + STAGE_MOVEMENT + META_SECTION + GRANOLA_SECTION + DATA_SCRIPT + CHARTS_SCRIPT
out_path = Path(__file__).parent / "axis-growth.html"
out_path.write_text(html, encoding="utf-8")

print(f"Dashboard written → {out_path}")
print("  Open with:  open axis-growth.html")
