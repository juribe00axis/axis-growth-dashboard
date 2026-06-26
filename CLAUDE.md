# GHL CRM Workspace

## What this workspace is
This folder is for working with our GoHighLevel (GHL) CRM via its REST API.
The operator is new to APIs and terminals — explain what you're doing in
plain language before running commands.

## API conventions
- Base URL: https://services.leadconnectorhq.com
- API version: v2. Every request MUST include the header: Version: 2021-07-28
- Auth: read the token from the .env file in this folder (GHL_TOKEN).
  Use it as: Authorization: Bearer $GHL_TOKEN
- Location ID: read from .env (GHL_LOCATION_ID). Most endpoints need it
  as a query parameter or path segment.
- Rate limits: 100 requests per 10 seconds. Add small delays in any loop.
- API reference: https://marketplace.gohighlevel.com/docs/

## Standing safety rules (do not override without explicit instruction)
1. READ-ONLY by default. Only GET requests. Never POST, PUT, PATCH, or
   DELETE unless the operator explicitly requests a write action in that
   same session and confirms after seeing the exact request.
2. Never print the token value to the screen, into files, or into logs.
   Reference it only as $GHL_TOKEN.
3. Never copy .env contents anywhere, and never suggest committing this
   folder to git or uploading it.
4. Before any request, briefly state in one line what it does and why.
5. This CRM is production data for a real business. When in doubt, ask.

## Common task patterns
Exact parameter syntax should be verified against the API reference
(https://marketplace.gohighlevel.com/docs/) before first use of each
endpoint — then note the working pattern here for reuse.

### Contacts
- List/search: GET /contacts/ with locationId (basic), or
  POST /contacts/search for filtered queries (e.g. by dateAdded range).
- "How many contacts came in yesterday/this week" = search filtered by
  dateAdded between two dates, count results across pages.
- Always paginate: responses cap at ~100 records per page.

## Connected accounts
Credentials for each sub-account live in .env, suffixed by account label.
When a request is ambiguous about which account, ASK — never assume,
never mix accounts in one request.

- Cornerstone = READ-ONLY, highest caution.
  Variables: GHL_TOKEN, GHL_LOCATION
- AXISKEY = READ-ONLY
  Variables: GHL_TOKEN_AXISKEY, GHL_LOCATION_ID_AXISKEY

### Opportunities & pipelines
- Pipeline structure (names, stages, stage IDs):
  GET /opportunities/pipelines with locationId. Cache this in
  /data/pipelines.json — stage IDs are needed to label everything else.
- Opportunity counts per stage: GET /opportunities/search with
  location_id, filtered by pipeline_id and pipeline_stage_id.
  To answer "how many in each stage of each pipe": loop over stages,
  count per stage, present as a table.
- Each opportunity record includes status (open/won/lost/abandoned),
  monetaryValue, createdAt, and lastStageChangeAt.

### Pipeline movement tracking (snapshot strategy — see caveat below)
- The API reports CURRENT state, not movement history.
- Daily snapshot: fetch all open opportunities with their stage, save to
  /data/snapshots/opportunities-YYYY-MM-DD.json.
- Movement reports = diff two snapshots: which opportunity IDs changed
  stage, which appeared (new), which closed (won/lost).
- When asked about "movements", check which snapshots exist first; the
  answer can only cover the snapshot period.

### Calendars & appointments
- List calendars: GET /calendars/ with locationId.
- Appointments list: GET /calendars/events with locationId, startTime,
  endTime (epoch milliseconds). Filterable by calendarId or userId.
- "This week's appointments" = events between Monday 00:00 and Sunday
  23:59 local time; show as date-sorted list with contact, calendar,
  and appointment status.

### Conversations & messages
- Search conversations: GET /conversations/search with locationId;
  sortable by last message date.
- Messages in a thread: GET /conversations/{conversationId}/messages.
- Message volume counts require iterating conversations — respect the
  rate limit (100 req/10s), batch with delays, and save results to
  /data so counts aren't re-fetched.

### Data folder conventions
- /data/snapshots/ — dated opportunity snapshots (the movement archive)
- /data/exports/   — one-off query results (.json or .csv)
- Prefer reusing a saved export from today over re-fetching.
