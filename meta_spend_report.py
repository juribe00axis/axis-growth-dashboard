"""
meta_spend_report.py
Fetches last 7 days of daily spend for the AxisKey Meta Ads account
and injects the data into axis-growth.html.

Run:  python3 meta_spend_report.py
"""

import http.client, json, urllib.parse, re
from datetime import datetime, timedelta, timezone

# ── Load credentials ──────────────────────────────────────────────────────────
env = {}
with open(".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

TOKEN      = env["META_TOKEN_AXISKEY"]
ACCOUNT_ID = "act_2367308470283644"  # AxisKey

# ── Fetch last 7 days of daily spend ─────────────────────────────────────────
today      = datetime.now(timezone.utc).date()
start_date = today - timedelta(days=6)

conn   = http.client.HTTPSConnection("graph.facebook.com")
params = urllib.parse.urlencode({
    "fields":       "date_start,spend,impressions,clicks",
    "time_range":   json.dumps({"since": str(start_date), "until": str(today)}),
    "time_increment": 1,
    "access_token": TOKEN,
})
conn.request("GET", f"/v21.0/{ACCOUNT_ID}/insights?{params}")
res  = conn.getresponse()
data = json.loads(res.read())

if "error" in data:
    print(f"API error: {data['error']}")
    raise SystemExit(1)

rows = sorted(data.get("data", []), key=lambda r: r["date_start"])

# ── Print summary to terminal ─────────────────────────────────────────────────
print(f"\nAxisKey — Daily Spend ({start_date} → {today})\n")
print(f"{'Date':<12} {'Spend':>10} {'Impressions':>13} {'Clicks':>8}")
print("-" * 48)
total = 0.0
for r in rows:
    spend = float(r.get("spend", 0))
    total += spend
    print(f"{r['date_start']:<12} ${spend:>9.2f} {int(r.get('impressions',0)):>13,} {int(r.get('clicks',0)):>8,}")
print("-" * 48)
print(f"{'Total':<12} ${total:>9.2f}\n")

# ── Build JS values ───────────────────────────────────────────────────────────
labels   = [datetime.strptime(r["date_start"], "%Y-%m-%d").strftime("%-m/%-d") for r in rows]
spends   = [float(r.get("spend", 0)) for r in rows]
avg      = total / len(spends) if spends else 0
today_sp = spends[-1] if spends else 0

start_fmt = datetime.strptime(rows[0]["date_start"], "%Y-%m-%d").strftime("%b %-d") if rows else ""
end_fmt   = datetime.strptime(rows[-1]["date_start"], "%Y-%m-%d").strftime("%b %-d") if rows else ""

js_labels = json.dumps(labels)
js_spends = json.dumps(spends)
js_total  = f'"${total:,.0f}"'
js_avg    = f'"${avg:,.0f}"'
js_today  = f'"${today_sp:,.0f}"'
js_range  = f'"{start_fmt} – {end_fmt} · AxisKey"'

# ── Inject into axis-growth.html ──────────────────────────────────────────────
with open("axis-growth.html", "r") as f:
    html = f.read()

def replace_const(html, name, new_value):
    return re.sub(
        rf'(const {name}\s*=\s*).*?(;)',
        rf'\g<1>{new_value}\2',
        html
    )

html = replace_const(html, "META_LABELS", js_labels)
html = replace_const(html, "META_SPEND",  js_spends)
html = replace_const(html, "META_TOTAL",  js_total)
html = replace_const(html, "META_AVG",    js_avg)
html = replace_const(html, "META_TODAY",  js_today)
html = replace_const(html, "META_RANGE",  js_range)

# Update the generated timestamp in the header
now_str = datetime.now(timezone.utc).strftime("Generated %B %-d, %Y at %H:%M UTC")
html = re.sub(r'Generated [^"]+UTC', now_str, html)

with open("axis-growth.html", "w") as f:
    f.write(html)

print("axis-growth.html updated with latest Meta spend data.")
