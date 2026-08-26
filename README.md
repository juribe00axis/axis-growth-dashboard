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

## new_contacts_daily_log.py

**Question:** Which new contacts came in, who owns them, and what source are they from?

```
python3 new_contacts_daily_log.py                                       # yesterday only — the daily trigger
python3 new_contacts_daily_log.py --date 2026-08-09                     # one specific day
python3 new_contacts_daily_log.py --start 2026-08-07 --end 2026-08-10   # backfill a range
```

- "New" = contact creation date (`dateAdded`), bucketed by the location's own local timezone (America/New_York for AXISKEY), not UTC.
- Excludes any contact named "test" (substring match) or literally named "Joncarlo Tamayo" (a dummy/test contact — not to be confused with the rep of the same name in the Owner column).
- `data/exports/axiskey_new_contacts_log.xlsx` is a **hand-curated file** (since 2026-08-12):
  - **New Contacts** sheet — append-only. New contacts (matched by date+name so nothing already deleted comes back) are inserted at the *correct chronological position* (newest first) — fixed 2026-08-18, see bug note below. Every existing row, and any manual fixes to them, is left alone. Every run also rechecks any row still blank on Source or Owner against GHL (those fields sometimes get filled in after the contact is created) and fills them in if now available — added 2026-08-18, only ever fills a blank, never overwrites a value already there.
  - **Joncarlo / Alex / Stormer / Cole** tabs — append-only. Daily activity tracker per rep: Day, Leads (auto-filled/updated by this script for whatever day(s) it just fetched), Discovery Calls, Strategy Calls, Proposal Sent, Agreement Signed (all filled in by hand — column renamed from "Due Diligence Calls" to "Strategy Calls" by the operator at some point). Only the Leads column is ever touched, only for the day(s) just fetched, and it's inserted at the correct chronological position (see bug note below). Cole added 2026-08-24 (joined the team) — adding a 5th rep later just means creating their tab the same way and adding them to `OWNER_TABS` in the script.
  - **Contacts per owner** — fully rebuilt every run (added 2026-08-14, expanded same day). No manual columns, so it's regenerated from the current New Contacts data each time rather than patched. Week 1 = Aug 7–13, 2026. Three tables, five charts:
    - **Owner per week** (top-left) — count per owner per week, stacked column chart beside it (X axis = week).
    - **Source per week** (top-right, beside the owner chart) — same, broken out by Source instead of Owner. This replaced the old day-bucketed standalone "By Source" sheet, which is retired.
    - **Owner × Source per week** (below both) — one row per owner per week with a source breakdown; rows are grouped by owner (Alex, Joncarlo, Stormer, then "(unassigned)" last) so each owner's weeks are contiguous. One stacked chart per **real** owner only (no chart for "(unassigned)" — nothing meaningful to show there), stacked vertically underneath the table.
- `data/new_contacts_log.json` is a separate running record of every contact ever fetched (for the exclusion list and dedup bookkeeping) — it is bookkeeping only, not the spreadsheet's source of truth.
- Daily trigger: say **"update"** and this gets run with no arguments (defaults to yesterday's contacts).
- **Bug fixed 2026-08-18 — row order corruption on backfill.** New rows used to always insert at row 2 (the very top), which assumes whatever's already there is older. That's true for a normal single most-recent-day "update," but breaks the moment a backfill adds date(s) *older* than something already appended that session (e.g. a plain "update" adds day N, then a `--start/--end` backfill for N-3..N-1 runs afterward — those got shoved above day N instead of below it). Symptom: dates out of order in New Contacts and/or the owner tabs. Fixed by `insert_sorted_by_date()`, which finds the row a date actually belongs at instead of assuming row 2 — order-independent regardless of what sequence dates get processed in. If dates ever look out of order again, it's not this bug (recurrence would mean something new).

---

## update_funnel.py

**Question:** What does the sales funnel look like per rep, from New Lead down to Proposal Sent?

```
python3 update_funnel.py
```

- **Automated from GHL as of 2026-08-25.** Counts come from the six "Date Entered - &lt;Stage&gt;" DATE custom fields on each opportunity — the same source of truth `build_dashboard.py` uses for Weekly Rocks (`FIELD_DATE_STAGES`). They were hand-typed before that, on the mistaken assumption the API couldn't supply them.
- Edit `SINCE` and `FUNNEL_REPS` at the top of the script to change the window or which reps appear (currently Alex + Joncarlo; Stormer and Cole are deliberately excluded).
- Future-dated stage entries are skipped as data-entry errors, matching `build_dashboard.py`'s rule. The script reports how many it skipped — worth chasing those down in GHL.
- Only ever touches the "Funnel" sheet in `axiskey_new_contacts_log.xlsx` — every other sheet is left alone.
- Builds one funnel-shaped chart per rep (a stacked horizontal bar chart with an invisible padding series — openpyxl has no native funnel chart type) plus a table with conversion rate from the previous stage and from New Lead.

---

## update_weekly_tables.py

**Question:** Week by week, how many contacts hit each stage — per rep?

```
python3 update_weekly_tables.py
```

- Rebuilds the **"Weekly by Rep"** sheet in `axiskey_new_contacts_log.xlsx`: for each rep, a week x stage count matrix plus the matching conversion matrix (4 tables total). Only that sheet is touched.
- Same source as `update_funnel.py` — the "Date Entered - &lt;Stage&gt;" opportunity DATE fields.
- Weeks auto-extend to today. `W0` is the Jul 29–31 partial; `W1` starts Aug 1; partial weeks are flagged with `*` since their counts aren't comparable to full weeks.
- **The weekly conversion figures are throughput ratios, not cohort conversion.** They divide stage entries occurring in the same week, but a lead and its discovery call usually fall in *different* weeks — so a stage can exceed 100% of the stage above it (Joncarlo W2 Proposal Sent reads 120%). Only the **Total** column is a true conversion rate. A blank cell means the previous stage had no entries that week. This caveat is written into the sheet itself.
- For real per-week conversion the query has to be cohort-based — follow each week's leads forward across subsequent weeks. Not built yet.

---

## build_funnel_deck.js — lives in `~/Downloads`, not this folder

**Question:** Can I put the funnel in front of people as slides?

```
cd ~/Downloads && node build_funnel_deck.js axiskey_lead_funnel.pptx && python3 fix_chart_axes.py axiskey_lead_funnel.pptx
```

Kept alongside the other decks in `~/Downloads` (operator's preference, 2026-08-25): `build_funnel_deck.js`, `fix_chart_axes.py`, and the output `axiskey_lead_funnel.pptx`. Documented here because the numbers come from this workspace.

- Depends on `pptxgenjs`, installed locally at `~/Downloads/node_modules`. A global install needs admin rights, so it lives there instead; if it goes missing, `cd ~/Downloads && npm install pptxgenjs`.
- **Rebuilding overwrites the deck and discards hand-edits made in PowerPoint.** The generator was last synced to the live deck on 2026-08-25 (slide-2 title, per-rep conversion percentages). If you edit slides by hand again, either fold the change back into this script or stop rebuilding and edit the .pptx directly.

- Two-slide 16:9 deck: the funnel (Alex + Joncarlo, split bars) and the data observations.
- Matches `AxisKey_GTM_Presentation.pptx`, **not** the dashboard: Eurostile headings, Inter body, grayscale (`36454F` charcoal, `8A8A8A`, `D9D9D9`, `F2F2F2`) — no hero lime.
- Counts are hardcoded in the `stages` array at the top. Refresh them from `update_funnel.py`'s output when the window moves.
- **`fix_chart_axes.py` is not optional.** pptxgenjs 4.x writes three `<c:axId>` children into a 2D `<c:barChart>` while declaring only two axes; PowerPoint can discard the chart and call the file corrupt. Reproduced on a one-series minimal chart, so it is a library defect, not a config mistake. Run it after every build.
- Note: **Inter is not installed on this machine** (not in system fonts, not among Office's bundled fonts) — PowerPoint substitutes it. Eurostile *is* bundled with PowerPoint and renders correctly.

---

## Data folders

| Folder | What goes there |
|---|---|
| `data/exports/` | One-off query results (weekly reports, lead counts) |
| `data/snapshots/` | Daily opportunity snapshots for movement tracking |
