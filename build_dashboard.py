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

def ghl_get(path, params=None, max_retries=3):
    """Make one GET request to the GHL API and return parsed JSON.

    Retries transient failures (429, 5xx, or a body reporting a timeout —
    GHL sometimes wraps its own backend timeouts in a 401) with backoff,
    since these clear up on their own rather than indicating a bad token.
    """
    url_path = path
    if params:
        url_path += "?" + urllib.parse.urlencode(params)
    for attempt in range(max_retries + 1):
        conn = http.client.HTTPSConnection(
            "services.leadconnectorhq.com",
            context=ssl.create_default_context(),
        )
        conn.request("GET", url_path, headers=HEADERS)
        resp = conn.getresponse()
        if resp.status == 200:
            return json.loads(resp.read())
        body = resp.read().decode()
        transient = resp.status == 429 or resp.status >= 500 or "timed out" in body.lower()
        if transient and attempt < max_retries:
            wait = 2 ** attempt
            print(f"  Transient error (HTTP {resp.status}) on {url_path}, retrying in {wait}s...")
            time.sleep(wait)
            continue
        raise Exception(f"HTTP {resp.status} on {url_path}: {body}")


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


# ─── 5b. EXCLUDE "instantly"-TAGGED LEADS ────────────────────────────────────
# Instantly (instantly.ai) is a cold-email tool -- opportunities it creates get
# tagged "instantly" on the contact record (not a distinct source value, and
# not consistently on a dedicated "Instantly" source either -- e.g. some carry
# source "SGL" or no source at all). Operator doesn't want these counted as
# organic new-lead volume, so every "new leads by created date" metric in the
# Marketing & Leads section (This Week/Last Week/WoW, Monthly Volume chart +
# its full-history popout's non-MGL numbers) is computed from leads_opps
# instead of all_opps. Everything else on the dashboard (funnel, MGL score
# buckets, source breakdown, won/pipeline metrics) is untouched.
EXCLUDE_LEAD_TAG = "instantly"

def _has_tag(opp, tag):
    tags = (opp.get("contact") or {}).get("tags") or []
    return any((t or "").strip().lower() == tag for t in tags)

leads_opps = [opp for opp in all_opps if not _has_tag(opp, EXCLUDE_LEAD_TAG)]
print(f"  Excluding {len(all_opps) - len(leads_opps)} opp(s) tagged '{EXCLUDE_LEAD_TAG}' from lead-volume metrics")
print()


# ─── 6. COMPUTE METRICS ──────────────────────────────────────────────────────

print("Computing metrics...")

# ── 6a. Daily new leads — last 14 days ───────────────────────────────────────
# Build 14 date buckets, count how many opportunities have createdAt in each.

date_range        = [day_14_ago + timedelta(days=i) for i in range(14)]
date_keys         = [d.strftime("%Y-%m-%d") for d in date_range]
date_labels_short = [f"{d.strftime('%b')} {d.day}" for d in date_range]

daily_counts = defaultdict(int)
for opp in leads_opps:
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
for opp in leads_opps:
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
    1 for opp in leads_opps
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
    1 for opp in leads_opps
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

month1_count   = _month_count(leads_opps, _m1_year, _m1_month)
month2_count   = _month_count(leads_opps, _m2_year, _m2_month)
cur_month_count = _month_count(leads_opps, _this_year, _this_month)
month1_label   = datetime(_m1_year, _m1_month, 1).strftime("%b")
month2_label   = datetime(_m2_year, _m2_month, 1).strftime("%b")
cur_month_label = today.strftime("%b")

# Month sequence helper for the Monthly Volume "expand" view -- May 2026 is
# the earliest month with real lead data. monthly_full itself (MGL-only, per
# operator request) is built in 6d once mgl_opps exists.
ALL_MONTHS_START = (2026, 5)
def _month_seq(start_year, start_month, end_year, end_month):
    y, m = start_year, start_month
    out = []
    while (y, m) <= (end_year, end_month):
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out

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

# Full month-by-month history for the Monthly Volume "expand" popout -- MGL
# only (per operator request), unlike the compact 3-month chart above it
# which shows all lead volume minus "instantly"-tagged leads. May 2026 is
# the earliest month with real lead data.
monthly_full = [
    {"label": datetime(y, m, 1).strftime("%b"), "count": _month_count(mgl_opps, y, m)}
    for (y, m) in _month_seq(*ALL_MONTHS_START, _this_year, _this_month)
]

# Current-month MGL count/pct for the Monthly Volume badge (matches the chart's
# own timeframe -- was previously a rolling-14-day figure, which didn't line up).
cur_month_mgl     = _month_count(mgl_opps, _this_year, _this_month)
cur_month_mgl_pct = round(cur_month_mgl / cur_month_count * 100) if cur_month_count else 0
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

# Monthly source mix -- lets the Lead Sources card compare how the MGL/SGL/Other
# split evolves month over month (created-date based), instead of only ever
# showing the flat all-time total. "All Time" (the totals above) stays the
# dropdown default so existing behavior/screenshots don't change.
def _created_month_key(opp):
    _c = opp.get("createdAt") or ""
    return _c[:7] if len(_c) >= 7 else None

_source_months = sorted({mk for o in source_opps if (mk := _created_month_key(o))})
source_by_month = {}
for _mk in _source_months:
    _m_opps  = [o for o in source_opps if _created_month_key(o) == _mk]
    _m_mgl   = sum(1 for o in _m_opps if (o.get("source") or "") in MGL_SOURCES)
    _m_sgl   = sum(1 for o in _m_opps if (o.get("source") or "") in SGL_SOURCES)
    _m_other = len(_m_opps) - _m_mgl - _m_sgl
    source_by_month[_mk] = {
        "labels": source_chart_labels,
        "data":   [_m_mgl, _m_sgl, _m_other],
        "total":  len(_m_opps),
    }

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

# ── Won bento (added 2026-08-18) — reuses the SAME Onboarding-stage "Won"
# definition as the Sales Pipeline Funnel card above, for one consistent
# "Won" number across the whole dashboard rather than introducing a third
# one. won_deals_events is the per-deal detail behind that count, for the
# Raw Data page's new "All Time Won" tab.
won_deals_events = []
for opp in onboarding_opps:
    _ts = opp.get("lastStageChangeAt") or opp.get("createdAt")
    if not _ts:
        continue
    _dt = datetime.fromisoformat(_ts.replace("Z", "+00:00"))
    _owner_id = opp.get("assignedTo")
    won_deals_events.append({
        "id":      opp.get("id"),
        "date":    _dt.strftime("%Y-%m-%d"),
        "month":   _dt.strftime("%Y-%m"),
        "opp_name": opp.get("name") or "(unnamed)",
        "contact":  (opp.get("contact") or {}).get("name") or "",
        "value":    float(opp.get("monetaryValue") or 0),
        "owner":    user_map.get(_owner_id, "Unassigned") if _owner_id else "Unassigned",
        "source":   opp.get("source") or "",
    })
won_deals_events.sort(key=lambda e: e["date"], reverse=True)

# blended_cost_per_signing is computed later (6g2), once meta_campaign_spend
# is fetched -- it needs won_onboarding_total, which is already available here.

won_month_chart_labels = [datetime.strptime(mk, "%Y-%m").strftime("%b") for mk in _won_month_keys]
won_month_chart_data   = [_won_by_month[mk] for mk in _won_month_keys]

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


# ─── 6e. Weekly Rocks — shared week grid (still used by Appointments below) ──
# The homepage Weekly Rocks stage card itself no longer uses a week grid (see
# 6e3) -- it reads field_movement_events directly with a date-range picker.
# This week-index machinery is kept because the Appointments mini-table below
# still shows a week-by-week grid (useful for comparisons) and needs the same
# WEEK1_START/_week_index/move_display_weeks it always has.

print("Computing week grid...")

# The reps whose Weekly Rocks are tracked individually (owner ID -> display name).
# Jennifer left the team (2026-07-24) and is no longer mapped -- her historical
# movements/appointments still show under "All" but not a dedicated tab.
# Cole Lytle added 2026-08-26, joined the team.
ROCK_OWNERS = {
    "mMOdJLXRcIhuzcgsHx3M": "Stormer",
    "NftaswY26aPKq64te7Hn": "Alex",
    "kNU1jv4vrjjoehHNhlne": "Joncarlo",
    "Xeprp2GjJCDQFoJV4jqS": "Cole",
}

WEEK1_START = datetime(2026, 7, 1, tzinfo=timezone.utc).date()

def _week_index(date_obj):
    return (date_obj - WEEK1_START).days // 7 + 1

_current_week_idx  = _week_index(today.date())
# Every week from W1 through the current (in-progress) week -- previously this
# was derived from which weeks had snapshot-diff movement data; now that the
# homepage stage table no longer uses a week grid at all, Appointments (the
# only remaining consumer) just gets the full range.
move_display_weeks = list(range(1, _current_week_idx + 1))

def _week_range_str(wk):
    _start = WEEK1_START + timedelta(days=(wk - 1) * 7)
    _end   = _start + timedelta(days=6)
    return f"{_start.strftime('%b %-d')}–{_end.strftime('%-d')}"

move_display_labels = [
    f"W{wk}" + (" *" if wk == _current_week_idx else "")
    for wk in move_display_weeks
]

print(f"  {len(move_display_weeks)} week(s) through W{_current_week_idx}")
print()

# ─── 6e2. Field-based movement events — the source of truth for Weekly Rocks ─
# GHL workflows ("Date Entered - <Stage>") write the actual stage-entry date
# directly onto each opportunity via 6 DATE custom fields -- fixed and
# validated live 2026-07-27. These fields hold the true entry date GHL
# recorded at the moment the opportunity hit that stage: no day-over-day
# snapshot diffing, no missed same-day transitions, no misdated historical
# entries. This is the only movement data source now -- the old snapshot-diff
# approach (stage-snap-*.json day-over-day comparison) has been retired.
FIELD_DATE_STAGES = {
    "YOUfzDu5jq9T3EpsdtgL": "New Lead",
    "fb5FWif6GUyl4c3E60bR": "Discovery Call",
    "aVXVp6kynBp7taut7l3Z": "Strategy Call",
    "hiccRLHd3sqdrYPErOkc": "Proposal Sent",
    "NJjKKBDgxFzoIEhkfFbq": "Agreement Sent",
    "Qru4091H66VTPvH2rQKO": "Agreement Signed",
}
FIELD_STAGE_LABELS = list(FIELD_DATE_STAGES.values())

field_movement_events = []
_future_dated_skipped = 0
for opp in all_opps:
    for cf in (opp.get("customFields") or []):
        _stage_name = FIELD_DATE_STAGES.get(cf.get("id"))
        _ms = cf.get("fieldValueDate")
        if not _stage_name or not _ms:
            continue
        _fd = datetime.fromtimestamp(_ms / 1000, tz=timezone.utc).date()
        if _fd < WEEK1_START:
            continue
        # Date-Entered fields record when a stage was ACTUALLY hit, so a value
        # after today is always a data-entry mistake (e.g. wrong year picked),
        # not a real future stage-entry -- exclude it rather than let it spawn
        # a bogus future month in Funnel Conversion / Weekly Rocks. Confirmed
        # 2026-08-18 after finding a 2027-08-12 entry that was clearly wrong.
        if _fd > today.date():
            _future_dated_skipped += 1
            continue
        _owner_id = opp.get("assignedTo")
        field_movement_events.append({
            "date":       _fd.strftime("%Y-%m-%d"),
            "week":       f"W{_week_index(_fd)}",
            "stage":      _stage_name,
            "opp_name":   opp.get("name") or "(unnamed)",
            "contact":    (opp.get("contact") or {}).get("name") or "",
            "owner":      user_map.get(_owner_id, "Unassigned") if _owner_id else "Unassigned",
            "rock_owner": ROCK_OWNERS.get(_owner_id, ""),  # Stormer/Alex/Joncarlo/Cole, or "" if untracked/unassigned
            "source":     opp.get("source") or "",
        })

field_movement_events.sort(key=lambda e: e["date"], reverse=True)
print(f"  {len(field_movement_events)} field-based movement events (from opportunity date fields)")
if _future_dated_skipped:
    print(f"  ⚠ {_future_dated_skipped} skipped for a Date-Entered value after today (likely data-entry error) -- check these in GHL")
print()

# ─── 6e3. Homepage Weekly Rocks — date-range filterable, no week grid ────────
# The homepage card shows a simple Stage x Count table over an operator-chosen
# date range (default: since Jul 1 to today), filterable by owner. All
# filtering happens client-side in JS (see rocksRender() in CHARTS_SCRIPT) so
# the range can change without a rebuild -- this just ships the raw events.
ROCKS_STAGE_LABELS = ["New Lead", "Discovery Call", "Strategy Call", "Proposal Sent", "Agreement Signed"]

rocks_events = [
    {"date": e["date"], "stage": e["stage"], "owner": e["rock_owner"]}
    for e in field_movement_events
    if e["stage"] in ROCKS_STAGE_LABELS
]
print(f"  {len(rocks_events)} rock entries across {len(ROCKS_STAGE_LABELS)} tracked stages")
print()

# ─── 6e4. Funnel Conversion — monthly stage-entry counts, ecommerce-style ────
# Like a "logins vs. purchases" web funnel: each stage is tallied
# independently for the month its own Date-Entered field falls in -- an
# Agreement Signed event counts in whatever month it was SIGNED, whether or
# not that same opp has a New Lead date on file. No per-opportunity join to
# New Lead is made or required, unlike an earlier version of this section --
# confirmed 2026-08-18 that a join undercounts badly, since most opps that
# reach late stages (e.g. 11 of 14 all-time Agreement Signed) have no New
# Lead date at all: it's normal for that field to be blank since it's only
# been recorded since Jul 27, so many in-flight or pre-existing deals never
# picked one up. Percent = that stage's count divided by New Lead's count in
# the SAME month, same spirit as ROCKS_STAGE_LABELS/rocks_events (6e3) just
# bucketed monthly with a % column added.
#
# Floor is Jul 27, 2026 -- when the Date-Entered custom fields were validated
# live (see 6e2) -- not WEEK1_START (Jul 1), which Rocks/Appointments use for
# an unrelated reason. In practice no field data exists before Jul 27 anyway.
#
# Caveat baked into the note text below: because deals take real time to
# move stage to stage, a month's later-stage count is mostly filled by leads
# that entered in EARLIER months, not that month's own New Leads -- so this
# is a same-period activity ratio, not a true this-cohort-converted rate.
# That's expected and mirrors the earlier weekly-rocks-vs-cohort mismatch
# the operator flagged (9 Aug signings counted here vs. 2 under the retired
# cohort-join version).
FUNNEL_FLOOR = datetime(2026, 7, 27, tzinfo=timezone.utc).date()

funnel_monthly = defaultdict(lambda: defaultdict(int))
for _e in field_movement_events:
    if _e["stage"] not in ROCKS_STAGE_LABELS:
        continue
    if _e["date"] < FUNNEL_FLOOR.isoformat():
        continue
    _month_key = _e["date"][:7]
    funnel_monthly[_month_key][_e["stage"]] += 1

funnel_months = sorted(funnel_monthly.keys())
print(f"  {len(funnel_months)} month(s) of stage-entry data since {FUNNEL_FLOOR}")
print()

# Transposed layout -- months run left-to-right across the top, stages run
# top-to-bottom down the left, so counts shrink going down a column the way
# an actual funnel reads. All Time column = every month's counts summed.
# Shared by both carousel pages below (6e4 step-over-step, 6e4b vs. New
# Lead) -- built once so both tables stay in sync off the same columns.
_funnel_all_time_counts = defaultdict(int)
for _month in funnel_months:
    for _stage, _count in funnel_monthly[_month].items():
        _funnel_all_time_counts[_stage] += _count

FUNNEL_COLUMNS = [(m, funnel_monthly[m], False) for m in funnel_months] + [("All Time", _funnel_all_time_counts, True)]

def _funnel_table_html(get_denominator, note_html):
    """Renders one carousel page. get_denominator(col_counts, stage_index)
    returns the count each row's % is divided by -- the only thing that
    differs between the two pages."""
    if not funnel_months:
        return '<div class="smv-note" style="text-align:center;padding:32px 0;">No data yet.</div>'

    col_ths = ""
    for month, _counts, is_total in FUNNEL_COLUMNS:
        if is_total:
            col_ths += '<th class="smv-th-total">All Time</th>'
            continue
        _label = datetime.strptime(month, "%Y-%m").strftime("%b %Y")
        if month == _current_month_key:
            _label += ' <span style="color:var(--hero);font-weight:800;">·in progress</span>'
        col_ths += f"<th>{_label}</th>"
    thead = f'<thead><tr><th class="smv-th-stage">Stage</th>{col_ths}</tr></thead>'

    tbody_rows = ""
    for i, stage in enumerate(ROCKS_STAGE_LABELS):
        cells = ""
        for _month, col_counts, is_total in FUNNEL_COLUMNS:
            _val_cls = "smv-val smv-total" if is_total else "smv-val smv-val-pos"
            count = col_counts.get(stage, 0)
            if i == 0:
                cells += f'<td class="{_val_cls}">{count}</td>' if count else f'<td class="smv-val smv-val-zero">—</td>'
                continue
            denom = get_denominator(col_counts, i)
            if count > 0 and denom > 0:
                pct = round(count / denom * 100)
                cells += f'<td class="{_val_cls}">{count} <span style="color:var(--text-mute);font-weight:600;">({pct}%)</span></td>'
            elif count > 0:
                cells += f'<td class="{_val_cls}">{count}</td>'
            else:
                cells += '<td class="smv-val smv-val-zero">—</td>'
        tbody_rows += f'<tr><td class="smv-stage-cell">{stage}</td>{cells}</tr>\n'

    return f'<div class="smv-wrap"><table class="smv-table">{thead}<tbody>{tbody_rows}</tbody></table></div>{note_html}'

def _funnel_matrix_html():
    # Page 1 -- step-over-step: each stage's % is of the stage directly
    # above it (Strategy Call % is of Discovery Call, etc.), not New Lead.
    note = (
        '<div class="smv-note">Each stage counted independently for the month its own Date-Entered field falls in -- '
        "not tied to whether that same opp has a New Lead date on file · % on each row is that stage's count divided "
        "by the stage directly above it, in the same month column (step-over-step conversion), not always over New Lead · "
        "because deals take time to move stage to stage, a month's later-stage count mostly reflects leads that entered "
        "in earlier months, not that month's own New Leads -- read this as period activity, not a per-lead conversion rate · "
        "All Time = every month's counts summed, same step-over-step math</div>"
    )
    return _funnel_table_html(lambda col_counts, i: col_counts.get(ROCKS_STAGE_LABELS[i - 1], 0), note)

def _funnel_vs_new_lead_html():
    # Page 2 -- "Funnel vs New Lead" (added 2026-08-18): same table shape,
    # but every row's % is of that SAME column's New Lead count instead of
    # the row above it -- how likely a New Lead is to end up in each stage.
    # Still an independent-tally ratio per period, not a per-opp cohort
    # join (no New-Lead-date requirement per opp, so it doesn't inherit the
    # missing-New-Lead-date gap found 2026-08-18) -- it'll read noisy on
    # small months and get steadier as more data accumulates day by day.
    note = (
        '<div class="smv-note">Same table as the first page, but every row\'s % is of that SAME column\'s New Lead '
        "count, not the row above it -- how likely a New Lead is to end up reaching each stage · still counted "
        "independently per period (not a per-opportunity join), so it reads noisy on light months and gets steadier "
        "as more data accumulates · All Time = every month's counts summed, same vs.-New-Lead math</div>"
    )
    return _funnel_table_html(lambda col_counts, i: col_counts.get("New Lead", 0), note)

# FUNNEL_CONVERSION_SECTION itself (the f-string using _info_icon) is built
# further down, alongside STAGE_MOVEMENT, since _info_icon isn't defined yet
# at this point in the file -- _funnel_matrix_html() above has everything it
# needs already and is called from there.

# ── 6f. Lead → Discovery conversion (since Jul 1, W1) ────────────────────────
# Numerator is the running total of distinct opportunities that entered
# Discovery Call, read straight from field_movement_events (6e2) -- no week
# bucketing needed, just a count. Denominator is total leads (opportunities
# created) since the same Jul 1 start date -- a flow-based rate, not the
# current-stage-position snapshot the other funnel KPIs use.
total_discovery_since_jul1 = sum(1 for e in field_movement_events if e["stage"] == "Discovery Call")
total_leads_since_jul1 = sum(
    1 for opp in all_opps
    if opp.get("createdAt")
    and datetime.fromisoformat(opp["createdAt"].replace("Z", "+00:00")).date() >= WEEK1_START
)
lead_to_disc_pct = round(total_discovery_since_jul1 / total_leads_since_jul1 * 100) if total_leads_since_jul1 else 0
print(f"  Lead to Discovery: {lead_to_disc_pct}% ({total_discovery_since_jul1} of {total_leads_since_jul1} leads since Jul 1)")
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
    _cpl    = round(_spend / _leads, 2) if _leads else 0
    mktg_daily.append({
        "date": _ds,
        "label": datetime.strptime(_ds, "%Y-%m-%d").strftime("%-m/%-d"),
        "spend": round(_spend, 2),
        "clicks": _clicks,
        "cpc": _cpc,
        "leads": _leads,
        "conv_pct": _conv,
        "cpl": _cpl,
    })

mktg_min_date = mktg_daily[0]["date"]
mktg_max_date = mktg_daily[-1]["date"]

# ── MGL CPL hero: computed client-side from MKTG_DAILY (see setCplPeriod JS) ──
# so it can be toggled between Last 7 / 30 / 90 Days without a rebuild.
_cpl_7d_leads = sum(mgl_by_date.get(d["date"], 0) for d in mktg_daily[-7:])
_cpl_7d_spend = sum(d["spend"] for d in mktg_daily[-7:])
cpl_lw_str = f"${_cpl_7d_spend / _cpl_7d_leads:.0f}" if _cpl_7d_leads else "—"

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

# ── 6g2. Meta spend since real sales efforts began (Won bento's blended
# cost/signing) ───────────────────────────────────────────────────────────
# The ad account's history goes back to Oct 2024, long before AxisKey's sales
# effort started -- date_preset="maximum" was pulling in ~1.5 years of spend
# that never had a chance to convert, badly inflating "blended cost per
# signing". Floored at Apr 1, 2026 instead: a month before May's first
# signing (incl. the first MGL one), giving spend a fair head start on the
# leads that became those wins.
META_SPEND_FLOOR = "2026-04-01"
_meta_campaign_resp = meta_get(f"/v21.0/{META_ACCT}/insights", {
    "fields": "spend",
    "time_range": json.dumps({"since": META_SPEND_FLOOR, "until": today.strftime("%Y-%m-%d")}),
})
if "error" in _meta_campaign_resp:
    print(f"  Meta campaign spend fetch error: {_meta_campaign_resp['error'].get('message')} — blended cost/signing will show as unavailable")
    meta_campaign_spend = 0
else:
    _meta_campaign_rows = _meta_campaign_resp.get("data", [])
    meta_campaign_spend = float(_meta_campaign_rows[0]["spend"]) if _meta_campaign_rows else 0

blended_cost_per_signing = (meta_campaign_spend / won_onboarding_total) if won_onboarding_total else 0
blended_cost_str = f"${blended_cost_per_signing:,.0f}" if won_onboarding_total else "—"
print(f"  Spend since {META_SPEND_FLOOR}: ${meta_campaign_spend:,.0f} | Blended cost/signing: {blended_cost_str} ({won_onboarding_total} won)")

# Same spend-since-April figure, but divided by MGL-source won deals only --
# still "blended" (uses total spend, not MGL-attributed spend), just a
# narrower denominator. Reuses onboarding_opps (6f) and MGL_SOURCES (6d).
mgl_won_total = sum(1 for opp in onboarding_opps if (opp.get("source") or "") in MGL_SOURCES)
blended_cost_per_signing_mgl = (meta_campaign_spend / mgl_won_total) if mgl_won_total else 0
blended_cost_mgl_str = f"${blended_cost_per_signing_mgl:,.0f}" if mgl_won_total else "—"
_mgl_won_this_month = sum(
    1 for opp in onboarding_opps
    if (opp.get("source") or "") in MGL_SOURCES
    and (opp.get("lastStageChangeAt") or opp.get("createdAt") or "")[:7] == today.strftime("%Y-%m")
)
print(f"  MGL blended cost/signing: {blended_cost_mgl_str} ({mgl_won_total} MGL won all-time, {_mgl_won_this_month} this month)")
print()


# ─── 6h. Appointments Log — every calendar event since Jul 1, W1 ────────────
# /calendars/events requires a calendarId (or userId/groupId) per call -- there
# is no "all calendars" mode -- so we list calendars first, then fetch events
# for each one in the same [WEEK1_START, +90 days] window and merge. The +90
# day lookahead includes appointments already booked for future dates, not
# just ones that already happened.

print("Fetching appointments...")

_appt_start_ms = int(datetime.combine(WEEK1_START, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
_appt_end_ms   = int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp() * 1000)

calendars = ghl_get("/calendars/", {"locationId": LOCATION_ID}).get("calendars", [])
calendar_map = {c["id"]: (c.get("name") or c["id"]).strip() for c in calendars}

# Discovery vs. Strategy Call classification comes from the calendar's GROUP,
# not its name -- calendar names repeat across groups (e.g. two different
# calendars both called "Alex Zinny Personal Calendar" exist, one in the
# "Discovery" group and one in the newer "Strategy Call" group), so name
# matching alone would misclassify. The "Strategy Call" group was created
# 2026-07-23; any calendar not in a group whose name contains "strategy"
# defaults to Discovery Call, which correctly buckets all pre-7/23 history
# (no separate strategy calendars existed yet).
_calendar_groups = ghl_get("/calendars/groups", {"locationId": LOCATION_ID}).get("groups", [])
_group_name_map = {g["id"]: g.get("name", "") for g in _calendar_groups}
calendar_type_map = {
    c["id"]: ("Strategy Call" if "strategy" in _group_name_map.get(c.get("groupId"), "").lower() else "Discovery Call")
    for c in calendars
}

# Contact names resolved from the opportunities pull (no per-contact API calls
# needed -- every appointment in this business is tied to a pipeline contact).
contact_name_by_id = {
    opp["contactId"]: (opp.get("contact") or {}).get("name")
    for opp in all_opps if opp.get("contactId") and (opp.get("contact") or {}).get("name")
}

# Weekly appointment counts for the Weekly Rocks — Appointments mini-table
# (wk -> {label: count} shape, consumed by _build_weekly_matrix), split into
# Discovery Calls / Strategy Calls by the calendar's group (see
# calendar_type_map above). Capped to move_display_weeks/current week for a
# consistent set of columns; the full set including future-booked
# appointments lives on the Raw Data page.
APPT_TYPE_LABELS = ["Discovery Calls", "Strategy Calls"]
weekly_appts = defaultdict(lambda: {lbl: 0 for lbl in APPT_TYPE_LABELS})
weekly_appts_by_owner = {
    name: defaultdict(lambda: {lbl: 0 for lbl in APPT_TYPE_LABELS})
    for name in ROCK_OWNERS.values()
}

appointment_events = []
_appt_owner_ids = {}
for cid in calendar_map:
    _evs = ghl_get("/calendars/events", {
        "locationId": LOCATION_ID,
        "calendarId": cid,
        "startTime":  _appt_start_ms,
        "endTime":    _appt_end_ms,
    }).get("events", [])
    for ev in _evs:
        if ev.get("deleted"):
            continue
        _start = datetime.fromisoformat(ev["startTime"])
        if _start.date() < WEEK1_START:
            continue
        _owner_id  = ev.get("assignedUserId")
        _appt_type = calendar_type_map.get(cid, "Discovery Call")
        _rec = {
            "date":       _start.strftime("%Y-%m-%d"),
            "time":       _start.strftime("%-I:%M %p"),
            "week":       f"W{_week_index(_start.date())}",
            "calendar":   calendar_map.get(cid, cid),
            "type":       _appt_type,
            "contact":    contact_name_by_id.get(ev.get("contactId"), ev.get("title", "")),
            "owner":      user_map.get(_owner_id, "Unassigned") if _owner_id else "Unassigned",
            "status":     ev.get("appointmentStatus", ""),
            "booked_on":  ev.get("dateAdded", "")[:10],
        }
        appointment_events.append(_rec)
        _appt_owner_ids[id(_rec)] = _owner_id

# De-dupe mirrored bookings: some reps' personal calendars carry a second copy
# of a meeting already booked on the shared "AxisKey — Discovery/Strategy Call"
# calendar (e.g. "AxisKey - David Richert <> Stormer Santana" on Alex Zinny's
# Personal Calendar duplicating "David Richert" on AxisKey — Discovery Call,
# same date/time) -- found by spot-checking W4, confirmed 2026-07-30. Two
# events are only treated as the same meeting when they share date+time AND
# one contact/title is a substring of the other's; same-time slots with two
# different contacts are real, separate appointments and must not be merged.
# The shared-calendar copy is kept over the personal-calendar mirror.
def _appt_names_match(a, b):
    a, b = a.lower().strip(), b.lower().strip()
    return bool(a) and bool(b) and (a in b or b in a)

_deduped_appts = []
for ev in sorted(appointment_events, key=lambda e: "personal calendar" in e["calendar"].lower()):
    if any(
        other["date"] == ev["date"] and other["time"] == ev["time"]
        and _appt_names_match(ev["contact"], other["contact"])
        for other in _deduped_appts
    ):
        continue
    _deduped_appts.append(ev)
appointment_events = _deduped_appts

for ev in appointment_events:
    _wk_ap = int(ev["week"][1:])
    _appt_type_label = "Strategy Calls" if ev["type"] == "Strategy Call" else "Discovery Calls"
    if _wk_ap in move_display_weeks:
        weekly_appts[_wk_ap][_appt_type_label] += 1
        _appt_owner_name = ROCK_OWNERS.get(_appt_owner_ids.get(id(ev)))
        if _appt_owner_name:
            weekly_appts_by_owner[_appt_owner_name][_wk_ap][_appt_type_label] += 1

appointment_events.sort(key=lambda e: (e["date"], e["time"]), reverse=True)
print(f"  {len(appointment_events)} appointments across {len(calendar_map)} calendars")
print()


# ─── 6i. Whiteboard Monthly Aggregation ──────────────────────────────────────
# Powers kpi_whiteboard.html, now generated live here instead of hand-
# maintained. Reuses the same month sequence as the Monthly Volume "expand"
# view (_month_seq/_month_count/ALL_MONTHS_START, section 6a) so May 2026
# stays the single source of truth for "the first real month" everywhere on
# the dashboard, and reuses won_deals_events (6f) rather than re-deriving
# won/MGL-won counts from scratch.
_wb_month_seq = _month_seq(*ALL_MONTHS_START, _this_year, _this_month)
whiteboard_month_keys   = [f"{y:04d}-{m:02d}" for (y, m) in _wb_month_seq]
whiteboard_month_labels = {f"{y:04d}-{m:02d}": datetime(y, m, 1).strftime("%b") for (y, m) in _wb_month_seq}
whiteboard_current_month_key = f"{_this_year:04d}-{_this_month:02d}"

# "Leads" here means the same thing as the Monthly Volume metric elsewhere on
# this dashboard: opportunities created that month across ALL pipelines, not
# Sales-Pipeline-only -- verified live 2026-08-18 against the whiteboard's
# previously hand-typed May/June figures (56/87), which matched this
# definition exactly (July was off by a few, just ordinary data drift since
# the last manual refresh, not a wrong definition).
whiteboard_leads_by_month = {
    f"{y:04d}-{m:02d}": _month_count(all_opps, y, m) for (y, m) in _wb_month_seq
}

whiteboard_won_by_month = defaultdict(int)
whiteboard_mgl_won_by_month = defaultdict(int)
for _e in won_deals_events:
    whiteboard_won_by_month[_e["month"]] += 1
    if _e["source"] in MGL_SOURCES:
        whiteboard_mgl_won_by_month[_e["month"]] += 1

# One Meta call, monthly time_increment, same spend floor as the Won bento's
# blended cost/signing tiles (6g2) -- verified live 2026-08-18 that this
# lands cleanly on calendar-month boundaries for this account, and that
# May/June/July exactly match the whiteboard's previously hand-typed spend
# figures ($1,383.09 / $5,695.72 / $7,588.77).
_wb_meta_resp = meta_get(f"/v21.0/{META_ACCT}/insights", {
    "fields": "spend",
    "time_range": json.dumps({"since": META_SPEND_FLOOR, "until": today.strftime("%Y-%m-%d")}),
    "time_increment": "monthly",
})
whiteboard_meta_by_month = {}
if "error" in _wb_meta_resp:
    print(f"  Whiteboard monthly Meta spend fetch error: {_wb_meta_resp['error'].get('message')}")
else:
    for _row in _wb_meta_resp.get("data", []):
        whiteboard_meta_by_month[_row["date_start"][:7]] = float(_row["spend"])

# Agreement Signed Date + Capital Raiser Intent -- live GHL custom fields on
# the opportunity. Replaces kpi_whiteboard.html's old browser-editable/
# localStorage override -- edit these directly on the opportunity in GHL now,
# same as every other manually-influenced field elsewhere on this dashboard
# (e.g. MGL quality score). Added onto won_deals_events (6f) in place, which
# also flows through unchanged into the Raw Data page's All Time Won export.
#
# Capital Raiser Intent's value key differs by endpoint -- confirmed live
# 2026-08-18 that GET /opportunities/{id} (used for a one-off spot check)
# returns it as cf["fieldValue"], but /opportunities/search (what this
# script actually fetches all_opps/onboarding_opps from) returns the SAME
# field as cf["fieldValueArray"] instead. Checking both, in that order,
# covers whichever shape shows up.
AGREEMENT_SIGNED_FIELD      = "Qru4091H66VTPvH2rQKO"
CAPITAL_RAISER_INTENT_FIELD = "FNFagZQq9ZPZACmA8aHc"
_onboarding_by_id = {opp.get("id"): opp for opp in onboarding_opps}
for _e in won_deals_events:
    _e["agreement_signed"] = None
    _e["capital_raiser_intent"] = None
    _opp = _onboarding_by_id.get(_e["id"])
    if not _opp:
        continue
    for _cf in (_opp.get("customFields") or []):
        if _cf.get("id") == AGREEMENT_SIGNED_FIELD and _cf.get("fieldValueDate"):
            _e["agreement_signed"] = datetime.fromtimestamp(
                _cf["fieldValueDate"] / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d")
        elif _cf.get("id") == CAPITAL_RAISER_INTENT_FIELD:
            _cri = _cf.get("fieldValueArray") or _cf.get("fieldValue")
            if _cri:
                _e["capital_raiser_intent"] = ", ".join(_cri)

# kpi_whiteboard.html's WON schema uses date_won/name (its original
# hand-curated field names) rather than won_deals_events' date/opp_name
# (matched to the Raw Data page's schema instead) -- remap here rather than
# renaming won_deals_events itself, which the Raw Data page also reads.
whiteboard_won = [
    {
        "id": e["id"], "date_won": e["date"], "name": e["opp_name"],
        "value": e["value"], "owner": e["owner"], "source": e["source"],
        "agreement_signed": e["agreement_signed"], "capital_raiser_intent": e["capital_raiser_intent"],
    }
    for e in won_deals_events
]

print(f"  Whiteboard: {len(whiteboard_month_keys)} months ({whiteboard_month_keys[0]}–{whiteboard_month_keys[-1]})")
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

    /* Section groups -- big vertical category rail sitting outside the card
       boxes, one per group of related cards (added 2026-08-18 so categories
       read clearly at a glance instead of relying on each card's own small
       card-label). */
    .section-group { display: flex; gap: 20px; align-items: stretch; }
    .section-group + .section-group { margin-top: 40px; }
    .section-rail { flex: 0 0 auto; width: 34px; display: flex; align-items: center; justify-content: center; }
    .section-rail span {
      writing-mode: vertical-rl;
      transform: rotate(180deg);
      font-size: 1.05rem;
      font-weight: 800;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--hero);
      white-space: nowrap;
    }
    .section-group-body { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; }

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

    /* Won bento -- merged into the Won Deals by Month card, whole section
       links to kpi_whiteboard.html */
    .bento-link { display: block; text-decoration: none; color: inherit; }
    .bento-link .bento-wrap { transition: box-shadow 0.15s ease; border-radius: 14px; }
    .bento-link:hover .bento-wrap { box-shadow: 0 0 0 1.5px var(--hero); }
    .bento-grid-3 { display: grid; grid-template-columns: 1.3fr 1fr 0.9fr; gap: 16px; align-items: stretch; }
    .bento-panel { background: var(--surface-2); border-radius: 12px; padding: 14px 20px; }
    .bento-tiles-col { display: flex; flex-direction: column; gap: 14px; }
    .bento-tile { background: var(--surface-2); border-radius: 12px; padding: 14px 20px; display: flex; flex-direction: column; justify-content: center; flex: 1; }
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
    .prev-quote-row {
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
    }
    .prev-quote-row:last-child { border-bottom: none; }
    .prev-quote-text {
      font-size: 0.85rem;
      font-weight: 500;
      line-height: 1.5;
      color: var(--text);
      margin-bottom: 4px;
    }
    .prev-quote-source {
      font-size: 0.64rem;
      color: var(--text-mute);
      letter-spacing: 0.06em;
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

    /* Glossary button + modal */
    .glossary-btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--surface-2);
      border: 1px solid var(--line);
      color: var(--text-mute);
      border-radius: 8px;
      padding: 6px 12px;
      font-family: inherit;
      font-size: 0.68rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      cursor: pointer;
    }
    .glossary-btn:hover { color: var(--hero); border-color: var(--hero); }
    #glossaryOverlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.65);
      z-index: 200;
      align-items: center;
      justify-content: center;
      padding: 40px;
    }
    #glossaryOverlay.open { display: flex; }
    #monthlyOverlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.65);
      z-index: 200;
      align-items: center;
      justify-content: center;
      padding: 40px;
    }
    #monthlyOverlay.open { display: flex; }
    .expand-icon-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 16px;
      height: 16px;
      border-radius: 4px;
      background: var(--surface-2);
      border: 1px solid var(--line);
      color: var(--text-mute);
      font-size: 0.62rem;
      line-height: 1;
      cursor: pointer;
      padding: 0;
    }
    .expand-icon-btn:hover { color: var(--hero); border-color: var(--hero); }
    .glossary-modal {
      background: var(--surface);
      border-radius: 18px;
      padding: 30px 34px;
      max-width: 640px;
      width: 100%;
      max-height: 82vh;
      overflow-y: auto;
      box-shadow: 0 24px 70px rgba(0,0,0,0.6);
    }
    .glossary-modal-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 6px;
    }
    .glossary-modal-title {
      font-size: 1.1rem;
      font-weight: 800;
      color: var(--text);
    }
    .glossary-close {
      background: none;
      border: none;
      color: var(--text-mute);
      font-size: 1.3rem;
      line-height: 1;
      cursor: pointer;
      padding: 4px 8px;
    }
    .glossary-close:hover { color: var(--text); }
    .glossary-sub {
      font-size: 0.68rem;
      color: var(--text-mute);
      margin-bottom: 18px;
    }
    .glossary-term { padding: 14px 0; border-bottom: 1px solid var(--line); }
    .glossary-term:last-child { border-bottom: none; padding-bottom: 0; }
    .glossary-term-label {
      font-size: 0.85rem;
      font-weight: 700;
      color: var(--hero);
      margin-bottom: 4px;
    }
    .glossary-term-def {
      font-size: 0.76rem;
      color: var(--text);
      line-height: 1.55;
    }

    /* Inline info-icon tooltips on a few key metrics */
    .info-icon {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: var(--surface-2);
      border: 1px solid var(--line);
      color: var(--text-mute);
      font-size: 0.58rem;
      font-weight: 700;
      font-style: normal;
      cursor: help;
      margin-left: 5px;
      position: relative;
      vertical-align: middle;
    }
    .info-icon .tooltip-box {
      display: none;
      position: absolute;
      bottom: 135%;
      left: 50%;
      transform: translateX(-50%);
      background: #0F0F14;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 12px;
      width: 220px;
      font-size: 0.68rem;
      font-weight: 400;
      text-transform: none;
      letter-spacing: normal;
      color: var(--text);
      line-height: 1.45;
      z-index: 60;
      box-shadow: 0 10px 28px rgba(0,0,0,0.55);
    }
    .info-icon:hover .tooltip-box { display: block; }
  </style>
</head>
<body>
"""

# ── 7b. Header (f-string — embeds generated_at) ──────────────────────────────
def _info_icon(text):
    """Small inline (i) icon with a hover tooltip -- used on a handful of the
    most commonly-misread metrics. Full definitions for every metric live in
    the Glossary modal (see GLOSSARY_TERMS)."""
    return f'<span class="info-icon">i<span class="tooltip-box">{text}</span></span>'

HEADER = f"""
  <header class="header">
    <div class="header-left">
      <img src="assets/logo.svg" alt="Axis Growth" class="header-logo">
      <span class="header-title">Axis Growth</span>
    </div>
    <div style="display:flex;align-items:center;gap:16px;">
      <a href="axis-growth-data.html" class="glossary-btn" style="text-decoration:none;">Raw Data →</a>
      <button class="glossary-btn" onclick="document.getElementById('glossaryOverlay').classList.add('open')">? Glossary</button>
      <span class="header-meta">Generated {generated_at}</span>
    </div>
  </header>
"""

# ── 7c. Marketing & Leads hero section ───────────────────────────────────────
HERO = f"""
  <section class="section-hero card">

    <!-- Title row + MGL CPL hero -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:10px;">
      <div class="card-label" style="margin-bottom:0;">Marketing &amp; Leads</div>
      <div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;">
        <div style="display:flex;gap:4px;">
          <button onclick="setCplPeriod(7,this)" class="mktg-btn cpl-period-btn mktg-btn-active">7D</button>
          <button onclick="setCplPeriod(30,this)" class="mktg-btn cpl-period-btn">30D</button>
          <button onclick="setCplPeriod(90,this)" class="mktg-btn cpl-period-btn">90D</button>
        </div>
        <div id="cplHeroValue" style="font-size:2.2rem;font-weight:800;color:var(--hero);line-height:1;">{cpl_lw_str}</div>
        <div style="font-size:0.56rem;font-weight:600;letter-spacing:0.16em;text-transform:uppercase;color:var(--text-mute);">MGL CPL{_info_icon("Meta ad spend divided by MGL-source leads for the selected period. Compared against the immediately preceding period of equal length (e.g. last 7 days vs. the 7 days before that); no comparison is shown for 90 Days since only 90 days of spend history are fetched. Lower is better.")}</div>
        <div id="cplHeroDelta" style="display:none;align-items:center;gap:5px;background:var(--surface-2);border-radius:6px;padding:3px 9px;">
          <span id="cplHeroDeltaVal" style="font-size:0.8rem;font-weight:800;"></span>
          <span style="font-size:0.48rem;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:var(--text-mute);">vs prior</span>
        </div>
        <div id="cplHeroRange" style="font-size:0.58rem;color:var(--text-mute);"></div>
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
          <div style="display:flex;align-items:center;gap:6px;">
            <div style="font-size:0.56rem;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:var(--text-mute);">Monthly Volume{_info_icon("Compact view shows the last 3 months (2 months ago, last month, current month) of all new leads by created date, excluding any lead tagged 'instantly' (Instantly.ai cold-email leads). The expand icon opens a separate popout: MGL-source leads only, every month back to May 2026. The MGL badge is always scoped to the current month, not a rolling window.")}</div>
            <button class="expand-icon-btn" onclick="openMonthlyModal()" title="View full month-by-month history" aria-label="Expand Monthly Volume">⤢</button>
          </div>
          <span class="mgl-pill" style="font-size:0.56rem;padding:2px 7px;white-space:nowrap;">MGL&nbsp;{cur_month_mgl}&nbsp;·&nbsp;{cur_month_mgl_pct}%&nbsp;·&nbsp;{cur_month_label}</span>
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
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
      <div class="card-label" style="margin-bottom:0;">Lead Sources — New Lead &amp; Beyond &nbsp;·&nbsp; <span id="sourceTotalLabel">{total_source_opps} Opportunities</span>{_info_icon("This is essentially all-time, not just currently-open deals: every Sales Pipeline opportunity ever won or lost, plus any currently open at New Lead stage or beyond. Use the month selector to compare how the MGL/SGL/Other mix evolves month over month, by opportunity created date, instead of the flat all-time total.")}</div>
      <select id="sourceMonthSelect" style="background:var(--surface-2);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:4px 12px;font-family:inherit;font-size:0.72rem;font-weight:600;cursor:pointer;outline:none;">
        <option value="">All Time</option>
      </select>
    </div>
    <div style="display:grid;grid-template-columns:3fr 2fr;gap:28px;align-items:stretch;margin-top:16px;">

      <!-- Left: source bar chart — constrained width so bars cluster together -->
      <div style="display:flex;align-items:center;justify-content:center;">
        <div style="position:relative;width:300px;height:100%;">
          <canvas id="chartSource"></canvas>
          <div id="sourceNoData" style="display:none;position:absolute;inset:0;align-items:center;justify-content:center;text-align:center;font-size:0.7rem;color:var(--text-mute);padding:0 20px;">No source data for this period.</div>
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
          <span class="funnel-kpi-label">Lead to Discovery{_info_icon("Since Jul 1, 2026. Numerator is the running total of distinct opportunities that entered Discovery Call, read directly from GHL's 'Date Entered' custom fields (not diffed from snapshots). Denominator is total leads created since the same date. Unlike the other three funnel KPIs (which are all-time, based on current stage position), this one is flow-based and grows over time.")}</span>
          <span class="funnel-kpi-value">{lead_to_disc_pct}%</span>
          <span class="funnel-kpi-sub">{total_discovery_since_jul1} of {total_leads_since_jul1} leads since Jul 1</span>
        </div>
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
          <span class="funnel-kpi-label">Won{_info_icon("Not GHL's won/lost status field -- this counts opportunities currently sitting in the Onboarding stage, which is how this pipeline defines a closed-won deal.")}</span>
          <span class="funnel-kpi-value">{won_onboarding_total}</span>
          <span class="funnel-kpi-sub">currently in Onboarding</span>
        </div>
      </div>

      <a href="kpi_whiteboard.html" class="bento-link" title="Open the full Spend Efficiency Whiteboard">
        <div class="bento-wrap" style="margin-top:22px;padding-top:20px;border-top:1px solid var(--line);">
          <div class="card-label" style="margin-bottom:4px;">Won Deals — Entered Onboarding by Month{_info_icon("Same 'Won' definition as the KPI chip above -- opportunities currently sitting in the Onboarding stage, not GHL's own won/lost status field. All Time Won, Blended Cost/Signing, and Blended Cost/Signing (MGL) are live from the GHL + Meta APIs. Click anywhere in this section to open the full Spend Efficiency Whiteboard for month-by-month detail, projections, and per-deal notes.")}</div>
          <div style="font-size:0.68rem;color:var(--text-mute);margin-bottom:14px;">Based on lastStageChangeAt · month-over-month vs. prior completed month</div>
          <div class="bento-grid-3">
            <div class="bento-panel">
              {_won_month_rows if _won_month_rows else '<div class="comp-none">None recorded yet.</div>'}
            </div>
            <div class="bento-panel">
              <span class="funnel-kpi-label">Signings by Month</span>
              <div class="chart-wrap" style="height:150px;margin-top:8px;">
                <canvas id="chartWonMonthly"></canvas>
              </div>
            </div>
            <div class="bento-tiles-col">
              <div class="bento-tile">
                <span class="funnel-kpi-label">All Time Won</span>
                <span class="funnel-kpi-value">{won_onboarding_total}</span>
                <span class="funnel-kpi-sub">currently in Onboarding</span>
              </div>
              <div class="bento-tile">
                <span class="funnel-kpi-label">Blended Cost / Signing{_info_icon("Meta spend since Apr 1, 2026 (a month before May's first signing, giving spend a fair head start on the leads that became those wins) -- not the ad account's full history, which goes back to Oct 2024, long before AxisKey's sales effort started and would badly inflate this number.")}</span>
                <span class="funnel-kpi-value">{blended_cost_str}</span>
                <span class="funnel-kpi-sub">${meta_campaign_spend:,.0f} spend (since Apr 2026) ÷ {won_onboarding_total} won</span>
              </div>
              <div class="bento-tile">
                <span class="funnel-kpi-label">Blended Cost / Signing (MGL){_info_icon("Same Meta spend since Apr 1, 2026 as Blended Cost/Signing, divided by MGL-source won deals only instead of all won deals -- still 'blended' since it uses total ad spend, not MGL-attributed spend.")}</span>
                <span class="funnel-kpi-value">{blended_cost_mgl_str}</span>
                <span class="funnel-kpi-sub">${meta_campaign_spend:,.0f} spend (since Apr 2026) ÷ {mgl_won_total} MGL won</span>
              </div>
            </div>
          </div>
        </div>
      </a>
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
# Quote of the week is replaced each week -- but the outgoing quote is archived
# into previous_quotes (most recent first) rather than discarded.
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

def _prev_quote_rows(quotes):
    return "\n        ".join(
        f'<div class="prev-quote-row">'
        f'<div class="prev-quote-text">"{q["text"]}"</div>'
        f'<div class="prev-quote-source">{q["source"]}</div>'
        f'</div>'
        for q in quotes
    ) if quotes else '<div class="comp-none">None archived yet.</div>'

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

      <div class="g-card" style="grid-column: 1 / -1;">
        <div class="g-card-label">Previous Highlighted Quotes</div>
        {_prev_quote_rows(_gi["previous_quotes"])}
      </div>

    </div>
  </section>
"""

# ── 7h2. Glossary modal — plain-English definition for every metric on the page
GLOSSARY_TERMS = [
    ("This Week / Last Week", "New leads (opportunities created), Monday-Sunday, excluding any lead tagged 'instantly' (Instantly.ai cold-email tool). WoW badge compares this week's count to last week's."),
    ("MGL CPL", "Meta ad spend divided by MGL-source leads for the selected period (7D/30D/90D toggle). The delta badge compares to the immediately preceding period of equal length; hidden for 90D since spend history only goes back 90 days. Lower is better."),
    ("Monthly Volume (bar chart)", "Only the last 3 months (2 months ago, last month, current month) by lead created date, across all pipelines/statuses, excluding leads tagged 'instantly' (Instantly.ai cold-email tool). The MGL badge next to it shows the current month's MGL count and % of that month's total leads shown in this chart (not a rolling window). The expand icon opens a separate full-history popout showing MGL-source leads only, back to May 2026 -- a different slice of data than this compact chart, not the same data zoomed out."),
    ("Daily Performance table (Spend/Clicks/CPC/Leads/Conv%/CPL)", "Meta ad spend, link clicks, cost-per-click, MGL leads, lead conversion rate, and cost per MGL lead, one row per day. Excludes today (spend/clicks are incomplete until the day closes out). Use the date-range picker to view any custom window back to 90 days."),
    ("Lead Sources — MGL / SGL / Other", "Essentially all-time within the Sales Pipeline: every opportunity ever won or lost, plus any currently open at New Lead stage or beyond. MGL = Marketing Generated Lead, SGL = Sales Generated Lead."),
    ("Call Quality (Great Fit / Potential / Poor Fit / Unscored)", "Quality score set on the contact record, shown for opportunities at Discovery Call stage or beyond, broken out by source (MGL/SGL/Other)."),
    ("Pipeline Snapshot — Open Deals by Stage", "Current count of OPEN opportunities in each Sales Pipeline stage, as of the selected daily snapshot (use the Viewing Snapshot dropdown to look at a past date)."),
    ("Lead to Discovery", "Since Jul 1, 2026: total distinct opportunities that entered Discovery Call (read directly from GHL's 'Date Entered' custom fields) divided by total leads created since the same date. Flow-based and grows over time -- unlike the other three funnel KPIs below, which are all-time and based on current stage position."),
    ("Discovery to Proposal / Proposal to Signed / Lead to Won", "All-time conversion rates within the Sales Pipeline: of all opportunities that ever reached the first stage in the pair (any status), what % also reached the second stage (or won, for Lead to Won)."),
    ("Won", "NOT GHL's won/lost status field -- this counts opportunities currently sitting in the Onboarding stage, which is how this pipeline defines a closed-won deal."),
    ("Won Deals — Entered Onboarding by Month", "Based on lastStageChangeAt: which month each currently-Onboarding opportunity most recently moved into that stage. Month-over-month % compares to the prior completed month; the current month is marked in progress and excluded from that comparison. Blended Cost/Signing divides Meta spend since Apr 1, 2026 (not the ad account's full history, which predates AxisKey's sales effort) by all-time won; Blended Cost/Signing (MGL) divides the same spend by MGL-source won deals only."),
    ("Weekly Rocks", "Distinct opportunities that ENTERED each of 5 key stages (New Lead, Discovery Call, Strategy Call, Proposal Sent, Agreement Signed) within the selected date range (defaults to since Jul 1, 2026), read directly from GHL's 'Date Entered' custom fields -- the true stage-entry date GHL recorded, not diffed from daily snapshots. Change the date range to compare any period. Owner filter uses each opportunity's current assigned owner."),
    ("Weekly Rocks — Appointments", "Appointments scheduled per week (any calendar/status), from GHL's own calendar events -- a real log, not diffed from daily snapshots. Bucketed by the appointment's own date into a week grid (W1 = Jul 1, 2026), kept as weeks (rather than a date range like the stage table above) since that's what makes week-over-week comparisons easy. Owner filter uses the appointment's assigned user, not the linked opportunity's owner."),
    ("Meta Campaign Spending (bottom section)", "Last 7 days of Meta ad spend, including today's partial/incomplete spend as its own tile -- unlike the Daily Performance table above, which excludes today."),
    ("Call Intelligence", "Cumulative, all-time data extracted from Granola call notes: fund sizes and competitors ever mentioned by prospects, and the most common discovery-call questions. Quote of the Week is replaced when a new standout quote comes in -- the outgoing quote is archived into Previous Highlighted Quotes rather than discarded. Everything else accumulates and never resets."),
]

def _glossary_rows():
    return "\n".join(
        f'<div class="glossary-term">'
        f'<div class="glossary-term-label">{label}</div>'
        f'<div class="glossary-term-def">{definition}</div>'
        f'</div>'
        for label, definition in GLOSSARY_TERMS
    )

GLOSSARY_MODAL = f"""
  <div id="glossaryOverlay" onclick="if(event.target===this)this.classList.remove('open')">
    <div class="glossary-modal">
      <div class="glossary-modal-header">
        <span class="glossary-modal-title">Dashboard Glossary</span>
        <button class="glossary-close" onclick="document.getElementById('glossaryOverlay').classList.remove('open')">&times;</button>
      </div>
      <div class="glossary-sub">What every metric on this dashboard actually means</div>
      {_glossary_rows()}
    </div>
  </div>
"""

MONTHLY_MODAL = """
  <div id="monthlyOverlay" onclick="if(event.target===this)closeMonthlyModal()">
    <div class="glossary-modal" style="max-width:820px;">
      <div class="glossary-modal-header">
        <span class="glossary-modal-title">MGL Volume — Full History</span>
        <button class="glossary-close" onclick="closeMonthlyModal()">&times;</button>
      </div>
      <div class="glossary-sub">MGL-source leads by month, since May 2026 (by lead created date)</div>
      <div style="position:relative;height:380px;"><canvas id="chartMonthlyFull"></canvas></div>
    </div>
  </div>
"""

# ── 7i. Weekly matrix builder (weeks as columns, Total column) — shared by
# the Weekly Rocks stage table and the Appointments mini-table below it.
def _build_weekly_matrix(movement_dict, row_labels, note_text, empty_text):
    if not movement_dict:
        return f'<div class="smv-note" style="text-align:center;padding:32px 0;">{empty_text}</div>'

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
    for label in row_labels:
        week_tds  = ""
        row_total = 0
        for wk in move_display_weeks:
            val = movement_dict[wk].get(label, 0)
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
        f'<div class="smv-note">{note_text}'
        + (' · * current week in progress, excluded from Total' if _current_week_idx in move_display_weeks else '')
        + '</div>'
    )
    return f'<div class="smv-wrap"><table class="smv-table">{thead}{tbody}</table></div>{note}'

_rocks_default_from = WEEK1_START.isoformat()
_rocks_default_to   = today.date().isoformat()

_rocks_rows_html = "".join(
    f'<tr><td class="smv-stage-cell">{lbl}</td><td class="smv-val smv-total" id="rockCount-{i}">0</td></tr>'
    for i, lbl in enumerate(ROCKS_STAGE_LABELS)
)

STAGE_MOVEMENT = f"""
  <section class="card" style="margin-top:22px;">
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
      <div class="card-label" style="margin-bottom:0;">Weekly Rocks{_info_icon("Distinct opportunities that entered each stage, read directly from GHL's 'Date Entered' custom fields (New Lead / Discovery Call / Strategy Call / Proposal Sent / Agreement Signed) -- the true stage-entry date GHL recorded, not diffed from daily snapshots. Counts reflect the selected date range (default: since Jul 1, 2026). Owner filter uses each opportunity's current assigned owner.")}</div>
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <div style="display:flex;gap:5px;align-items:center;">
          <input type="date" id="rockFrom" value="{_rocks_default_from}" onchange="rocksRender()"
            style="background:var(--surface-2);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-family:inherit;font-size:0.68rem;outline:none;color-scheme:dark;">
          <span style="font-size:0.62rem;color:var(--text-mute);">–</span>
          <input type="date" id="rockTo" value="{_rocks_default_to}" onchange="rocksRender()"
            style="background:var(--surface-2);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-family:inherit;font-size:0.68rem;outline:none;color-scheme:dark;">
        </div>
        <div style="display:flex;gap:4px;">
          <button onclick="setRockOwner('all',this)" class="mktg-btn smv-owner-btn mktg-btn-active">All</button>
          <button onclick="setRockOwner('stormer',this)" class="mktg-btn smv-owner-btn">Stormer</button>
          <button onclick="setRockOwner('alex',this)" class="mktg-btn smv-owner-btn">Alex</button>
          <button onclick="setRockOwner('joncarlo',this)" class="mktg-btn smv-owner-btn">Joncarlo</button>
          <button onclick="setRockOwner('cole',this)" class="mktg-btn smv-owner-btn">Cole</button>
        </div>
      </div>
    </div>
    <div class="smv-wrap" style="margin-top:14px;">
      <table class="smv-table">
        <thead><tr><th class="smv-th-stage">Stage</th><th class="smv-th-total">Count</th></tr></thead>
        <tbody>{_rocks_rows_html}</tbody>
      </table>
    </div>
  </section>
"""

FUNNEL_CONVERSION_SECTION = f"""
  <section class="card" style="margin-top:22px;">
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
      <div class="card-label" style="margin-bottom:0;"><span id="funnelTitleText">Funnel Conversion</span>{_info_icon("Each stage counted independently by the month its own 'Date Entered' field falls in -- like an ecommerce funnel comparing total logins vs. total purchases in a period, not tracking the same individual through every step. An opp counts toward Agreement Signed in whatever month it was signed, whether or not it has a New Lead date on file (most won deals don't -- that field's only been recorded since 2026-07-27, so many in-flight/pre-existing deals never got one). Two carousel pages, shown by the pill below the title: 'Funnel Conversion' -- % on each row is that stage's count divided by the stage directly above it (step-over-step conversion). 'Funnel vs New Lead' -- every row instead divided by that same period's New Lead count, i.e. how likely a New Lead is to reach each stage. Neither is a per-opportunity cohort join, so both read noisy on light months and get steadier as more data accumulates.")}</div>
      <div style="display:flex;align-items:center;gap:8px;">
        <button onclick="funnelPagePrev()" class="mktg-btn smv-owner-btn" style="padding:2px 9px;line-height:1;" aria-label="Previous">‹</button>
        <button onclick="funnelPageNext()" class="mktg-btn smv-owner-btn" style="padding:2px 9px;line-height:1;" aria-label="Next">›</button>
      </div>
    </div>
    <div style="margin-top:12px;">
      <span id="funnelModeBadge" style="display:inline-flex;align-items:center;gap:7px;padding:5px 14px;border-radius:20px;background:var(--hero);color:#101014;border:1.5px solid var(--hero);font-size:0.66rem;font-weight:800;letter-spacing:0.07em;text-transform:uppercase;">● Viewing: vs Previous Stage</span>
    </div>
    <div id="funnelPage-0" style="margin-top:14px;">{_funnel_matrix_html()}</div>
    <div id="funnelPage-1" style="margin-top:14px;display:none;">{_funnel_vs_new_lead_html()}</div>
  </section>
"""

# ── 7i2. Appointments mini-table — same weekly grid, one row, owner filter ──
_APW_NOTE = ('Appointments scheduled that week (any calendar/status) · from GHL calendar events, not snapshot-diffed'
             ' · split by calendar group: the "Strategy Call" group was created 2026-07-23, so everything before that date falls under Discovery Calls')
_APW_EMPTY = 'No appointment data for this period yet.'

_apw_all      = _build_weekly_matrix(weekly_appts, APPT_TYPE_LABELS, _APW_NOTE, _APW_EMPTY)
_apw_stormer  = _build_weekly_matrix(weekly_appts_by_owner["Stormer"], APPT_TYPE_LABELS, _APW_NOTE, _APW_EMPTY)
_apw_alex     = _build_weekly_matrix(weekly_appts_by_owner["Alex"], APPT_TYPE_LABELS, _APW_NOTE, _APW_EMPTY)
_apw_joncarlo = _build_weekly_matrix(weekly_appts_by_owner["Joncarlo"], APPT_TYPE_LABELS, _APW_NOTE, _APW_EMPTY)
_apw_cole     = _build_weekly_matrix(weekly_appts_by_owner["Cole"], APPT_TYPE_LABELS, _APW_NOTE, _APW_EMPTY)

APPT_WEEKLY_SECTION = f"""
  <section class="card" style="margin-top:22px;">
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
      <div class="card-label" style="margin-bottom:0;">Weekly Rocks — Appointments{_info_icon("Appointments scheduled per week, from GHL's own calendar events (a real log, not diffed from daily snapshots). Bucketed by the appointment's own date into a week grid (W1 = Jul 1, 2026) -- kept as weeks rather than a date range so week-over-week comparisons stay easy. Split into Discovery Calls vs. Strategy Calls by the calendar's GHL calendar group (not its name -- some calendars share a name across groups). Owner filter uses the appointment's assigned user. Full detail, including future-booked appointments, is on the Raw Data page.")}</div>
      <div style="display:flex;gap:4px;">
        <button onclick="setApptOwner('all',this)" class="mktg-btn smv-owner-btn2 mktg-btn-active">All</button>
        <button onclick="setApptOwner('stormer',this)" class="mktg-btn smv-owner-btn2">Stormer</button>
        <button onclick="setApptOwner('alex',this)" class="mktg-btn smv-owner-btn2">Alex</button>
        <button onclick="setApptOwner('joncarlo',this)" class="mktg-btn smv-owner-btn2">Joncarlo</button>
        <button onclick="setApptOwner('cole',this)" class="mktg-btn smv-owner-btn2">Cole</button>
      </div>
    </div>
    <div id="apwView-all" style="margin-top:14px;">{_apw_all}</div>
    <div id="apwView-stormer" style="margin-top:14px;display:none;">{_apw_stormer}</div>
    <div id="apwView-alex" style="margin-top:14px;display:none;">{_apw_alex}</div>
    <div id="apwView-joncarlo" style="margin-top:14px;display:none;">{_apw_joncarlo}</div>
    <div id="apwView-cole" style="margin-top:14px;display:none;">{_apw_cole}</div>
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
    const MONTHLY_FULL     = {json.dumps(monthly_full)};
    const META_LABELS     = {json.dumps(meta_labels)};
    const META_SPEND      = {json.dumps(meta_spends)};
    const SOURCE_LABELS      = {json.dumps(source_chart_labels)};
    const SOURCE_DATA        = {json.dumps(source_chart_data)};
    const SOURCE_TOTAL       = {json.dumps(total_source_opps)};
    const SOURCE_BY_MONTH    = {json.dumps(source_by_month)};
    const SOURCE_MONTHS      = {json.dumps(_source_months)};
    const PIPELINE_HISTORY   = {json.dumps(pipeline_history)};
    const PIPELINE_DATES     = {json.dumps(pipeline_dates)};
    const MKTG_DAILY         = {json.dumps(mktg_daily)};
    const ROCKS_EVENTS       = {json.dumps(rocks_events)};
    const ROCKS_STAGE_LABELS = {json.dumps(ROCKS_STAGE_LABELS)};
    const WON_MONTH_LABELS   = {json.dumps(won_month_chart_labels)};
    const WON_MONTH_DATA     = {json.dumps(won_month_chart_data)};
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
      document.querySelectorAll(".mktg-btn:not(.smv-owner-btn):not(.smv-owner-btn2):not(.cpl-period-btn)").forEach(b => b.classList.remove("mktg-btn-active"));
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
      const totCpl    = totLeads  ? totSpend / totLeads  : 0;
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
          ${td(r.cpl > 0 ? fC(r.cpl) : "—", r.cpl > 0 ? "var(--hero)" : "var(--text-mute)")}
        </tr>`;
      });
      const foot = `<tr style="border-top:1px solid var(--line);">
        <td style="padding:5px 5px 3px 0;color:var(--text-mute);font-size:0.72rem;font-weight:700;">Total</td>
        ${td(fS(totSpend), undefined, 700)}
        ${td(totClicks || "—", totClicks ? undefined : "var(--text-mute)", 700)}
        ${td(totClicks ? fC(totCpc) : "—", totClicks ? undefined : "var(--text-mute)", 700)}
        ${td(totLeads > 0 ? totLeads : "—", totLeads > 0 ? "var(--hero)" : "var(--text-mute)", 700)}
        ${td(totConv > 0 ? totConv.toFixed(1) + "%" : "—", totConv > 0 ? undefined : "var(--text-mute)", 700)}
        ${td(totCpl > 0 ? fC(totCpl) : "—", totCpl > 0 ? "var(--hero)" : "var(--text-mute)", 700)}
      </tr>`;
      document.getElementById("mktgTable").innerHTML =
        `<table style="width:100%;border-collapse:collapse;">
          <thead><tr style="border-bottom:1px solid var(--line);">
            ${th("Date","left")}${th("Spend")}${th("Clicks")}${th("CPC")}${th("Leads")}${th("Conv%")}${th("CPL")}
          </tr></thead>
          <tbody>${body}</tbody>
          <tfoot>${foot}</tfoot>
        </table>`;
    }
    function setMktgPeriod(days, btn) {
      document.querySelectorAll(".mktg-btn:not(.smv-owner-btn):not(.smv-owner-btn2):not(.cpl-period-btn)").forEach(b => b.classList.remove("mktg-btn-active"));
      btn.classList.add("mktg-btn-active");
      renderMktgTable(days);
    }
    renderMktgTable(7);

    // ── Marketing & Leads — MGL CPL hero (segmented 7D / 30D / 90D) ──────
    function setCplPeriod(days, btn) {
      document.querySelectorAll(".cpl-period-btn").forEach(b => b.classList.remove("mktg-btn-active"));
      btn.classList.add("mktg-btn-active");
      renderCplHero(days);
    }
    function renderCplHero(days) {
      const cur  = MKTG_DAILY.slice(-days);
      const prev = MKTG_DAILY.slice(0, MKTG_DAILY.length - days).slice(-days);
      const sum  = (rows, k) => rows.reduce((s, r) => s + r[k], 0);
      const curLeads = sum(cur, "leads");
      const curCpl   = curLeads ? sum(cur, "spend") / curLeads : null;

      document.getElementById("cplHeroValue").textContent = curCpl != null ? `$${curCpl.toFixed(0)}` : "—";

      const fmtD = ds => new Date(ds + "T00:00:00").toLocaleDateString("en-US", {month: "short", day: "numeric"});
      document.getElementById("cplHeroRange").textContent =
        cur.length ? `${fmtD(cur[0].date)} – ${fmtD(cur[cur.length - 1].date)}` : "";

      const deltaEl = document.getElementById("cplHeroDelta");
      const prevLeads = prev.length === days ? sum(prev, "leads") : 0;
      const prevCpl   = prevLeads ? sum(prev, "spend") / prevLeads : null;
      if (curCpl != null && prevCpl) {
        const delta = Math.round((curCpl - prevCpl) / prevCpl * 100);
        const dir   = delta > 0 ? "↑" : (delta < 0 ? "↓" : "→");
        const color = delta > 0 ? "#FF5C5C" : "var(--hero)";  // lower CPL = better
        const valEl = document.getElementById("cplHeroDeltaVal");
        valEl.textContent = `${dir} ${delta >= 0 ? "+" : ""}${delta}%`;
        valEl.style.color = color;
        deltaEl.style.display = "inline-flex";
      } else {
        deltaEl.style.display = "none";
      }
    }
    renderCplHero(7);

    // ── Weekly Rocks — date range + owner filter (field-based, no week grid) ──
    let rockOwnerFilter = "all";
    const ROCK_OWNER_NAMES = { stormer: "Stormer", alex: "Alex", joncarlo: "Joncarlo", cole: "Cole" };

    function setRockOwner(which, btn) {
      rockOwnerFilter = which;
      document.querySelectorAll(".smv-owner-btn").forEach(b => b.classList.remove("mktg-btn-active"));
      btn.classList.add("mktg-btn-active");
      rocksRender();
    }

    function rocksRender() {
      const from = document.getElementById("rockFrom").value;
      const to   = document.getElementById("rockTo").value;
      const wantOwner = ROCK_OWNER_NAMES[rockOwnerFilter] || null;

      const counts = {};
      ROCKS_STAGE_LABELS.forEach(s => counts[s] = 0);
      ROCKS_EVENTS.forEach(e => {
        if (from && e.date < from) return;
        if (to   && e.date > to)   return;
        if (wantOwner && e.owner !== wantOwner) return;
        if (counts[e.stage] !== undefined) counts[e.stage]++;
      });
      ROCKS_STAGE_LABELS.forEach((s, i) => {
        document.getElementById("rockCount-" + i).textContent = counts[s];
      });
    }
    rocksRender();

    // ── Funnel Conversion — 2-page carousel ────────────────────────────────
    // Title text and the mode badge both swap on page change (2026-08-18:
    // previously only a small indicator text changed, which the operator
    // found too easy to miss) -- filled lime pill = vs Previous Stage,
    // outlined lime pill = vs New Lead, so the two modes are visually
    // unmistakable even at a glance, not just by reading text.
    let funnelPageIdx = 0;
    const FUNNEL_TITLES = ["Funnel Conversion", "Funnel vs New Lead"];
    const FUNNEL_BADGE_TEXT = ["● Viewing: vs Previous Stage", "● Viewing: vs New Lead"];
    function funnelShowPage(idx) {
      funnelPageIdx = idx;
      document.getElementById("funnelPage-0").style.display = idx === 0 ? "" : "none";
      document.getElementById("funnelPage-1").style.display = idx === 1 ? "" : "none";
      document.getElementById("funnelTitleText").textContent = FUNNEL_TITLES[idx];
      const badge = document.getElementById("funnelModeBadge");
      badge.textContent = FUNNEL_BADGE_TEXT[idx];
      badge.style.background = idx === 0 ? "var(--hero)" : "transparent";
      badge.style.color = idx === 0 ? "#101014" : "var(--hero)";
    }
    function funnelPagePrev() { funnelShowPage(funnelPageIdx === 0 ? 1 : 0); }
    function funnelPageNext() { funnelShowPage(funnelPageIdx === 1 ? 0 : 1); }

    // ── Weekly Rocks — Appointments — owner filter ────────────────────────
    function setApptOwner(which, btn) {
      document.querySelectorAll(".smv-owner-btn2").forEach(b => b.classList.remove("mktg-btn-active"));
      btn.classList.add("mktg-btn-active");
      ["all", "stormer", "alex", "joncarlo", "cole"].forEach(k => {
        document.getElementById("apwView-" + k).style.display = (k === which) ? "" : "none";
      });
    }

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

    // ── Won bento — Signings by Month ───────────────────────────────────────
    if (WON_MONTH_LABELS.length) {
      new Chart(document.getElementById("chartWonMonthly"), {
        type: "bar",
        data: {
          labels: WON_MONTH_LABELS,
          datasets: [{
            data:               WON_MONTH_DATA,
            backgroundColor:    WON_MONTH_DATA.map((_, i) => i === WON_MONTH_DATA.length - 1 ? "rgba(200,255,1,0.30)" : "#C8FF01"),
            borderRadius:       4,
            borderSkipped:      false,
            barPercentage:      0.65,
            categoryPercentage: 0.68,
          }],
        },
        options: {
          responsive:          true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: { displayColors: false, callbacks: { label: ctx => ` ${ctx.parsed.y} signed` } },
          },
          scales: {
            x: { grid: { display: false }, ticks: { color: "#9A9AA5", font: { size: 10 } } },
            y: { display: false, beginAtZero: true },
          },
        },
      });
    }

    // ── Monthly volume — full-history popout (built lazily on first open) ──
    let chartMonthlyFull = null;
    function openMonthlyModal() {
      document.getElementById("monthlyOverlay").classList.add("open");
      if (!chartMonthlyFull) {
        chartMonthlyFull = new Chart(document.getElementById("chartMonthlyFull"), {
          type: "bar",
          data: {
            labels: MONTHLY_FULL.map(m => m.label),
            datasets: [{
              data:               MONTHLY_FULL.map(m => m.count),
              backgroundColor:    MONTHLY_FULL.map((m, i) => i === MONTHLY_FULL.length - 1 ? "rgba(200,255,1,0.30)" : "#5B8FFF"),
              borderRadius:       6,
              borderSkipped:      false,
              barPercentage:      0.70,
              categoryPercentage: 0.68,
            }],
          },
          options: {
            responsive:          true,
            maintainAspectRatio: false,
            layout: { padding: { top: 24 } },
            plugins: {
              legend: { display: false },
              tooltip: {
                displayColors: false,
                callbacks: { label: ctx => ` ${ctx.parsed.y} MGL leads` },
              },
            },
            scales: {
              x: { grid: { display: false }, ticks: { color: "#9A9AA5", font: { size: 12 } } },
              y: { display: false, beginAtZero: true },
            },
          },
          plugins: [monthlyDataLabels],
        });
      } else {
        chartMonthlyFull.resize();
      }
    }
    function closeMonthlyModal() {
      document.getElementById("monthlyOverlay").classList.remove("open");
    }

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

    function renderShared(dateKey) {
      renderPipeline(dateKey);
    }

    renderShared(PIPELINE_DATES[0]);
    pipeSel.addEventListener("change", () => renderShared(pipeSel.value));

    // ── Lead Source bar — independent month selector (created-date based) ──
    // Compares how the MGL/SGL/Other mix evolves by calendar month, separate
    // from the "Viewing Snapshot" picker above (which is a point-in-time
    // cumulative pipeline state, a different question from "how did leads
    // sourced in a given month break down").
    function fmtSourceMonth(m) {
      const [y, mo] = m.split("-");
      return new Date(y, mo - 1, 1).toLocaleDateString("en-US", {month:"long", year:"numeric"});
    }

    const sourceMonthSel = document.getElementById("sourceMonthSelect");
    SOURCE_MONTHS.slice().reverse().forEach(m => {
      const opt = document.createElement("option");
      opt.value = m; opt.textContent = fmtSourceMonth(m);
      sourceMonthSel.appendChild(opt);
    });

    function renderSourceByMonth(monthKey) {
      const src = monthKey ? SOURCE_BY_MONTH[monthKey] : {labels: SOURCE_LABELS, data: SOURCE_DATA, total: SOURCE_TOTAL};
      const noDataEl = document.getElementById("sourceNoData");

      if (!src || src.total === 0) {
        noDataEl.style.display = "flex";
        sourceChart.data.datasets[0].data = [0, 0, 0];
        sourceChart.update();
        document.getElementById("sourceTotalLabel").textContent = "No data for this month";
        return;
      }

      noDataEl.style.display = "none";
      sourceChart.data.labels           = src.labels;
      sourceChart.data.datasets[0].data = src.data;
      sourceChart.update();
      document.getElementById("sourceTotalLabel").textContent = `${src.total} Opportunities`;
    }

    renderSourceByMonth("");
    sourceMonthSel.addEventListener("change", () => renderSourceByMonth(sourceMonthSel.value));

  </script>
</body>
</html>
"""

# ─── 7j. RAW DATA PAGE — Field Movements + Appointments log ─────────────────
# Separate static page (axis-growth-data.html), linked from the main header,
# with two tabs. "Field Movements" is the detail behind every Weekly Rocks
# count: each row is one opportunity entering one stage, read from GHL's
# "Date Entered" custom fields. "Appointments" is GHL's own calendar-events
# log, filtered to Jul 1, W1 onward -- both are real GHL records, no
# snapshot-diffing involved.

_ap_owners    = sorted({e["owner"] for e in appointment_events})
_ap_calendars = sorted({e["calendar"] for e in appointment_events})
_ap_types     = sorted({e["type"] for e in appointment_events})
_ap_statuses  = sorted({e["status"] for e in appointment_events if e["status"]})
_ap_weeks     = sorted({e["week"] for e in appointment_events}, key=lambda w: int(w[1:]), reverse=True)

_fm_owners = sorted({e["owner"] for e in field_movement_events})
_fm_weeks  = sorted({e["week"] for e in field_movement_events}, key=lambda w: int(w[1:]), reverse=True)

_won_owners  = sorted({e["owner"] for e in won_deals_events})
_won_sources = sorted({e["source"] for e in won_deals_events if e["source"]})

def _rd_options(values):
    return "".join(f'<option value="{v}">{v}</option>' for v in values)

RAW_DATA_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Axis Growth — Raw Data</title>
  <link rel="icon" type="image/svg+xml" href="assets/favicon.svg">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Saira:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg:        #101014;
      --surface:   #1C1C24;
      --surface-2: #262630;
      --hero:      #C8FF01;
      --text:      #F5F5F7;
      --text-mute: #9A9AA5;
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
    .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 28px; }
    .header-left  { display: flex; align-items: center; gap: 20px; }
    .header-logo  { height: 44px; width: auto; display: block; }
    .header-title { font-size: 1.55rem; font-weight: 800; letter-spacing: 0.05em; text-transform: uppercase; }
    .header-sub   { font-size: 0.7rem; color: var(--text-mute); letter-spacing: 0.1em; text-transform: uppercase; margin-top: 2px; }
    .header-meta  { font-size: 0.7rem; color: var(--text-mute); letter-spacing: 0.08em; text-transform: uppercase; }
    .btn {
      display: inline-flex; align-items: center; gap: 6px;
      background: var(--surface-2); border: 1px solid var(--line); color: var(--text-mute);
      border-radius: 8px; padding: 6px 12px; font-family: inherit; font-size: 0.68rem;
      font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; cursor: pointer; text-decoration: none;
    }
    .btn:hover { color: var(--hero); border-color: var(--hero); }
    .rd-tabs { display: flex; gap: 8px; margin-bottom: 18px; }
    .rd-tab {
      background: none; border: none; color: var(--text-mute); cursor: pointer;
      font-family: inherit; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em;
      text-transform: uppercase; padding: 8px 4px; border-bottom: 2px solid transparent;
    }
    .rd-tab:hover { color: var(--text); }
    .rd-tab-active { color: var(--hero); border-bottom-color: var(--hero); }
    .card { background: var(--surface); border-radius: 18px; padding: 24px 28px; box-shadow: 0 4px 28px rgba(0,0,0,0.5); }
    .rd-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }
    .rd-input, .rd-select {
      background: var(--surface-2); color: var(--text); border: 1px solid var(--line);
      border-radius: 6px; padding: 6px 10px; font-family: inherit; font-size: 0.74rem; outline: none;
    }
    .rd-input  { flex: 1; min-width: 200px; }
    .rd-count  { font-size: 0.66rem; color: var(--text-mute); letter-spacing: 0.08em; text-transform: uppercase; white-space: nowrap; }
    .rd-wrap   { overflow-x: auto; }
    table.rd-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
    .rd-table th {
      font-size: 0.62rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
      color: var(--text-mute); text-align: left; padding: 0 10px 10px; border-bottom: 1px solid var(--line);
      white-space: nowrap; cursor: pointer; user-select: none;
    }
    .rd-table th:hover { color: var(--text); }
    .rd-table th .rd-sort-arrow { opacity: 0.6; margin-left: 3px; }
    .rd-table td { padding: 8px 10px; border-bottom: 1px solid var(--line); color: var(--text); white-space: nowrap; }
    .rd-table td.rd-mute { color: var(--text-mute); }
    .rd-table tr:hover td { background: var(--surface-2); }
    .rd-empty { text-align: center; padding: 40px 0; color: var(--text-mute); font-size: 0.78rem; }
  </style>
</head>
<body>
"""

RAW_DATA_HEADER = f"""
  <header class="header">
    <div class="header-left">
      <img src="assets/logo.svg" alt="Axis Growth" class="header-logo">
      <div>
        <div class="header-title">Axis Growth</div>
        <div class="header-sub">Raw Data · Movements, Appointments &amp; Won</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:16px;">
      <a href="axis-growth.html" class="btn">&larr; Dashboard</a>
      <span class="header-meta">Generated {generated_at}</span>
    </div>
  </header>
"""

RAW_DATA_BODY = f"""
  <div class="rd-tabs">
    <button class="rd-tab rd-tab-active" id="tabBtn-appointments" onclick="switchTab('appointments')">Appointments</button>
    <button class="rd-tab" id="tabBtn-fieldmoves" onclick="switchTab('fieldmoves')">Field Movements</button>
    <button class="rd-tab" id="tabBtn-won" onclick="switchTab('won')">All Time Won</button>
  </div>

  <section class="card" id="tab-appointments">
    <div class="rd-toolbar">
      <input id="apSearch" class="rd-input" type="text" placeholder="Search contact, owner, or calendar…" oninput="apRender()">
      <select id="apOwner" class="rd-select" onchange="apRender()">
        <option value="">All Owners</option>
        {_rd_options(_ap_owners)}
      </select>
      <select id="apCalendar" class="rd-select" onchange="apRender()">
        <option value="">All Calendars</option>
        {_rd_options(_ap_calendars)}
      </select>
      <select id="apType" class="rd-select" onchange="apRender()">
        <option value="">All Types</option>
        {_rd_options(_ap_types)}
      </select>
      <select id="apStatus" class="rd-select" onchange="apRender()">
        <option value="">All Statuses</option>
        {_rd_options(_ap_statuses)}
      </select>
      <select id="apWeek" class="rd-select" onchange="apRender()">
        <option value="">All Weeks</option>
        {_rd_options(_ap_weeks)}
      </select>
      <span id="apCount" class="rd-count"></span>
      <button class="btn" onclick="downloadAppointmentsCSV()">&#8595; CSV</button>
    </div>
    <div class="rd-wrap">
      <table class="rd-table">
        <thead>
          <tr>
            <th onclick="apSort('date')">Date<span class="rd-sort-arrow" id="apArrow-date"></span></th>
            <th onclick="apSort('time')">Time<span class="rd-sort-arrow" id="apArrow-time"></span></th>
            <th onclick="apSort('week')">Week<span class="rd-sort-arrow" id="apArrow-week"></span></th>
            <th onclick="apSort('calendar')">Calendar<span class="rd-sort-arrow" id="apArrow-calendar"></span></th>
            <th onclick="apSort('type')">Type<span class="rd-sort-arrow" id="apArrow-type"></span></th>
            <th onclick="apSort('contact')">Contact<span class="rd-sort-arrow" id="apArrow-contact"></span></th>
            <th onclick="apSort('owner')">Owner<span class="rd-sort-arrow" id="apArrow-owner"></span></th>
            <th onclick="apSort('status')">Status<span class="rd-sort-arrow" id="apArrow-status"></span></th>
            <th onclick="apSort('booked_on')">Booked On<span class="rd-sort-arrow" id="apArrow-booked_on"></span></th>
          </tr>
        </thead>
        <tbody id="apBody"></tbody>
      </table>
      <div id="apEmpty" class="rd-empty" style="display:none;">No appointments match these filters.</div>
    </div>
  </section>

  <section class="card" id="tab-fieldmoves" style="display:none;">
    <div style="font-size:0.72rem;color:var(--text-mute);margin-bottom:14px;">
      Read directly off each opportunity's own "Date Entered" custom fields (New Lead / Discovery Call / Strategy Call / Proposal Sent / Agreement Sent / Agreement Signed) -- populated by GHL workflows fixed and validated live on 2026-07-27. This is the true stage-entry date GHL recorded, no day-over-day snapshot diffing involved. This is the source behind the homepage Weekly Rocks card.
    </div>
    <div class="rd-toolbar">
      <input id="fmSearch" class="rd-input" type="text" placeholder="Search opportunity, contact, or owner…" oninput="fmRender()">
      <select id="fmOwner" class="rd-select" onchange="fmRender()">
        <option value="">All Owners</option>
        {_rd_options(_fm_owners)}
      </select>
      <select id="fmStage" class="rd-select" onchange="fmRender()">
        <option value="">All Stages</option>
        {_rd_options(FIELD_STAGE_LABELS)}
      </select>
      <select id="fmWeek" class="rd-select" onchange="fmRender()">
        <option value="">All Weeks</option>
        {_rd_options(_fm_weeks)}
      </select>
      <span id="fmCount" class="rd-count"></span>
      <button class="btn" onclick="downloadFieldMovementsCSV()">&#8595; CSV</button>
    </div>
    <div class="rd-wrap">
      <table class="rd-table">
        <thead>
          <tr>
            <th onclick="fmSort('date')">Date<span class="rd-sort-arrow" id="fmArrow-date"></span></th>
            <th onclick="fmSort('week')">Week<span class="rd-sort-arrow" id="fmArrow-week"></span></th>
            <th onclick="fmSort('stage')">Stage Entered<span class="rd-sort-arrow" id="fmArrow-stage"></span></th>
            <th onclick="fmSort('opp_name')">Opportunity<span class="rd-sort-arrow" id="fmArrow-opp_name"></span></th>
            <th onclick="fmSort('contact')">Contact<span class="rd-sort-arrow" id="fmArrow-contact"></span></th>
            <th onclick="fmSort('owner')">Owner<span class="rd-sort-arrow" id="fmArrow-owner"></span></th>
            <th onclick="fmSort('source')">Source<span class="rd-sort-arrow" id="fmArrow-source"></span></th>
          </tr>
        </thead>
        <tbody id="fmBody"></tbody>
      </table>
      <div id="fmEmpty" class="rd-empty" style="display:none;">No field-based movements match these filters.</div>
    </div>
  </section>

  <section class="card" id="tab-won" style="display:none;">
    <div style="font-size:0.72rem;color:var(--text-mute);margin-bottom:14px;">
      Every opportunity currently sitting in the Sales Pipeline's Onboarding stage -- this pipeline's definition of a closed-won deal (not GHL's own won/lost status field; see the Won bento's info icon on the main dashboard). Date is when the opportunity entered that stage (lastStageChangeAt).
    </div>
    <div class="rd-toolbar">
      <input id="wonSearch" class="rd-input" type="text" placeholder="Search opportunity, contact, or owner…" oninput="wonRender()">
      <select id="wonOwner" class="rd-select" onchange="wonRender()">
        <option value="">All Owners</option>
        {_rd_options(_won_owners)}
      </select>
      <select id="wonSource" class="rd-select" onchange="wonRender()">
        <option value="">All Sources</option>
        {_rd_options(_won_sources)}
      </select>
      <span id="wonCount" class="rd-count"></span>
      <button class="btn" onclick="downloadWonCSV()">&#8595; CSV</button>
    </div>
    <div class="rd-wrap">
      <table class="rd-table">
        <thead>
          <tr>
            <th onclick="wonSort('date')">Date Won<span class="rd-sort-arrow" id="wonArrow-date"></span></th>
            <th onclick="wonSort('opp_name')">Opportunity<span class="rd-sort-arrow" id="wonArrow-opp_name"></span></th>
            <th onclick="wonSort('contact')">Contact<span class="rd-sort-arrow" id="wonArrow-contact"></span></th>
            <th onclick="wonSort('value')">Value<span class="rd-sort-arrow" id="wonArrow-value"></span></th>
            <th onclick="wonSort('owner')">Owner<span class="rd-sort-arrow" id="wonArrow-owner"></span></th>
            <th onclick="wonSort('source')">Source<span class="rd-sort-arrow" id="wonArrow-source"></span></th>
          </tr>
        </thead>
        <tbody id="wonBody"></tbody>
      </table>
      <div id="wonEmpty" class="rd-empty" style="display:none;">No won deals match these filters.</div>
    </div>
  </section>
"""

# Data injection (f-string — just the JSON arrays) — kept separate from the
# logic script below so the JS's own { } characters don't need doubling.
RAW_DATA_SCRIPT = f"""
  <script>
    const APPOINTMENT_EVENTS    = {json.dumps(appointment_events)};
    const FIELD_MOVEMENT_EVENTS = {json.dumps(field_movement_events)};
    const WON_DEALS_EVENTS      = {json.dumps(won_deals_events)};
  </script>
"""

RAW_DATA_LOGIC_SCRIPT = """
  <script>
    function switchTab(tab) {
      ["appointments", "fieldmoves", "won"].forEach(t => {
        document.getElementById("tab-" + t).style.display = (t === tab) ? "" : "none";
        document.getElementById("tabBtn-" + t).classList.toggle("rd-tab-active", t === tab);
      });
    }

    function csvDownload(rows, cols, headerRow, filename) {
      const esc  = v => `"${String(v).replace(/"/g, '""')}"`;
      const lines = [headerRow.map(esc).join(",")].concat(
        rows.map(e => cols.map(c => esc(e[c])).join(","))
      );
      const blob = new Blob([lines.join("\\n")], { type: "text/csv" });
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    }

    // ── Appointments log ──────────────────────────────────────────────────
    let apSortKey = "date";
    let apSortDir = -1;
    let apVisible = APPOINTMENT_EVENTS;

    function apSort(key) {
      if (apSortKey === key) { apSortDir *= -1; } else { apSortKey = key; apSortDir = 1; }
      apRender();
    }

    function apRender() {
      const q    = document.getElementById("apSearch").value.trim().toLowerCase();
      const owner = document.getElementById("apOwner").value;
      const cal   = document.getElementById("apCalendar").value;
      const typ   = document.getElementById("apType").value;
      const stat  = document.getElementById("apStatus").value;
      const week  = document.getElementById("apWeek").value;

      let rows = APPOINTMENT_EVENTS.filter(e => {
        if (owner && e.owner !== owner) return false;
        if (cal   && e.calendar !== cal) return false;
        if (typ   && e.type !== typ) return false;
        if (stat  && e.status !== stat) return false;
        if (week  && e.week !== week) return false;
        if (q && !(e.contact.toLowerCase().includes(q) || e.owner.toLowerCase().includes(q) || e.calendar.toLowerCase().includes(q))) return false;
        return true;
      });

      rows.sort((a, b) => {
        const av = a[apSortKey], bv = b[apSortKey];
        if (av < bv) return -1 * apSortDir;
        if (av > bv) return  1 * apSortDir;
        return 0;
      });
      apVisible = rows;

      document.querySelectorAll("#tab-appointments .rd-sort-arrow").forEach(el => el.textContent = "");
      document.getElementById("apArrow-" + apSortKey).textContent = apSortDir === 1 ? "▲" : "▼";

      document.getElementById("apCount").textContent = rows.length + " appointment" + (rows.length === 1 ? "" : "s");
      document.getElementById("apEmpty").style.display = rows.length ? "none" : "block";

      document.getElementById("apBody").innerHTML = rows.map(e => `
        <tr>
          <td>${e.date}</td>
          <td class="rd-mute">${e.time}</td>
          <td class="rd-mute">${e.week}</td>
          <td>${e.calendar}</td>
          <td class="rd-mute">${e.type}</td>
          <td>${e.contact}</td>
          <td>${e.owner}</td>
          <td>${e.status}</td>
          <td class="rd-mute">${e.booked_on}</td>
        </tr>
      `).join("");
    }

    function downloadAppointmentsCSV() {
      csvDownload(
        apVisible,
        ["date","time","week","calendar","type","contact","owner","status","booked_on"],
        ["Date","Time","Week","Calendar","Type","Contact","Owner","Status","Booked On"],
        "axis-appointments.csv"
      );
    }

    // ── Field-based movements (from opportunity "Date Entered" fields) ─────
    let fmSortKey = "date";
    let fmSortDir = -1;
    let fmVisible = FIELD_MOVEMENT_EVENTS;

    function fmSort(key) {
      if (fmSortKey === key) { fmSortDir *= -1; } else { fmSortKey = key; fmSortDir = 1; }
      fmRender();
    }

    function fmRender() {
      const q     = document.getElementById("fmSearch").value.trim().toLowerCase();
      const owner = document.getElementById("fmOwner").value;
      const stage = document.getElementById("fmStage").value;
      const week  = document.getElementById("fmWeek").value;

      let rows = FIELD_MOVEMENT_EVENTS.filter(e => {
        if (owner && e.owner !== owner) return false;
        if (stage && e.stage !== stage) return false;
        if (week  && e.week  !== week)  return false;
        if (q && !(e.opp_name.toLowerCase().includes(q) || e.contact.toLowerCase().includes(q) || e.owner.toLowerCase().includes(q))) return false;
        return true;
      });

      rows.sort((a, b) => {
        const av = a[fmSortKey], bv = b[fmSortKey];
        if (av < bv) return -1 * fmSortDir;
        if (av > bv) return  1 * fmSortDir;
        return 0;
      });
      fmVisible = rows;

      document.querySelectorAll("#tab-fieldmoves .rd-sort-arrow").forEach(el => el.textContent = "");
      document.getElementById("fmArrow-" + fmSortKey).textContent = fmSortDir === 1 ? "▲" : "▼";

      document.getElementById("fmCount").textContent = rows.length + " movement" + (rows.length === 1 ? "" : "s");
      document.getElementById("fmEmpty").style.display = rows.length ? "none" : "block";

      document.getElementById("fmBody").innerHTML = rows.map(e => `
        <tr>
          <td>${e.date}</td>
          <td class="rd-mute">${e.week}</td>
          <td>${e.stage}</td>
          <td>${e.opp_name}</td>
          <td class="rd-mute">${e.contact}</td>
          <td>${e.owner}</td>
          <td class="rd-mute">${e.source}</td>
        </tr>
      `).join("");
    }

    function downloadFieldMovementsCSV() {
      csvDownload(
        fmVisible,
        ["date","week","stage","opp_name","contact","owner","source"],
        ["Date","Week","Stage Entered","Opportunity","Contact","Owner","Source"],
        "axis-field-movements.csv"
      );
    }

    // ── All Time Won (opportunities in the Onboarding stage) ───────────────
    let wonSortKey = "date";
    let wonSortDir = -1;
    let wonVisible = WON_DEALS_EVENTS;

    function wonSort(key) {
      if (wonSortKey === key) { wonSortDir *= -1; } else { wonSortKey = key; wonSortDir = 1; }
      wonRender();
    }

    function wonRender() {
      const q      = document.getElementById("wonSearch").value.trim().toLowerCase();
      const owner  = document.getElementById("wonOwner").value;
      const source = document.getElementById("wonSource").value;

      let rows = WON_DEALS_EVENTS.filter(e => {
        if (owner  && e.owner !== owner) return false;
        if (source && e.source !== source) return false;
        if (q && !(e.opp_name.toLowerCase().includes(q) || e.contact.toLowerCase().includes(q) || e.owner.toLowerCase().includes(q))) return false;
        return true;
      });

      rows.sort((a, b) => {
        const av = a[wonSortKey], bv = b[wonSortKey];
        if (av < bv) return -1 * wonSortDir;
        if (av > bv) return  1 * wonSortDir;
        return 0;
      });
      wonVisible = rows;

      document.querySelectorAll("#tab-won .rd-sort-arrow").forEach(el => el.textContent = "");
      document.getElementById("wonArrow-" + wonSortKey).textContent = wonSortDir === 1 ? "▲" : "▼";

      document.getElementById("wonCount").textContent = rows.length + " won deal" + (rows.length === 1 ? "" : "s");
      document.getElementById("wonEmpty").style.display = rows.length ? "none" : "block";

      document.getElementById("wonBody").innerHTML = rows.map(e => `
        <tr>
          <td>${e.date}</td>
          <td>${e.opp_name}</td>
          <td class="rd-mute">${e.contact}</td>
          <td>${e.value ? "$" + e.value.toLocaleString("en-US", {maximumFractionDigits: 0}) : "—"}</td>
          <td>${e.owner}</td>
          <td class="rd-mute">${e.source}</td>
        </tr>
      `).join("");
    }

    function downloadWonCSV() {
      csvDownload(
        wonVisible,
        ["date","opp_name","contact","value","owner","source"],
        ["Date Won","Opportunity","Contact","Value","Owner","Source"],
        "axis-all-time-won.csv"
      );
    }

    apRender();
    fmRender();
    wonRender();
  </script>
</body>
</html>
"""

raw_data_html = RAW_DATA_HEAD + RAW_DATA_HEADER + RAW_DATA_BODY + RAW_DATA_SCRIPT + RAW_DATA_LOGIC_SCRIPT
raw_data_path = Path(__file__).parent / "axis-growth-data.html"
raw_data_path.write_text(raw_data_html, encoding="utf-8")
print(f"Raw data page written → {raw_data_path} ({len(field_movement_events)} field movements, {len(appointment_events)} appointments, {len(won_deals_events)} won deals)")


# ─── 7k. WHITEBOARD PAGE — kpi_whiteboard.html, now generated live ──────────
# Was hand-maintained (WON/DATA hardcoded JS consts, manually refreshed) --
# folded in here so it's produced by the same script, on the same twice-daily
# CI cadence, as axis-growth.html and axis-growth-data.html. See 6i for the
# aggregation this reads from. SDR Capacity tab and the July Sales Report
# tab (a frozen historical recap) are carried over unchanged.

# ============ WHITEBOARD_HEAD (verbatim CSS/head, plain string) ============
WHITEBOARD_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AXISKEY — Spend Efficiency Whiteboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Saira:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
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
    --series-1:  #3987e5;  /* SGL */
    --series-2:  #d95926;  /* Referral */
    --series-3:  #199e70;  /* MGL */
    --series-4:  #c98500;  /* Client */
    --series-other: #6B6B76;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: "Saira", "Eurostile", system-ui, sans-serif;
    padding: 32px 40px 80px;
  }
  header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 16px;
    margin-bottom: 28px;
    border-bottom: 1px solid var(--line);
    padding-bottom: 20px;
  }
  h1 {
    font-size: 1.6rem;
    font-weight: 800;
    margin: 0 0 4px;
    letter-spacing: 0.02em;
  }
  .subtitle {
    color: var(--text-mute);
    font-size: 0.82rem;
    max-width: 640px;
    line-height: 1.5;
  }
  .badge {
    display: inline-block;
    background: var(--surface-2);
    color: var(--text-mute);
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 4px 12px;
    font-size: 0.68rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    white-space: nowrap;
  }
  section {
    margin-bottom: 36px;
  }
  .section-title {
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-mute);
    margin: 0 0 14px;
  }

  /* Hero figure */
  .hero-card {
    background: var(--surface);
    border-radius: 18px;
    padding: 24px 28px;
    display: flex;
    align-items: baseline;
    gap: 20px;
    flex-wrap: wrap;
    margin-bottom: 24px;
  }
  .hero-value {
    font-size: 3rem;
    font-weight: 800;
    color: var(--hero);
    line-height: 1;
  }
  .hero-label {
    font-size: 0.78rem;
    color: var(--text-mute);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .hero-note {
    color: var(--text-mute);
    font-size: 0.78rem;
    flex-basis: 100%;
  }

  /* Stat tile grid */
  .tile-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 14px;
  }
  .tile {
    background: var(--surface);
    border-radius: 18px;
    padding: 16px 18px;
  }
  .tile-label {
    font-size: 0.66rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-mute);
    margin-bottom: 6px;
  }
  .tile-value {
    font-size: 1.5rem;
    font-weight: 700;
  }
  .tile-value.hero-color { color: var(--hero); }
  .tile-sub {
    font-size: 0.68rem;
    color: var(--text-mute);
    margin-top: 4px;
  }

  /* Funnel visual */
  .funnel-wrap {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    padding: 12px 0 4px;
  }
  .funnel-bar {
    background: var(--hero);
    color: #101014;
    border-radius: 10px;
    padding: 16px 26px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    box-sizing: border-box;
  }
  .funnel-stage-label {
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .funnel-stage-value {
    font-size: 1.6rem;
    font-weight: 800;
    white-space: nowrap;
  }
  .funnel-conv {
    color: var(--text-mute);
    font-size: 0.76rem;
    font-weight: 700;
    padding: 6px 0;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .funnel-conv .funnel-conv-pct { color: var(--text); font-size: 0.9rem; }

  /* Small-multiple bar charts */
  .chart-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 16px;
  }
  .chart-card {
    background: var(--surface);
    border-radius: 18px;
    padding: 18px 20px 12px;
  }
  .chart-card-title {
    font-size: 0.78rem;
    font-weight: 700;
    margin-bottom: 10px;
  }
  svg text { font-family: "Saira", system-ui, sans-serif; }
  .bar-value {
    fill: var(--text);
    font-size: 12px;
    font-weight: 600;
  }
  .bar-month {
    fill: var(--text-mute);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .gridline { stroke: var(--line); stroke-width: 1; }
  .bar-rect { fill: var(--hero); cursor: pointer; }
  .bar-rect:hover, .bar-rect.hover { fill: #dbff4d; }
  .bar-hit { fill: transparent; cursor: pointer; }

  /* Table */
  table {
    width: 100%;
    border-collapse: collapse;
    background: var(--surface);
    border-radius: 18px;
    overflow: hidden;
  }
  th, td {
    padding: 12px 16px;
    text-align: right;
    font-variant-numeric: tabular-nums;
    font-size: 0.84rem;
    border-bottom: 1px solid var(--line);
  }
  th {
    color: var(--text-mute);
    font-size: 0.66rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 600;
  }
  th:first-child, td:first-child { text-align: left; }
  tr:last-child td { border-bottom: none; }
  td.hero-color { color: var(--hero); font-weight: 700; }

  /* Simulator */
  .sim-card {
    background: var(--surface);
    border-radius: 18px;
    padding: 24px 26px;
  }
  .sim-controls {
    display: flex;
    gap: 24px;
    flex-wrap: wrap;
    align-items: flex-end;
    margin-bottom: 22px;
  }
  .control-group { min-width: 220px; flex: 1; }
  .control-label {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-mute);
    margin-bottom: 8px;
    display: block;
  }
  .segmented {
    display: flex;
    background: var(--surface-2);
    border-radius: 10px;
    padding: 3px;
    gap: 3px;
  }
  .segmented button {
    flex: 1;
    background: transparent;
    border: none;
    color: var(--text-mute);
    font-family: inherit;
    font-size: 0.76rem;
    font-weight: 600;
    padding: 7px 10px;
    border-radius: 8px;
    cursor: pointer;
  }
  .segmented button.active {
    background: var(--hero);
    color: #101014;
  }
  input[type="range"] {
    width: 100%;
    accent-color: var(--hero);
    height: 4px;
  }
  .slider-value {
    font-size: 1.3rem;
    font-weight: 800;
    color: var(--hero);
    margin-bottom: 8px;
  }
  .sim-results {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 14px;
    margin-bottom: 22px;
  }
  .sim-tile {
    background: var(--surface-2);
    border-radius: 14px;
    padding: 14px 16px;
  }
  .sim-tile .tile-value { color: var(--hero); }
  .caveat {
    color: var(--text-mute);
    font-size: 0.72rem;
    line-height: 1.5;
    margin-top: 14px;
    border-top: 1px solid var(--line);
    padding-top: 14px;
  }

  /* Line chart */
  .legend {
    display: flex;
    gap: 18px;
    font-size: 0.72rem;
    color: var(--text-mute);
    margin-bottom: 8px;
  }
  .legend-item { display: flex; align-items: center; gap: 6px; }
  .legend-swatch-line { width: 14px; height: 2px; background: var(--hero); display: inline-block; }
  .legend-swatch-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-mute); display: inline-block; }
  .legend-swatch-rect { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
  .legend-source { margin-bottom: 10px; flex-wrap: wrap; }
  .seg-hit:hover + .seg-mark, .seg-mark.seg-hover { filter: brightness(1.18); }

  #tooltip {
    position: fixed;
    background: var(--surface-2);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 8px 12px;
    font-size: 0.76rem;
    pointer-events: none;
    display: none;
    z-index: 10;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
  }
  #tooltip .tt-value { font-weight: 700; color: var(--text); font-size: 0.86rem; }
  #tooltip .tt-label { color: var(--text-mute); }

  /* Tabs */
  .tabs {
    display: flex;
    gap: 6px;
    margin-bottom: 24px;
  }
  .tab-btn {
    background: var(--surface);
    color: var(--text-mute);
    border: 1px solid var(--line);
    font-family: inherit;
    font-size: 0.8rem;
    font-weight: 600;
    padding: 9px 18px;
    border-radius: 10px;
    cursor: pointer;
  }
  .tab-btn.active {
    background: var(--hero);
    color: #101014;
    border-color: var(--hero);
  }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  td.mute { color: var(--text-mute); }
  td.empty-field { color: var(--text-mute); font-style: italic; }
  tr.bd-row td { background: rgba(57, 135, 229, 0.16); }
  tr.bd-row td:first-child { border-left: 3px solid var(--series-1); }

  input.date-input {
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 4px 8px;
    font-family: inherit;
    font-size: 0.8rem;
    color-scheme: dark;
    width: 145px;
  }
  input.date-input.input-defaulted {
    color: var(--text-mute);
    font-style: italic;
  }
  input.value-input {
    background: var(--surface-2);
    color: var(--text);
    border: 1px dashed var(--line);
    border-radius: 6px;
    padding: 4px 8px;
    font-family: inherit;
    font-size: 0.84rem;
    font-variant-numeric: tabular-nums;
    width: 110px;
    text-align: right;
  }
  input.value-input:focus { border-style: solid; border-color: var(--hero); outline: none; }

  /* SDR Capacity tab */
  .mono {
    font-family: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums;
  }
  .input-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 12px;
    background: var(--surface);
    border-radius: 18px;
    padding: 18px 20px;
  }
  .field label {
    display: block;
    font-size: 0.66rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-mute);
    margin-bottom: 6px;
  }
  .field-row { display: flex; gap: 6px; align-items: center; }
  .field input[type="number"] {
    width: 100%;
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 7px 9px;
    font-size: 0.92rem;
    font-family: inherit;
  }
  .field input[type="number"]:focus { outline: 1px solid var(--hero); border-color: var(--hero); }
  .preset-btn {
    background: var(--surface-2);
    border: 1px solid var(--line);
    color: var(--text-mute);
    font-family: inherit;
    font-size: 0.66rem;
    padding: 6px 8px;
    border-radius: 6px;
    cursor: pointer;
    white-space: nowrap;
  }
  .preset-btn.active { border-color: var(--hero); color: var(--hero); }
  tr.total-row td { font-weight: 700; color: var(--text); }
  tr.total-row { background: var(--surface-2); }
  td.role-label { color: var(--text-mute); font-size: 0.8rem; }
  .hero-box {
    background: linear-gradient(160deg, var(--surface-2), var(--surface));
    border: 1px solid var(--hero);
    border-radius: 18px;
    padding: 22px 26px;
  }
  .hero-box-title {
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: var(--hero);
    margin-bottom: 14px;
  }
  .hero-box-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 18px;
  }
  .sdr-hero-num {
    font-size: 2.4rem;
    font-weight: 800;
    color: var(--text);
    line-height: 1;
  }
  .hero-box-label {
    font-size: 0.72rem;
    color: var(--text-mute);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-top: 6px;
  }
  .hero-assumption {
    margin-top: 16px;
    padding-top: 14px;
    border-top: 1px solid var(--line);
    color: var(--text-mute);
    font-size: 0.78rem;
  }
  .hero-assumption .mono { color: var(--hero); font-weight: 700; }
  .steps {
    background: var(--surface);
    border-radius: 18px;
    padding: 16px 20px;
    font-size: 0.82rem;
    line-height: 1.9;
    color: var(--text-mute);
  }
  .steps .step-line { white-space: nowrap; overflow-x: auto; }
  .steps .step-val { color: var(--text); font-weight: 600; }
  .steps .step-result { color: var(--hero); font-weight: 700; }

  /* Print / Save-as-PDF: only whichever tab is active prints (tab-panel
     display rules above already handle that); this drops the tab buttons.
     One giant custom page (generously tall) instead of Letter/A4 -- so the
     whole tab prints as a single continuous page with no breaks at all.
     This also sidesteps the flex/grid clipping bug from before (Chromium's
     print engine treats flex/grid containers like .funnel-wrap/.chart-row
     as one atomic block and clips whatever doesn't fit when a page boundary
     cuts through them) -- with only one page, no boundary ever cuts through
     anything. Trade-off: if content is shorter than the reserved height
     you'll get blank space at the bottom; if it's ever taller, the excess
     spills onto a genuine second page and the clipping risk returns there. */
  @media print {
    @page { size: 340mm 1300mm; margin: 12mm; }
    .tabs { display: none; }
  }
</style>
</head>
"""

# ============ WHITEBOARD_HEADER (f-string, embeds generated_at) ============
WHITEBOARD_HEADER = f"""
<body>

<header>
  <div>
    <h1>AXISKEY — Spend Efficiency Whiteboard</h1>
    <div class="subtitle">
      Meta Ads spend vs. GHL leads &amp; signings, live from the same GHL + Meta
      data as the main dashboard -- this page is generated by the same script,
      on the same twice-daily refresh. See Overview for month-by-month detail,
      the simulator for spend projections, and All-Time Won for the full deal
      list. The July Sales Report tab is a frozen historical recap and stays
      as originally written.
    </div>
  </div>
  <span class="badge">Generated {generated_at}</span>
</header>

<div class="tabs">
  <button class="tab-btn active" data-tab="tab-overview">Overview</button>
  <button class="tab-btn" data-tab="tab-won" id="wonTabBtn">All-Time Won</button>
  <button class="tab-btn" data-tab="tab-sdr">SDR Capacity</button>
  <button class="tab-btn" data-tab="tab-july">July Sales Report</button>
</div>
"""

# ============ WHITEBOARD_BODY_OVERVIEW (verbatim markup, 2 edits applied) ============
WHITEBOARD_BODY_OVERVIEW = """<div id="tab-overview" class="tab-panel active">

<section>
  <div class="hero-card">
    <div>
      <div class="hero-value" id="heroCPL">—</div>
      <div class="hero-label" id="heroCPLLabel">Blended cost per lead</div>
    </div>
    <div class="hero-note" id="heroNote"></div>
  </div>

  <div class="tile-grid">
    <div class="tile"><div class="tile-label">Total spend</div><div class="tile-value" id="totSpend">—</div></div>
    <div class="tile"><div class="tile-label">Total leads</div><div class="tile-value" id="totLeads">—</div></div>
    <div class="tile"><div class="tile-label">Total signings</div><div class="tile-value hero-color" id="totSignings">—</div></div>
    <div class="tile"><div class="tile-label">Blended CPL (all months)</div><div class="tile-value" id="totCPL">—</div></div>
    <div class="tile">
      <div class="tile-label">Blended cost / signing</div>
      <div class="tile-value" id="totCPS">—</div>
      <div class="tile-sub" id="totCPSFormula">—</div>
    </div>
    <div class="tile">
      <div class="tile-label">Blended cost / signing (MGL)</div>
      <div class="tile-value" id="totCPSMgl">—</div>
      <div class="tile-sub" id="totCPSMglFormula">—</div>
    </div>
  </div>
</section>

<section>
  <div class="section-title">Monthly breakdown</div>
  <div class="subtitle" style="margin-bottom: 14px;">
    Signings are classified by Agreement Signed date where it's been set on the
    All-Time Won tab, otherwise by Date Won.
  </div>
  <div class="chart-row">
    <div class="chart-card">
      <div class="chart-card-title">Meta Spend</div>
      <svg id="chartSpend" viewBox="0 0 240 160" width="100%"></svg>
    </div>
    <div class="chart-card">
      <div class="chart-card-title">GHL Leads Entered</div>
      <svg id="chartLeads" viewBox="0 0 240 160" width="100%"></svg>
    </div>
    <div class="chart-card">
      <div class="chart-card-title">Signings</div>
      <svg id="chartSignings" viewBox="0 0 240 160" width="100%"></svg>
    </div>
    <div class="chart-card">
      <div class="chart-card-title">Signings by Source</div>
      <div class="segmented" id="sourceMonthToggle" style="margin-bottom: 10px;"></div>
      <svg id="chartSourceBreakdown" viewBox="0 0 240 200" width="100%"></svg>
      <div class="legend legend-source" id="sourceLegend"></div>
    </div>
  </div>
  <div class="chart-row" style="margin-top: 16px;">
    <div class="chart-card" style="grid-column: 1 / -1;">
      <div class="chart-card-title">Cost / Signing</div>
      <div class="segmented" id="cpsToggle" style="margin-bottom: 10px;"></div>
      <svg id="chartCPS" viewBox="0 0 720 180" width="100%"></svg>
    </div>
  </div>
</section>

<section>
  <div class="section-title">Monthly KPI table</div>
  <table>
    <thead>
      <tr><th>Month</th><th>Spend</th><th>Leads</th><th>Signings</th><th>CPL</th><th>Cost / Signing</th></tr>
    </thead>
    <tbody id="kpiTableBody"></tbody>
  </table>
</section>

<section>
  <div class="section-title">Spend → Leads simulator</div>
  <div class="sim-card">
    <div class="sim-controls">
      <div class="control-group">
        <span class="control-label">CPL / conversion basis</span>
        <div class="segmented" id="basisToggle"></div>
      </div>
      <div class="control-group" style="flex: 2;">
        <span class="control-label">Monthly ad spend</span>
        <div class="slider-value" id="spendValue">$0</div>
        <input type="range" id="spendSlider" min="0" max="15000" step="50" value="6000">
      </div>
    </div>

    <div class="sim-results">
      <div class="sim-tile"><div class="tile-label">Predicted leads</div><div class="tile-value" id="predLeads">—</div></div>
      <div class="sim-tile"><div class="tile-label">Predicted signings</div><div class="tile-value" id="predSignings">—</div></div>
      <div class="sim-tile"><div class="tile-label">Predicted cost / signing</div><div class="tile-value" id="predCPS">—</div></div>
      <div class="sim-tile"><div class="tile-label">CPL used</div><div class="tile-value" id="predCPLUsed">—</div></div>
    </div>

    <div class="legend">
      <span class="legend-item"><span class="legend-swatch-line"></span> Projected (selected basis)</span>
      <span class="legend-item"><span class="legend-swatch-dot"></span> Actual month</span>
    </div>
    <svg id="chartSim" viewBox="0 0 800 260" width="100%"></svg>

    <div class="caveat">
      This is a straight-line projection (leads = spend ÷ CPL; signings = leads × that
      month's conversion rate) — it assumes the selected month's cost-per-lead holds at
      other spend levels. It won't hold forever: May's CPL ($24.70 at ~$1.4K spend) was
      far cheaper than June/July's (~$65–74 at ~$5.7–7.6K spend), so cost-per-lead has
      already risen once as spend scaled up. Treat this as a rough directional tool, not
      a guarantee — actual diminishing returns likely kick in at some point past current spend.
    </div>
  </div>
</section>

</div>
"""

# ============ WHITEBOARD_BODY_WON (hand-written, live-field caveat) ============
WHITEBOARD_BODY_WON = """
<div id="tab-won" class="tab-panel">
<section>
  <div class="section-title">All-time won opportunities (AXISKEY, all pipelines)</div>
  <div class="subtitle" style="margin-bottom: 14px;">
    "Date Won" is <code>lastStageChangeAt</code> -- when the opportunity entered
    the Onboarding stage, same basis as the Won bento on the main dashboard.
    "Agreement Signed" and "Capital Raiser Intent" are live GHL custom fields
    on the opportunity record, read directly -- no longer editable here; edit
    them on the opportunity in GHL and they'll appear on the next refresh.
    "Source" is GHL's standard opportunity source field.
  </div>
  <table>
    <thead>
      <tr>
        <th>Date Won</th>
        <th style="text-align:left;">Deal</th>
        <th>Value</th>
        <th style="text-align:left;">Owner</th>
        <th style="text-align:left;">Source</th>
        <th>Agreement Signed</th>
        <th style="text-align:left;">Capital Raiser Intent</th>
      </tr>
    </thead>
    <tbody id="wonTableBody"></tbody>
  </table>
  <div class="caveat">
    GHL's own "Agreement Signed Date" custom field was created 2026-07-23 and
    was never backfilled on deals won before then -- those show Date Won
    instead (muted styling) until the field is set in GHL. "Capital Raiser
    Intent" is populated only where that field was filled in on the
    opportunity in GHL. "Source" is the deal's own source field as recorded
    in GHL (including any specific referrer name like "Referral - Diego");
    blanks reflect missing data on the record, not an extraction issue here.
    "Won" only includes opportunities *currently* sitting in the Onboarding
    stage -- a deal that later moves further along the pipeline drops off
    this list, same as everywhere else on the dashboard that uses this
    definition.
  </div>
</section>
</div>
"""

# ============ WHITEBOARD_BODY_SDR (verbatim, fully independent) ============
WHITEBOARD_BODY_SDR = """<div id="tab-sdr" class="tab-panel">
<section>
  <div class="section-title">SDR Capacity &amp; Lead Target Calculator</div>
  <div class="subtitle" style="margin-bottom: 16px;">
    How many marketing contacts you need per month to fully saturate current calling
    capacity, given a contact→discovery-call conversion rate. Independent of the rest
    of this page — its own inputs, not tied to GHL/Meta data.
  </div>

  <div class="segmented" id="scenarioToggle" style="max-width: 420px; margin-bottom: 20px;"></div>

  <div class="section-title">Inputs</div>
  <div class="input-grid" style="margin-bottom: 24px;">
    <div class="field">
      <label for="numSDRs">Number of SDRs</label>
      <input type="number" id="numSDRs" value="2" min="0" step="1">
    </div>
    <div class="field">
      <label for="callsPerSDR">Calls / SDR / day</label>
      <input type="number" id="callsPerSDR" value="3" min="0" step="0.5">
    </div>
    <div class="field">
      <label for="numManagers">Sales Managers taking calls</label>
      <input type="number" id="numManagers" value="1" min="0" step="1">
    </div>
    <div class="field">
      <label for="managerPct">Manager capacity (% of SDR rate)</label>
      <input type="number" id="managerPct" value="50" min="0" max="500" step="5">
    </div>
    <div class="field">
      <label for="workingDays">Working days / week</label>
      <input type="number" id="workingDays" value="5" min="1" max="7" step="1">
    </div>
    <div class="field">
      <label for="weeksPerMonth">Weeks / month</label>
      <div class="field-row">
        <input type="number" id="weeksPerMonth" value="4.33" min="1" step="0.01">
        <button type="button" class="preset-btn" id="preset4">4</button>
        <button type="button" class="preset-btn active" id="preset433">4.33</button>
      </div>
    </div>
    <div class="field">
      <label for="conversionRate">Contact → discovery call rate (%)</label>
      <input type="number" id="conversionRate" value="50" min="1" max="100" step="1">
    </div>
  </div>

  <div class="section-title">Calling capacity breakdown</div>
  <table style="margin-bottom: 24px;">
    <thead>
      <tr><th>Role</th><th>/ day</th><th>/ week</th><th>/ month</th></tr>
    </thead>
    <tbody id="capacityBody"></tbody>
  </table>

  <div class="hero-box" style="margin-bottom: 24px;">
    <div class="hero-box-title">Leads to Generate</div>
    <div class="hero-box-row">
      <div>
        <div class="sdr-hero-num mono" id="targetMonth">—</div>
        <div class="hero-box-label">contacts / month</div>
      </div>
      <div>
        <div class="sdr-hero-num mono" id="targetWeek">—</div>
        <div class="hero-box-label">contacts / week</div>
      </div>
      <div>
        <div class="sdr-hero-num mono" id="targetDay">—</div>
        <div class="hero-box-label">contacts / working day</div>
      </div>
    </div>
    <div class="hero-assumption">
      Assumes a <span class="mono" id="assumedRate">—</span> contact → discovery-call conversion rate.
      Change it above to see how the target moves.
    </div>
  </div>

  <div class="section-title">Calculation steps</div>
  <div class="steps" id="stepsBody"></div>
</section>
</div>
"""

# ============ WHITEBOARD_BODY_JULY (verbatim, frozen historical content) ============
WHITEBOARD_BODY_JULY = """<div id="tab-july" class="tab-panel">
<div style="font-size: 2rem; font-weight: 800; letter-spacing: 0.01em; color: var(--hero); margin-bottom: 22px;">
  July 2026 Sales Report
</div>
<section>
  <div class="section-title">Sales Updates</div>
  <div style="background: var(--surface); border-radius: 14px; padding: 18px 22px; color: var(--text); font-size: 0.86rem; line-height: 1.6; margin-bottom: 28px;">
    On July 29, Meta ad spend jumped from an average of $218/day to $497/day — a 128% increase — following
    a budget increase made that day. Spend held at this elevated level through the rest of the month.
  </div>
</section>

<section>
  <div class="section-title">July at a Glance</div>
  <div class="tile-grid">
    <div class="tile"><div class="tile-label">Total Spend</div><div class="tile-value">$7,588.77</div></div>
    <div class="tile"><div class="tile-label">MGL Leads</div><div class="tile-value hero-color">63</div></div>
    <div class="tile"><div class="tile-label">MGL CPL</div><div class="tile-value">$120.46</div></div>
    <div class="tile"><div class="tile-label">Discovery Calls</div><div class="tile-value">63</div></div>
    <div class="tile"><div class="tile-label">Deals Won</div><div class="tile-value hero-color">13</div></div>
  </div>
</section>

<section>
  <div class="section-title">Funnel — Leads to Won</div>
  <div class="funnel-wrap">
    <div class="funnel-bar" style="width:100%;max-width:560px;">
      <span class="funnel-stage-label">Total Leads (July)</span>
      <span class="funnel-stage-value">99</span>
    </div>
    <div class="funnel-conv">↓ <span class="funnel-conv-pct">83.8%</span> (83 of 99) to Discovery Call</div>
    <div class="funnel-bar" style="width:84%;max-width:470px;">
      <span class="funnel-stage-label">Discovery Calls Booked</span>
      <span class="funnel-stage-value">83</span>
    </div>
    <div class="funnel-conv">↓ <span class="funnel-conv-pct">15.7%</span> (13 of 83) to Won</div>
    <div class="funnel-bar" style="width:40%;max-width:225px;">
      <span class="funnel-stage-label">Won Deals</span>
      <span class="funnel-stage-value">13</span>
    </div>
  </div>
  <div style="display:flex;align-items:baseline;gap:10px;justify-content:center;margin-top:18px;">
    <div style="font-size:1.8rem;font-weight:800;color:var(--hero);line-height:1;">13.1%</div>
    <div style="font-size:0.7rem;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:var(--text-mute);">Lead &rarr; Won</div>
  </div>
</section>

<section>
  <div class="section-title">Deal Source Distribution — July</div>
  <div class="chart-row" style="grid-template-columns: 1fr 1fr;">
    <div class="chart-card">
      <div class="chart-card-title">Leads by Source</div>
      <svg id="chartJulyLeadsSource" viewBox="0 0 240 200" width="100%"></svg>
      <div class="legend legend-source" id="julyLeadsLegend"></div>
    </div>
    <div class="chart-card">
      <div class="chart-card-title">Won Deals by Source</div>
      <svg id="chartJulyWonSource" viewBox="0 0 240 200" width="100%"></svg>
      <div class="legend legend-source" id="julyWonLegend"></div>
    </div>
  </div>
</section>

<section>
  <div class="section-title">Won Deals — Month over Month</div>
  <div class="chart-row" style="grid-template-columns: repeat(3, 1fr);">
    <div class="chart-card" style="text-align:center;">
      <div class="chart-card-title" id="momLabel0">—</div>
      <div style="font-size:2.2rem;font-weight:800;color:var(--hero);line-height:1;margin:14px 0 6px;" id="momValue0">—</div>
      <div style="font-size:0.68rem;color:var(--text-mute);" id="momDelta0">won deals</div>
    </div>
    <div class="chart-card" style="text-align:center;">
      <div class="chart-card-title" id="momLabel1">—</div>
      <div style="font-size:2.2rem;font-weight:800;color:var(--hero);line-height:1;margin:14px 0 6px;" id="momValue1">—</div>
      <div style="font-size:0.68rem;color:var(--text-mute);" id="momDelta1">won deals</div>
    </div>
    <div class="chart-card" style="text-align:center;">
      <div class="chart-card-title" id="momLabel2">—</div>
      <div style="font-size:2.2rem;font-weight:800;color:var(--hero);line-height:1;margin:14px 0 6px;" id="momValue2">—</div>
      <div style="font-size:0.68rem;color:var(--text-mute);" id="momDelta2">won deals</div>
    </div>
  </div>
</section>

<section>
  <div class="section-title">Won Deals — July</div>
  <div class="tile-grid" style="margin-bottom: 16px;">
    <div class="tile"><div class="tile-label">Broker Dealer Deals</div><div class="tile-value hero-color">3 of 13</div></div>
  </div>
  <table>
    <thead>
      <tr>
        <th style="text-align:left;">Deal</th>
        <th>Value</th>
        <th style="text-align:left;">Capital Raiser Intent</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td style="text-align:left;">Altitude Capital Partners GP LLC</td>
        <td><input type="text" class="value-input" data-id="gfWnkv7v3nnVvSLaYlbH" placeholder="$"></td>
        <td style="text-align:left;">Broker Dealer</td>
      </tr>
      <tr>
        <td style="text-align:left;">Doug Beasley - BD</td>
        <td><input type="text" class="value-input" data-id="zhH9NbSez7WGLOjkfCCY" placeholder="$"></td>
        <td style="text-align:left;">Broker Dealer</td>
      </tr>
      <tr>
        <td style="text-align:left;">Bob Weissman</td>
        <td><input type="text" class="value-input" data-id="zYAbC9cELbUYk1b9lBGb" placeholder="$"></td>
        <td style="text-align:left;" class="empty-field">—</td>
      </tr>
      <tr>
        <td style="text-align:left;">Sierra Vale Capital</td>
        <td><input type="text" class="value-input" data-id="d4YbtFMqmCjOb5ZFM6yL" placeholder="$"></td>
        <td style="text-align:left;" class="empty-field">—</td>
      </tr>
      <tr>
        <td style="text-align:left;">Amazing Hospitality Group</td>
        <td><input type="text" class="value-input" data-id="LAq3RBwfvJ07CDjLA03Z" placeholder="$"></td>
        <td style="text-align:left;">Issuer Team</td>
      </tr>
      <tr>
        <td style="text-align:left;">Skygate Growth Strategies</td>
        <td><input type="text" class="value-input" data-id="bP0hWoW5hV2ae6mLVLZp" placeholder="$"></td>
        <td style="text-align:left;">Broker Dealer</td>
      </tr>
      <tr class="bd-row">
        <td style="text-align:left;">RAS Technologies</td>
        <td><input type="text" class="value-input" data-id="7VvPSdUyrFl9caGcISZM" placeholder="$"></td>
        <td style="text-align:left;">Broker Dealer</td>
      </tr>
      <tr>
        <td style="text-align:left;">Fincera Series 1</td>
        <td><input type="text" class="value-input" data-id="lAG77KU7lPgP8493GJQC" placeholder="$"></td>
        <td style="text-align:left;">Issuer Team</td>
      </tr>
      <tr>
        <td style="text-align:left;">Orlando Surf Park Fund I, LP</td>
        <td><input type="text" class="value-input" data-id="LuJiEk7swQFH39k10XEz" placeholder="$"></td>
        <td style="text-align:left;">Broker Dealer</td>
      </tr>
      <tr class="bd-row">
        <td style="text-align:left;">IPC - Reg D</td>
        <td><input type="text" class="value-input" data-id="Rc2HLIgjqoNJwDvUlZ0i" placeholder="$"></td>
        <td style="text-align:left;">Broker Dealer</td>
      </tr>
      <tr>
        <td style="text-align:left;">Teaching Mens Fashion</td>
        <td><input type="text" class="value-input" data-id="kQVDEhI1zN4hKJyOfNWW" placeholder="$"></td>
        <td style="text-align:left;">Broker Dealer</td>
      </tr>
      <tr class="bd-row">
        <td style="text-align:left;">Clever Capital Fund</td>
        <td><input type="text" class="value-input" data-id="m46wiNn9Uvht1sqxVGhL" placeholder="$"></td>
        <td style="text-align:left;">Broker Dealer</td>
      </tr>
      <tr>
        <td style="text-align:left;">Baymahni</td>
        <td><input type="text" class="value-input" data-id="RM0racM0bT8M4dY5I2LU" placeholder="$"></td>
        <td style="text-align:left;" class="empty-field">—</td>
      </tr>
    </tbody>
  </table>
</section>

<section>
  <div class="section-title">Notes</div>
  <div class="caveat" style="margin-top: 0; border-top: none; padding-top: 0;">
    <strong style="color: var(--text);">July at a Glance —</strong> MGL Leads/CPL match the fully-reviewed
    Leads by Source donut below (source in MGL/FORM/"Meta Survey - Capital Raising", plus the
    manually-resolved contact-level and booking-artifact cases from the 2026-08-03 review), created-date in
    July, "Test Lead Axiskey" dummy record excluded -- 63 of 99 real leads. Discovery Calls = unique prospects
    with a Discovery Call appointment dated in July, deduped across mirrored calendar bookings; excludes 7
    events on Alex Zinny's Personal Calendar that were mis-tagged to a rep's own contact record instead of the
    real prospect (2 of which were clearly internal meetings, not prospect calls). Deals Won = opportunities
    marked won with a Date Won in July, matching the All-Time Won tab.
  </div>
  <div class="caveat">
    <strong style="color: var(--text);">Funnel —</strong> Overall Leads → Won: 13.1% (13 of 99). Total Leads
    matches the fully-reviewed Leads by Source donut (99, "Test Lead Axiskey" dummy record excluded). Bar
    widths are for visual taper only, not exact area-proportional to volume. "Discovery Calls Booked" here is
    total call volume (85 deduped calendar events, minus 2 clearly-internal meetings misfiled on the Discovery
    Call calendar) -- not the same as the "Discovery Calls" tile above, which counts 63 unique prospects
    reached (a lead getting 2 calls via reschedule counts once there, twice here). These are period counts,
    not a strict single cohort: "Discovery Calls Booked" and "Won Deals" can include contacts whose lead
    entered the pipeline in an earlier month, so these conversion rates are directional, not a precise
    same-cohort conversion rate.
  </div>
  <div class="caveat">
    <strong style="color: var(--text);">Deal Source Distribution —</strong> Same channel buckets as the
    Overview tab's Signings by Source chart (SGL/Referral/MGL/Client/Other), "Referral - &lt;name&gt;"
    variants folded into one Referral slice. Leads by Source falls back to the contact's own "Source" custom
    field when the deal's own source is blank, treats any "AxisKey Discovery Call"-style value (a
    booking-calendar artifact, not a real source) as MGL, and treats a bare referrer name (e.g. "Kirt", "Rob
    Saracco") as Referral. Fully manually reviewed 2026-08-03 -- every lead resolved to a real channel except
    "Test Lead Axiskey", a dummy test record excluded entirely, leaving 99 -- the same total used in the
    funnel and MGL Leads tile above. Won Deals by Source updates live if you edit an Agreement Signed date on
    the All-Time Won tab in a way that moves a deal into or out of July; Teaching Mens Fashion's source was
    backfilled on the contact record 2026-08-03.
  </div>
  <div class="caveat">
    <strong style="color: var(--text);">Won Deals — Month over Month —</strong> Same May/June/July won-deal
    counts as the Overview tab's Signings bar chart, classified by Agreement Signed date where set, otherwise
    Date Won; updates live from the same edits. % badge is growth vs. the prior month; May has none since
    there's no April data in this system.
  </div>
  <div class="caveat">
    <strong style="color: var(--text);">Won Deals —</strong> Blue-highlighted rows (RAS Technologies, Clever
    Capital Fund, IPC - Reg D) are the deals confirmed as exclusively Broker Dealer -- a manual call, not
    derived from the Capital Raiser Intent field: IPC's field was actually blank until confirmed manually,
    while 5 other deals show "Broker Dealer" in that field but aren't exclusively BD engagements. Rows ordered
    by Date Won, matching the All-Time Won tab. Value is a live input, typed directly into the browser -- it
    auto-saves to this browser's localStorage on change, so it survives a normal refresh of this same file.
    It will NOT carry over to a different browser, a different device, or if browsing data for this file's
    location gets cleared, and file:// pages can behave inconsistently across browsers for local storage --
    so for anything you can't afford to retype, copy the entered values back into this HTML file's
    &lt;input value="..."&gt; attributes as a permanent backup.
  </div>
</section>
</div>
"""

# ============ WHITEBOARD_DATA_SCRIPT (f-string, json.dumps injections) ============
WHITEBOARD_DATA_SCRIPT = f"""
<div id="tooltip"></div>

<script>
const MONTHS = {json.dumps(whiteboard_month_keys)};
const MONTH_LABEL = {json.dumps(whiteboard_month_labels)};
const CURRENT_MONTH_KEY = {json.dumps(whiteboard_current_month_key)};
const LEADS_BY_MONTH = {json.dumps(whiteboard_leads_by_month)};
const SPEND_BY_MONTH = {json.dumps(whiteboard_meta_by_month)};
const WON_BY_MONTH = {json.dumps(dict(whiteboard_won_by_month))};
const MGL_WON_BY_MONTH = {json.dumps(dict(whiteboard_mgl_won_by_month))};
const ALLTIME = {{
  spend: {meta_campaign_spend}, won: {won_onboarding_total}, mglWon: {mgl_won_total},
  cps: {blended_cost_per_signing}, cpsMgl: {blended_cost_per_signing_mgl},
}};
const WON = {json.dumps(whiteboard_won)};
// Fixed anchor for the frozen July tab -- always means July 2026, not a
// rolling "3rd month" -- so it stays correct no matter how many months
// MONTHS grows to cover. JULY_LEADS_BY_SOURCE (also frozen, no live
// equivalent for a per-lead source breakdown that old) is declared in
// WHITEBOARD_LOGIC_SCRIPT alongside the rest of the donut-drawing code.
const JULY_KEY = "2026-07";
</script>
"""

# ============ WHITEBOARD_LOGIC_SCRIPT (plain string, no interpolation) ============
WHITEBOARD_LOGIC_SCRIPT = """
<script>
function fmtMoney(n) {
  return "$" + n.toLocaleString("en-US", { maximumFractionDigits: 0 });
}
function fmtMoney2(n) {
  return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}


function effectiveDate(w) { return w.agreement_signed || w.date_won; }

function computeSigningsByMonth(months = MONTHS) {
  const counts = {};
  months.forEach(m => { counts[m] = 0; });
  WON.forEach(w => {
    const m = effectiveDate(w).slice(0, 7);
    if (m !== undefined && counts.hasOwnProperty(m)) counts[m] += 1;
  });
  return counts;
}


// ── Derived per-month + totals (all months, May through current) ──────────
let derived = {}, totals = {}, recent = {}, recentMonths = [], BASIS = {}, BASIS_LABEL = {};

function recomputeDerived() {
  const signingsByMonth = computeSigningsByMonth();
  const sourceByMonth = computeSourceByMonth();
  derived = {};
  MONTHS.forEach(m => {
    const spend = SPEND_BY_MONTH[m] || 0;
    const leads = LEADS_BY_MONTH[m] || 0;
    const signings = signingsByMonth[m] || 0;
    const mglSignings = (sourceByMonth[m] || {}).MGL || 0;
    derived[m] = {
      spend, leads, signings, mglSignings,
      cpl:    leads > 0 ? spend / leads : 0,
      cps:    signings > 0 ? spend / signings : 0,
      cpsMgl: mglSignings > 0 ? spend / mglSignings : 0,
      conv:   leads > 0 ? signings / leads : 0,
    };
  });

  totals = MONTHS.reduce((acc, m) => {
    acc.spend += derived[m].spend;
    acc.leads += derived[m].leads;
    acc.signings += derived[m].signings;
    return acc;
  }, { spend: 0, leads: 0, signings: 0 });
  totals.cpl = totals.leads > 0 ? totals.spend / totals.leads : 0;
  totals.cps = totals.signings > 0 ? totals.spend / totals.signings : 0;

  // "Recent" = last 2 COMPLETE months (excludes the current in-progress
  // month) -- used for the hero CPL and the simulator's default basis, same
  // intent as this file's original Jun+Jul basis right after July closed.
  const completeMonths = MONTHS.filter(m => m !== CURRENT_MONTH_KEY);
  recentMonths = completeMonths.slice(-2);
  recent = recentMonths.reduce((acc, m) => {
    acc.spend += derived[m].spend;
    acc.leads += derived[m].leads;
    acc.signings += derived[m].signings;
    return acc;
  }, { spend: 0, leads: 0, signings: 0 });
  recent.cpl = recent.leads > 0 ? recent.spend / recent.leads : 0;
  recent.cps = recent.signings > 0 ? recent.spend / recent.signings : 0;
  recent.conv = recent.leads > 0 ? recent.signings / recent.leads : 0;

  BASIS = {};
  BASIS_LABEL = {};
  MONTHS.forEach(m => {
    BASIS[m] = { cpl: derived[m].cpl, conv: derived[m].conv };
    BASIS_LABEL[m] = MONTH_LABEL[m];
  });
  BASIS["recent"] = { cpl: recent.cpl, conv: recent.conv };
  BASIS_LABEL["recent"] = recentMonths.map(m => MONTH_LABEL[m]).join("+");
}

// ── Render Overview (hero, tiles, table, bar charts) ───────────────────────
function renderOverview() {
  document.getElementById("heroCPL").textContent = fmtMoney2(recent.cpl);
  document.getElementById("heroCPLLabel").textContent = `Blended cost per lead (${BASIS_LABEL["recent"]})`;
  document.getElementById("heroNote").textContent =
    `Based on ${recent.leads} leads from ${fmtMoney(recent.spend)} of spend across ${recentMonths.map(m => MONTH_LABEL[m]).join(" + ")}.`;
  document.getElementById("totSpend").textContent = fmtMoney(totals.spend);
  document.getElementById("totLeads").textContent = totals.leads.toLocaleString();
  document.getElementById("totSignings").textContent = totals.signings.toLocaleString();
  document.getElementById("totCPL").textContent = fmtMoney2(totals.cpl);
  document.getElementById("totCPS").textContent = fmtMoney(ALLTIME.cps);
  document.getElementById("totCPSFormula").textContent =
    `${fmtMoney2(ALLTIME.spend)} spend since Apr 2026 \u00f7 ${ALLTIME.won} won`;
  document.getElementById("totCPSMgl").textContent = fmtMoney(ALLTIME.cpsMgl);
  document.getElementById("totCPSMglFormula").textContent =
    `${fmtMoney2(ALLTIME.spend)} spend since Apr 2026 \u00f7 ${ALLTIME.mglWon} MGL won`;

  const tbody = document.getElementById("kpiTableBody");
  tbody.innerHTML = "";
  MONTHS.forEach(m => {
    const d = derived[m];
    const tr = document.createElement("tr");
    const label = MONTH_LABEL[m] + (m === CURRENT_MONTH_KEY ? " (MTD)" : "");
    const cells = [
      label,
      fmtMoney2(d.spend),
      d.leads.toLocaleString(),
      d.signings.toLocaleString(),
      d.leads > 0 ? fmtMoney2(d.cpl) : "\u2014",
      d.signings > 0 ? fmtMoney(d.cps) : "\u2014",
    ];
    cells.forEach((c, i) => {
      const td = document.createElement("td");
      td.textContent = c;
      if (i === 3) td.className = "hero-color";
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });

  drawBarChart("chartSpend", "spend", v => fmtMoney(v), "Spend");
  drawBarChart("chartLeads", "leads", v => v.toLocaleString(), "Leads");
  drawBarChart("chartSignings", "signings", v => v.toLocaleString(), "Signings");
  drawSourceChart();
  drawCPSChart();

  // July tab's "Won Deals — Month over Month" mini-cards -- always the
  // first 3 months (May/June/July), a fixed historical view rather than a
  // rolling window, since this whole tab is specifically about July.
  MONTHS.slice(0, 3).forEach((m, i) => {
    const val = derived[m].signings;
    document.getElementById(`momLabel${i}`).textContent = MONTH_LABEL[m];
    document.getElementById(`momValue${i}`).textContent = val;
    const deltaEl = document.getElementById(`momDelta${i}`);
    const prevMonth = i > 0 ? MONTHS[i - 1] : null;
    const prevVal = prevMonth ? derived[prevMonth].signings : null;
    if (prevVal) {
      const pct = Math.round((val - prevVal) / prevVal * 100);
      deltaEl.innerHTML = `won deals &middot; <span style="color:var(--hero);font-weight:700;">${pct >= 0 ? "+" : ""}${pct}%</span>`;
    } else {
      deltaEl.textContent = "won deals";
    }
  });

  drawGenericDonut("chartJulyLeadsSource", "julyLeadsLegend", JULY_LEADS_BY_SOURCE, "leads");
  drawGenericDonut("chartJulyWonSource", "julyWonLegend", computeSourceByMonth()[JULY_KEY] || {}, "won");
}

// ── Signings by source (stacked bar) ───────────────────────────────────────
// "Referral - <name>" variants fold into one "Referral" channel — the
// specific referrer isn't a channel distinction for this chart (still visible
// per-deal on the All-Time Won tab).
const CHANNELS = [
  { key: "SGL",      label: "SGL",      color: "var(--series-1)" },
  { key: "Referral", label: "Referral", color: "var(--series-2)" },
  { key: "MGL",      label: "MGL",      color: "var(--series-3)" },
  { key: "client",   label: "Client",   color: "var(--series-4)" },
];
const OTHER_CHANNEL = { key: "Other", label: "Other", color: "var(--series-other)" };

// Bare referrer names (no "Referral - " prefix) that should still count as
// Referral -- found reviewing July's Other bucket 2026-08-03 (e.g. Fincera
// Series 1's source is literally "David Allen", a person, not a category).
const REFERRAL_NAME_HINTS = ["kirt", "rob saracco", "david allen"];

function sourceChannel(source) {
  if (!source) return "Other";
  if (source.startsWith("Referral")) return "Referral";
  if (source === "SGL") return "SGL";
  if (source === "MGL") return "MGL";
  if (source === "client") return "client";
  const lower = source.toLowerCase();
  if (REFERRAL_NAME_HINTS.some(n => lower.includes(n))) return "Referral";
  return "Other";
}

function computeSourceByMonth(months = MONTHS) {
  const result = {};
  months.forEach(m => { result[m] = {}; });
  WON.forEach(w => {
    const m = effectiveDate(w).slice(0, 7);
    if (!m || !result.hasOwnProperty(m)) return;
    const ch = sourceChannel(w.source);
    result[m][ch] = (result[m][ch] || 0) + 1;
  });
  return result;
}

// legend built once — channel set/order is fixed regardless of data
const sourceLegendEl = document.getElementById("sourceLegend");
CHANNELS.concat([OTHER_CHANNEL]).forEach(ch => {
  const item = document.createElement("span");
  item.className = "legend-item";
  const swatch = document.createElement("span");
  swatch.className = "legend-swatch-rect";
  swatch.style.background = ch.color;
  const label = document.createElement("span");
  label.textContent = ch.label;
  item.appendChild(swatch);
  item.appendChild(label);
  sourceLegendEl.appendChild(item);
});

// month selector for the pie (built once; "All" combines May–Jul)
let sourceMonthSelection = "All";
const sourceMonthToggle = document.getElementById("sourceMonthToggle");
["All", ...MONTHS].forEach(key => {
  const btn = document.createElement("button");
  btn.textContent = key === "All" ? "All" : MONTH_LABEL[key];
  btn.style.fontSize = "0.68rem";
  btn.style.padding = "6px 8px";
  if (key === sourceMonthSelection) btn.classList.add("active");
  btn.addEventListener("click", () => {
    sourceMonthSelection = key;
    [...sourceMonthToggle.children].forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    drawSourceChart();
  });
  sourceMonthToggle.appendChild(btn);
});

function polarPoint(cx, cy, r, angleDeg) {
  const rad = (angleDeg - 90) * Math.PI / 180;
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
}

function donutSlicePath(cx, cy, rOuter, rInner, startAngle, endAngle) {
  const startOuter = polarPoint(cx, cy, rOuter, endAngle);
  const endOuter = polarPoint(cx, cy, rOuter, startAngle);
  const startInner = polarPoint(cx, cy, rInner, endAngle);
  const endInner = polarPoint(cx, cy, rInner, startAngle);
  const largeArc = endAngle - startAngle > 180 ? "1" : "0";
  return `M${startOuter.x},${startOuter.y} A${rOuter},${rOuter} 0 ${largeArc} 0 ${endOuter.x},${endOuter.y} `
    + `L${endInner.x},${endInner.y} A${rInner},${rInner} 0 ${largeArc} 1 ${startInner.x},${startInner.y} Z`;
}

// ── Generic donut (July Leads/Won-by-source) — reuses CHANNELS/donutSlicePath
const JULY_LEADS_BY_SOURCE = { MGL: 63, SGL: 13, Referral: 16, client: 7, Other: 0 };

function buildChannelLegend(containerId) {
  const box = document.getElementById(containerId);
  CHANNELS.concat([OTHER_CHANNEL]).forEach(ch => {
    const item = document.createElement("span");
    item.className = "legend-item";
    const swatch = document.createElement("span");
    swatch.className = "legend-swatch-rect";
    swatch.style.background = ch.color;
    const label = document.createElement("span");
    label.textContent = ch.label;
    item.appendChild(swatch);
    item.appendChild(label);
    box.appendChild(item);
  });
}
buildChannelLegend("julyLeadsLegend");
buildChannelLegend("julyWonLegend");

function drawGenericDonut(svgId, legendId, data, centerLabel) {
  const svg = document.getElementById(svgId);
  svg.innerHTML = "";
  const total = Object.values(data).reduce((a, b) => a + b, 0);
  const cx = 120, cy = 92, rOuter = 66, rInner = 38;

  const centerVal = el("text", { x: cx, y: cy - 2, "text-anchor": "middle", class: "bar-value" });
  centerVal.style.fontSize = "20px";
  centerVal.textContent = total;
  const centerLbl = el("text", { x: cx, y: cy + 14, "text-anchor": "middle", class: "bar-month" });
  centerLbl.textContent = centerLabel;

  if (total === 0) {
    svg.appendChild(el("circle", { cx, cy, r: rOuter, fill: "none", stroke: "var(--line)", "stroke-width": 1 }));
    svg.appendChild(centerVal);
    svg.appendChild(centerLbl);
    return;
  }

  const orderedKeys = CHANNELS.map(c => c.key).concat([OTHER_CHANNEL.key]).filter(k => data[k] > 0);
  let angle = 0;
  const GAP_DEG = orderedKeys.length > 1 ? 2 : 0;

  orderedKeys.forEach(key => {
    const val = data[key];
    const chDef = CHANNELS.find(c => c.key === key) || OTHER_CHANNEL;
    const sweep = (val / total) * 360;
    const startAngle = angle + GAP_DEG / 2;
    const endAngle = angle + sweep - GAP_DEG / 2;

    const mark = el("path", {
      d: donutSlicePath(cx, cy, rOuter, rInner, startAngle, endAngle),
      fill: chDef.color, class: "seg-mark"
    });
    svg.appendChild(mark);
    mark.addEventListener("pointerenter", () => mark.classList.add("seg-hover"));
    mark.addEventListener("pointerleave", () => { mark.classList.remove("seg-hover"); hideTooltip(); });
    mark.addEventListener("pointermove", (e) => {
      const pct = Math.round((val / total) * 100);
      showTooltip(e.clientX, e.clientY, [[chDef.label, `${val} (${pct}%)`]]);
    });
    mark.style.cursor = "pointer";

    angle += sweep;
  });

  svg.appendChild(centerVal);
  svg.appendChild(centerLbl);
}


function drawSourceChart() {
  const svg = document.getElementById("chartSourceBreakdown");
  svg.innerHTML = "";
  const byMonth = computeSourceByMonth();

  let data;
  if (sourceMonthSelection === "All") {
    data = {};
    MONTHS.forEach(m => {
      Object.entries(byMonth[m]).forEach(([k, v]) => { data[k] = (data[k] || 0) + v; });
    });
  } else {
    data = byMonth[sourceMonthSelection];
  }

  const total = Object.values(data).reduce((a, b) => a + b, 0);
  const cx = 120, cy = 92, rOuter = 66, rInner = 38;

  const centerVal = el("text", { x: cx, y: cy - 2, "text-anchor": "middle", class: "bar-value" });
  centerVal.style.fontSize = "20px";
  centerVal.textContent = total;
  const centerLabel = el("text", { x: cx, y: cy + 14, "text-anchor": "middle", class: "bar-month" });
  centerLabel.textContent = "signings";

  if (total === 0) {
    svg.appendChild(el("circle", { cx, cy, r: rOuter, fill: "none", stroke: "var(--line)", "stroke-width": 1 }));
    svg.appendChild(centerVal);
    svg.appendChild(centerLabel);
    return;
  }

  const orderedKeys = CHANNELS.map(c => c.key).concat([OTHER_CHANNEL.key]).filter(k => data[k] > 0);
  let angle = 0;
  const GAP_DEG = orderedKeys.length > 1 ? 2 : 0;

  orderedKeys.forEach(key => {
    const val = data[key];
    const chDef = CHANNELS.find(c => c.key === key) || OTHER_CHANNEL;
    const sweep = (val / total) * 360;
    const startAngle = angle + GAP_DEG / 2;
    const endAngle = angle + sweep - GAP_DEG / 2;

    const mark = el("path", {
      d: donutSlicePath(cx, cy, rOuter, rInner, startAngle, endAngle),
      fill: chDef.color, class: "seg-mark"
    });
    svg.appendChild(mark);

    mark.addEventListener("pointerenter", () => mark.classList.add("seg-hover"));
    mark.addEventListener("pointerleave", () => { mark.classList.remove("seg-hover"); hideTooltip(); });
    mark.addEventListener("pointermove", (e) => {
      const pct = Math.round((val / total) * 100);
      showTooltip(e.clientX, e.clientY, [[chDef.label, `${val} (${pct}%)`]]);
    });
    mark.style.cursor = "pointer";

    angle += sweep;
  });

  svg.appendChild(centerVal);
  svg.appendChild(centerLabel);
}


// ── Tooltip helper ────────────────────────────────────────────────────────
const tooltip = document.getElementById("tooltip");
function showTooltip(x, y, rows) {
  tooltip.innerHTML = "";
  rows.forEach(([label, value]) => {
    const row = document.createElement("div");
    const vSpan = document.createElement("span");
    vSpan.className = "tt-value";
    vSpan.textContent = value;
    const lSpan = document.createElement("span");
    lSpan.className = "tt-label";
    lSpan.textContent = " " + label;
    row.appendChild(vSpan);
    row.appendChild(lSpan);
    tooltip.appendChild(row);
  });
  tooltip.style.display = "block";
  tooltip.style.left = (x + 14) + "px";
  tooltip.style.top = (y + 14) + "px";
}
function hideTooltip() { tooltip.style.display = "none"; }


// ── Small-multiple bar chart builder ──────────────────────────────────────
const SVG_NS = "http://www.w3.org/2000/svg";
function el(tag, attrs) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}


function drawBarChart(svgId, key, formatter, tooltipLabel) {
  const svg = document.getElementById(svgId);
  svg.innerHTML = "";
  const W = 240, H = 160, padTop = 14, padBottom = 26, padSide = 16;
  const plotH = H - padTop - padBottom;
  const maxVal = Math.max(...MONTHS.map(m => derived[m][key])) * 1.15;
  const barW = 24, gap = (W - padSide * 2 - barW * MONTHS.length) / (MONTHS.length - 1 + 1.5);

  // gridline (baseline)
  svg.appendChild(el("line", {
    x1: padSide, x2: W - padSide, y1: H - padBottom, y2: H - padBottom,
    class: "gridline"
  }));

  MONTHS.forEach((m, i) => {
    const val = derived[m][key];
    const barH = (val / maxVal) * plotH;
    const x = padSide + i * (barW + gap) + gap / 2;
    const y = H - padBottom - barH;

    const rect = el("rect", {
      x, y, width: barW, height: barH, rx: 4,
      class: "bar-rect"
    });
    svg.appendChild(rect);

    // value label above bar
    const label = el("text", {
      x: x + barW / 2, y: y - 6, "text-anchor": "middle", class: "bar-value"
    });
    label.textContent = formatter(val);
    svg.appendChild(label);

    // month label
    const monthLabel = el("text", {
      x: x + barW / 2, y: H - 8, "text-anchor": "middle", class: "bar-month"
    });
    monthLabel.textContent = MONTH_LABEL[m];
    svg.appendChild(monthLabel);

    // hit target
    const hit = el("rect", {
      x: x - 4, y: padTop, width: barW + 8, height: H - padTop - padBottom,
      class: "bar-hit"
    });
    hit.addEventListener("pointerenter", () => rect.classList.add("hover"));
    hit.addEventListener("pointerleave", () => { rect.classList.remove("hover"); hideTooltip(); });
    hit.addEventListener("pointermove", (e) => {
      showTooltip(e.clientX, e.clientY, [[tooltipLabel, formatter(val)], ["Month", MONTH_LABEL[m]]]);
    });
    svg.appendChild(hit);
  });
}


// ── Cost / Signing toggle chart — Overall vs. MGL ──────────────────────────
// Toggled rather than shown together -- Overall and MGL sit on very
// different scales, so sharing one axis flattens the Overall line. Each
// selection gets its own y-axis. A month with 0 signings for a series is
// skipped (cost/signing is undefined at zero) rather than plotted as 0.
const CPS_SERIES = {
  Overall: { key: "cps",    color: "var(--hero)",     signingsKey: "signings",    signingsLabel: "Signings" },
  MGL:     { key: "cpsMgl", color: "var(--series-3)", signingsKey: "mglSignings", signingsLabel: "MGL signings" },
};
let cpsSelection = "Overall";

const cpsToggle = document.getElementById("cpsToggle");
Object.keys(CPS_SERIES).forEach(key => {
  const btn = document.createElement("button");
  btn.textContent = key;
  if (key === cpsSelection) btn.classList.add("active");
  btn.addEventListener("click", () => {
    cpsSelection = key;
    [...cpsToggle.children].forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    drawCPSChart();
  });
  cpsToggle.appendChild(btn);
});

function drawCPSChart() {
  const svg = document.getElementById("chartCPS");
  svg.innerHTML = "";
  const { key, color, signingsKey, signingsLabel } = CPS_SERIES[cpsSelection];
  const W = 720, H = 180, padT = 18, padB = 30, padL = 60, padR = 20;
  const x = i => padL + (i / (MONTHS.length - 1)) * (W - padL - padR);
  const yMax = Math.max(...MONTHS.map(m => derived[m][key]), 1) * 1.15;
  const y = v => H - padB - (v / yMax) * (H - padT - padB);

  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const val = (yMax / ticks) * i;
    const ty = y(val);
    svg.appendChild(el("line", { x1: padL, x2: W - padR, y1: ty, y2: ty, class: "gridline" }));
    const t = el("text", { x: padL - 8, y: ty + 4, "text-anchor": "end", class: "bar-month" });
    t.textContent = fmtMoney(val);
    svg.appendChild(t);
  }

  MONTHS.forEach((m, i) => {
    const t = el("text", { x: x(i), y: H - 8, "text-anchor": "middle", class: "bar-month" });
    t.textContent = MONTH_LABEL[m] + (m === CURRENT_MONTH_KEY ? "*" : "");
    svg.appendChild(t);
  });

  const pts = MONTHS.map((m, i) => ({ m, i, val: derived[m][key] })).filter(p => p.val > 0);
  if (pts.length >= 2) {
    const d = pts.map((p, idx) => `${idx === 0 ? "M" : "L"}${x(p.i)},${y(p.val)}`).join(" ");
    svg.appendChild(el("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linecap": "round" }));
  }
  pts.forEach(p => {
    const cx = x(p.i), cy = y(p.val);
    svg.appendChild(el("circle", { cx, cy, r: 6, fill: "var(--surface)" }));
    svg.appendChild(el("circle", { cx, cy, r: 4, fill: color }));
    const hit = el("circle", { cx, cy, r: 14, fill: "transparent", style: "cursor:pointer" });
    hit.addEventListener("pointermove", (e) => {
      showTooltip(e.clientX, e.clientY, [
        [p.m === CURRENT_MONTH_KEY ? `${MONTH_LABEL[p.m]} (month to date)` : MONTH_LABEL[p.m], fmtMoney(p.val)],
        [signingsLabel, derived[p.m][signingsKey]],
      ]);
    });
    hit.addEventListener("pointerleave", hideTooltip);
    svg.appendChild(hit);
  });
}

// ── Initial compute + render (Overview depends on WON, so this must run
// before the basis toggle reads BASIS's keys) ──────────────────────────────
recomputeDerived();
renderOverview();

// ── Simulator ─────────────────────────────────────────────────────────────
let currentBasis = "recent";

const basisToggle = document.getElementById("basisToggle");
Object.keys(BASIS).forEach(key => {
  const btn = document.createElement("button");
  btn.textContent = BASIS_LABEL[key];
  if (key === currentBasis) btn.classList.add("active");
  btn.addEventListener("click", () => {
    currentBasis = key;
    [...basisToggle.children].forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    updateSim();
    drawSimChart();
  });
  basisToggle.appendChild(btn);
});

const spendSlider = document.getElementById("spendSlider");
const spendValueEl = document.getElementById("spendValue");

function updateSim() {
  const spend = Number(spendSlider.value);
  const basis = BASIS[currentBasis];
  const leads = basis.cpl > 0 ? spend / basis.cpl : 0;
  const signings = leads * basis.conv;
  const cps = signings > 0 ? spend / signings : 0;

  spendValueEl.textContent = fmtMoney(spend);
  document.getElementById("predLeads").textContent = leads.toFixed(1);
  document.getElementById("predSignings").textContent = signings.toFixed(1);
  document.getElementById("predCPS").textContent = signings > 0 ? fmtMoney(cps) : "\u2014";
  document.getElementById("predCPLUsed").textContent = fmtMoney2(basis.cpl);
  drawMarker();
}

// ── Simulator line chart ──────────────────────────────────────────────────
const simSvg = document.getElementById("chartSim");
const SIM_W = 800, SIM_H = 260, SIM_PAD_L = 50, SIM_PAD_R = 20, SIM_PAD_T = 16, SIM_PAD_B = 34;
const SIM_MAX_SPEND = 15000;

function simX(spend) {
  return SIM_PAD_L + (spend / SIM_MAX_SPEND) * (SIM_W - SIM_PAD_L - SIM_PAD_R);
}
function simY(leads, maxLeads) {
  return SIM_H - SIM_PAD_B - (leads / maxLeads) * (SIM_H - SIM_PAD_T - SIM_PAD_B);
}

let markerGroup = null;

function drawSimChart() {
  simSvg.innerHTML = "";
  const basis = BASIS[currentBasis];
  const maxLeadsOnLine = SIM_MAX_SPEND / basis.cpl;
  const maxActualLeads = Math.max(...MONTHS.map(m => derived[m].leads));
  const maxLeads = Math.max(maxLeadsOnLine, maxActualLeads) * 1.1;

  // gridlines (y-axis ticks)
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const val = (maxLeads / ticks) * i;
    const y = simY(val, maxLeads);
    simSvg.appendChild(el("line", { x1: SIM_PAD_L, x2: SIM_W - SIM_PAD_R, y1: y, y2: y, class: "gridline" }));
    const t = el("text", { x: SIM_PAD_L - 8, y: y + 4, "text-anchor": "end", class: "bar-month" });
    t.textContent = Math.round(val);
    simSvg.appendChild(t);
  }
  // x-axis label ticks
  [0, 5000, 10000, 15000].forEach(v => {
    const x = simX(v);
    const t = el("text", { x, y: SIM_H - 10, "text-anchor": "middle", class: "bar-month" });
    t.textContent = "$" + (v / 1000) + "K";
    simSvg.appendChild(t);
  });

  // projected line
  const x0 = simX(0), y0 = simY(0, maxLeads);
  const x1 = simX(SIM_MAX_SPEND), y1 = simY(maxLeadsOnLine, maxLeads);
  simSvg.appendChild(el("line", {
    x1: x0, y1: y0, x2: x1, y2: y1,
    stroke: "var(--hero)", "stroke-width": 2, "stroke-linecap": "round"
  }));

  // actual month dots
  MONTHS.forEach(m => {
    const d = derived[m];
    const cx = simX(d.spend), cy = simY(d.leads, maxLeads);
    const ring = el("circle", { cx, cy, r: 6, fill: "var(--surface)" });
    const dot = el("circle", { cx, cy, r: 4, fill: "var(--text-mute)" });
    simSvg.appendChild(ring);
    simSvg.appendChild(dot);

    const hit = el("circle", { cx, cy, r: 14, fill: "transparent", style: "cursor:pointer" });
    hit.addEventListener("pointermove", (e) => {
      showTooltip(e.clientX, e.clientY, [
        [MONTH_LABEL[m], `${fmtMoney(d.spend)} spend`],
        ["Leads", d.leads], ["Signings", d.signings], ["CPL", fmtMoney2(d.cpl)]
      ]);
    });
    hit.addEventListener("pointerleave", hideTooltip);
    simSvg.appendChild(hit);
  });

  // hover crosshair across whole chart
  const hitArea = el("rect", {
    x: SIM_PAD_L, y: SIM_PAD_T, width: SIM_W - SIM_PAD_L - SIM_PAD_R, height: SIM_H - SIM_PAD_T - SIM_PAD_B,
    fill: "transparent", style: "cursor:crosshair"
  });
  hitArea.addEventListener("pointermove", (e) => {
    const rect = simSvg.getBoundingClientRect();
    const scaleX = SIM_W / rect.width;
    const svgX = (e.clientX - rect.left) * scaleX;
    const spend = Math.max(0, Math.min(SIM_MAX_SPEND, ((svgX - SIM_PAD_L) / (SIM_W - SIM_PAD_L - SIM_PAD_R)) * SIM_MAX_SPEND));
    const leads = spend / basis.cpl;
    showTooltip(e.clientX, e.clientY, [
      [`at ${fmtMoney(spend)} spend`, `${leads.toFixed(1)} leads`]
    ]);
  });
  hitArea.addEventListener("pointerleave", hideTooltip);
  simSvg.appendChild(hitArea);

  markerGroup = el("g", {});
  simSvg.appendChild(markerGroup);
  drawMarker();
}

function drawMarker() {
  if (!markerGroup) return;
  markerGroup.innerHTML = "";
  const spend = Number(spendSlider.value);
  const basis = BASIS[currentBasis];
  const maxLeadsOnLine = SIM_MAX_SPEND / basis.cpl;
  const maxActualLeads = Math.max(...MONTHS.map(m => derived[m].leads));
  const maxLeads = Math.max(maxLeadsOnLine, maxActualLeads) * 1.1;
  const leads = spend / basis.cpl;
  const x = simX(spend), y = simY(leads, maxLeads);

  markerGroup.appendChild(el("line", {
    x1: x, x2: x, y1: SIM_PAD_T, y2: SIM_H - SIM_PAD_B,
    stroke: "var(--text-mute)", "stroke-width": 1, "stroke-dasharray": "3,3"
  }));
  const ring = el("circle", { cx: x, cy: y, r: 7, fill: "var(--surface)" });
  const dot = el("circle", { cx: x, cy: y, r: 5, fill: "var(--hero)" });
  markerGroup.appendChild(ring);
  markerGroup.appendChild(dot);
}


spendSlider.addEventListener("input", updateSim);

updateSim();
drawSimChart();

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.tab).classList.add("active");
  });
});


// ── All-time won list (live from GHL -- Agreement Signed / Capital Raiser
// Intent are read-only display now; edit them on the opportunity in GHL) ──
function renderWonTable() {
  const wonBody = document.getElementById("wonTableBody");
  wonBody.innerHTML = "";
  WON.forEach(w => {
    const tr = document.createElement("tr");

    const dateTd = document.createElement("td");
    dateTd.textContent = w.date_won;
    tr.appendChild(dateTd);

    const nameTd = document.createElement("td");
    nameTd.style.textAlign = "left";
    nameTd.textContent = w.name;
    tr.appendChild(nameTd);

    const valueTd = document.createElement("td");
    valueTd.textContent = fmtMoney2(w.value);
    tr.appendChild(valueTd);

    const ownerTd = document.createElement("td");
    ownerTd.style.textAlign = "left";
    ownerTd.textContent = w.owner;
    tr.appendChild(ownerTd);

    const sourceTd = document.createElement("td");
    sourceTd.style.textAlign = "left";
    if (w.source) {
      sourceTd.textContent = w.source;
    } else {
      sourceTd.textContent = "\u2014";
      sourceTd.className = "empty-field";
    }
    tr.appendChild(sourceTd);

    const agreementTd = document.createElement("td");
    if (w.agreement_signed) {
      agreementTd.textContent = w.agreement_signed;
    } else {
      agreementTd.textContent = w.date_won;
      agreementTd.className = "empty-field";
      agreementTd.title = "Not set on the opportunity in GHL yet -- showing Date Won as a default.";
    }
    tr.appendChild(agreementTd);

    const intentTd = document.createElement("td");
    intentTd.style.textAlign = "left";
    if (w.capital_raiser_intent) {
      intentTd.textContent = w.capital_raiser_intent;
    } else {
      intentTd.textContent = "\u2014";
      intentTd.className = "empty-field";
    }
    tr.appendChild(intentTd);

    wonBody.appendChild(tr);
  });
}
renderWonTable();
document.getElementById("wonTabBtn").textContent = `All-Time Won (${WON.length})`;

// ── July Sales Report — Won Deals Value (editable, saved in this browser) ──
const JULY_VALUE_STORAGE_KEY = "axiskey_kpi_whiteboard_july_won_values";
function loadJulyValueOverrides() {
  try { return JSON.parse(localStorage.getItem(JULY_VALUE_STORAGE_KEY) || "{}"); } catch (e) { return {}; }
}
function saveJulyValueOverride(id, val) {
  const o = loadJulyValueOverrides();
  o[id] = val;
  localStorage.setItem(JULY_VALUE_STORAGE_KEY, JSON.stringify(o));
}
const savedJulyValues = loadJulyValueOverrides();
document.querySelectorAll(".value-input").forEach(input => {
  const id = input.dataset.id;
  if (savedJulyValues[id] !== undefined) input.value = savedJulyValues[id];
  input.addEventListener("change", () => saveJulyValueOverride(id, input.value));
});


// ── SDR Capacity & Lead Target Calculator ──────────────────────────────────
(function () {
  const $ = id => document.getElementById(id);

  const sdrInputs = {
    numSDRs: $("numSDRs"),
    callsPerSDR: $("callsPerSDR"),
    numManagers: $("numManagers"),
    managerPct: $("managerPct"),
    workingDays: $("workingDays"),
    weeksPerMonth: $("weeksPerMonth"),
    conversionRate: $("conversionRate"),
  };

  let scenario = "target"; // "target" (raw math) | "conservative" (floors capacity/day)

  const scenarioToggle = $("scenarioToggle");
  [
    { key: "target", label: "Target (raw math)" },
    { key: "conservative", label: "Conservative (rounds down capacity)" },
  ].forEach(opt => {
    const btn = document.createElement("button");
    btn.textContent = opt.label;
    if (opt.key === scenario) btn.classList.add("active");
    btn.addEventListener("click", () => {
      scenario = opt.key;
      [...scenarioToggle.children].forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      sdrRecalc();
    });
    scenarioToggle.appendChild(btn);
  });

  $("preset4").addEventListener("click", () => {
    sdrInputs.weeksPerMonth.value = 4;
    $("preset4").classList.add("active");
    $("preset433").classList.remove("active");
    sdrRecalc();
  });
  $("preset433").addEventListener("click", () => {
    sdrInputs.weeksPerMonth.value = 4.33;
    $("preset433").classList.add("active");
    $("preset4").classList.remove("active");
    sdrRecalc();
  });
  sdrInputs.weeksPerMonth.addEventListener("input", () => {
    const v = Number(sdrInputs.weeksPerMonth.value);
    $("preset4").classList.toggle("active", v === 4);
    $("preset433").classList.toggle("active", Math.abs(v - 4.33) < 0.001);
  });

  function fmtCalls(n) { return Math.round(n).toLocaleString("en-US"); }
  function fmtContacts(n) { return Math.ceil(n).toLocaleString("en-US"); }
  function fmtRaw(n, decimals = 2) { return n.toLocaleString("en-US", { maximumFractionDigits: decimals }); }

  function sdrRecalc() {
    const numSDRs = Math.max(0, Number(sdrInputs.numSDRs.value) || 0);
    const callsPerSDRRaw = Math.max(0, Number(sdrInputs.callsPerSDR.value) || 0);
    const numManagers = Math.max(0, Number(sdrInputs.numManagers.value) || 0);
    const managerPct = Math.max(0, Number(sdrInputs.managerPct.value) || 0);
    const workingDays = Math.max(1, Number(sdrInputs.workingDays.value) || 1);
    const weeksPerMonth = Math.max(1, Number(sdrInputs.weeksPerMonth.value) || 1);
    const conversionRate = Math.min(100, Math.max(0.01, Number(sdrInputs.conversionRate.value) || 0.01));

    const perSDRDayRaw = callsPerSDRRaw;
    const perSDRDay = scenario === "conservative" ? Math.floor(perSDRDayRaw) : perSDRDayRaw;

    const managerDayRaw = numManagers * (callsPerSDRRaw * (managerPct / 100));
    const managerDay = scenario === "conservative" ? Math.floor(managerDayRaw) : managerDayRaw;

    const sdrRoleDay = numSDRs * perSDRDay;
    const totalDay = sdrRoleDay + managerDay;
    const totalWeek = totalDay * workingDays;
    const totalMonth = totalWeek * weeksPerMonth;

    const conv = conversionRate / 100;
    const contactsMonth = totalMonth / conv;
    const contactsWeek = totalWeek / conv;
    const contactsDay = totalDay / conv;

    const tbody = $("capacityBody");
    tbody.innerHTML = "";

    function addRow(label, day, week, month, isTotal, muted) {
      const tr = document.createElement("tr");
      if (isTotal) tr.className = "total-row";
      const labelTd = document.createElement("td");
      labelTd.textContent = label;
      if (muted) labelTd.className = "role-label";
      tr.appendChild(labelTd);
      [day, week, month].forEach(v => {
        const td = document.createElement("td");
        td.className = "mono";
        td.textContent = fmtCalls(v);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    }

    for (let i = 1; i <= numSDRs; i++) {
      addRow(`SDR ${i}`, perSDRDay, perSDRDay * workingDays, perSDRDay * workingDays * weeksPerMonth, false, true);
    }
    if (numManagers > 0) {
      addRow("Sales Manager (combined)", managerDay, managerDay * workingDays, managerDay * workingDays * weeksPerMonth, false, true);
    }
    addRow("Total calls", totalDay, totalWeek, totalMonth, true, false);

    $("targetMonth").textContent = fmtContacts(contactsMonth);
    $("targetWeek").textContent = fmtContacts(contactsWeek);
    $("targetDay").textContent = fmtContacts(contactsDay);
    $("assumedRate").textContent = conversionRate + "%";

    const steps = $("stepsBody");
    steps.innerHTML = "";
    const lines = [
      `Calls/day per SDR role = ${numSDRs} SDR${numSDRs === 1 ? "" : "s"} × ${fmtRaw(perSDRDay)} calls/day${scenario === "conservative" ? " (floored)" : ""} = <span class="step-val">${fmtRaw(sdrRoleDay)}</span>`,
      `Calls/day for Sales Manager = ${numManagers} manager${numManagers === 1 ? "" : "s"} × (${fmtRaw(callsPerSDRRaw)} × ${managerPct}%)${scenario === "conservative" ? " (floored)" : ""} = <span class="step-val">${fmtRaw(managerDay)}</span>`,
      `Total calls/day = ${fmtRaw(sdrRoleDay)} + ${fmtRaw(managerDay)} = <span class="step-val">${fmtRaw(totalDay)}</span> → rounded: <span class="step-result">${fmtCalls(totalDay)}</span>`,
      `Total calls/week = ${fmtRaw(totalDay)} × ${workingDays} working days = <span class="step-val">${fmtRaw(totalWeek)}</span> → rounded: <span class="step-result">${fmtCalls(totalWeek)}</span>`,
      `Total calls/month = ${fmtRaw(totalWeek)} × ${weeksPerMonth} weeks = <span class="step-val">${fmtRaw(totalMonth)}</span> → rounded: <span class="step-result">${fmtCalls(totalMonth)}</span>`,
      `Required contacts/month = ${fmtRaw(totalMonth)} ÷ ${conversionRate}% = <span class="step-result">${fmtContacts(contactsMonth)}</span> (rounded up)`,
      `Required contacts/week = ${fmtRaw(totalWeek)} ÷ ${conversionRate}% = <span class="step-result">${fmtContacts(contactsWeek)}</span> (rounded up)`,
      `Required contacts/working day = ${fmtRaw(totalDay)} ÷ ${conversionRate}% = <span class="step-result">${fmtContacts(contactsDay)}</span> (rounded up)`,
    ];
    lines.forEach(line => {
      const div = document.createElement("div");
      div.className = "step-line";
      div.innerHTML = line;
      steps.appendChild(div);
    });
  }

  Object.values(sdrInputs).forEach(inp => inp.addEventListener("input", sdrRecalc));
  sdrRecalc();
})();

</script>

</body>
</html>
"""

whiteboard_html = (
    WHITEBOARD_HEAD + WHITEBOARD_HEADER + WHITEBOARD_BODY_OVERVIEW + WHITEBOARD_BODY_WON
    + WHITEBOARD_BODY_SDR + WHITEBOARD_BODY_JULY + WHITEBOARD_DATA_SCRIPT + WHITEBOARD_LOGIC_SCRIPT
)
whiteboard_path = Path(__file__).parent / "kpi_whiteboard.html"
whiteboard_path.write_text(whiteboard_html, encoding="utf-8")
print(f"Whiteboard written → {whiteboard_path} ({len(whiteboard_won)} won deals, {len(whiteboard_month_keys)} months)")



# ─── 8. ASSEMBLE AND WRITE ───────────────────────────────────────────────────
# Join all sections into one string and write axis-growth.html.
# Running this script again will overwrite the file with fresh data.

def _section_group(label, *parts):
    """Wraps one or more existing HTML blocks with a big vertical category
    rail to their left -- purely a presentation wrapper, doesn't touch the
    blocks themselves. Page order is unchanged from before this existed;
    groups just make the existing order's categories explicit."""
    return f"""
  <div class="section-group">
    <div class="section-rail"><span>{label}</span></div>
    <div class="section-group-body">{"".join(parts)}</div>
  </div>
"""

GROUP_MARKETING = _section_group("Marketing &amp; Leads", HERO, SHARED_DATE_HEADER, MGL_CHART)
GROUP_PIPELINE  = _section_group("Sales Pipeline", MIDDLE)
GROUP_ACTIVITY  = _section_group("Sales Activity", STAGE_MOVEMENT, FUNNEL_CONVERSION_SECTION, APPT_WEEKLY_SECTION)
GROUP_ADSPEND   = _section_group("Ad Spend", META_SECTION)
GROUP_INTEL     = _section_group("Call Intelligence", GRANOLA_SECTION)

html     = HEAD + HEADER + GROUP_MARKETING + GROUP_PIPELINE + GROUP_ACTIVITY + GROUP_ADSPEND + GROUP_INTEL + GLOSSARY_MODAL + MONTHLY_MODAL + DATA_SCRIPT + CHARTS_SCRIPT
out_path = Path(__file__).parent / "axis-growth.html"
out_path.write_text(html, encoding="utf-8")

print(f"Dashboard written → {out_path}")
print("  Open with:  open axis-growth.html")
