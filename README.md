# GHL Workspace — Script Reference

All scripts read credentials from `.env` (AXISKEY account only) and are read-only (GET requests only).

---

## weekly_report.py

**Question:** How many deals were marked WON this week, broken down by pipeline?

```
python3 weekly_report.py
```

- Covers Monday through today (current week).
- Prints a table: pipeline name | count won | total monetary value.
- Saves a JSON result to `data/exports/axiskey_won_this_week_YYYY-MM-DD.json`.

---

## new_leads_report.py

**Question:** How many NEW opportunities were created this week, broken down by pipeline?

```
python3 new_leads_report.py
```

- "New" = creation date (`createdAt`), not stage change or won date.
- Covers Monday through today (current week).
- Prints a table: pipeline name | count of new opportunities.
- Saves a CSV to `data/exports/axiskey_new_leads_YYYY-MM-DD.csv`.

---

## snapshot_opportunities.py

**Question:** What does the full pipeline look like right now?

```
python3 snapshot_opportunities.py
```

- Fetches every opportunity across all pipelines and statuses (open, won, lost, abandoned).
- Each record includes stage name, pipeline name, value, status, and key timestamps.
- Saves a JSON file to `data/snapshots/opportunities-AXISKEY-YYYY-MM-DD.json`.
- Run daily to build a history. Diff two snapshots to see what moved, what closed, and what's new.

---

## Data folders

| Folder | What goes there |
|---|---|
| `data/exports/` | One-off query results (weekly reports, lead counts) |
| `data/snapshots/` | Daily opportunity snapshots for movement tracking |
