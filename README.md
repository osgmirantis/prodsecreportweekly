# Aikido → Google Sheets weekly security report

Weekly findings report per product, pulled from the Aikido REST API and written
to a Google Spreadsheet by a GitHub Actions workflow.

Each workspace decides how its teams map to products: by default only teams named
`Product:<name>` are reported (Mirantis), but a workspace can disable the filter so
that **every active team counts as a product** (MOSK). Products are merged across
workspaces, and one worksheet per week is produced with this layout:

| Product | As of 7/6/2026 (total) | Week 07/06 07/10 2026 added (Critical / Other) | Week 07/06 07/10 2026 resolved (Critical / Other) | As of 7/8/2026 |
|---------|------------------------|------------------------------------------------|---------------------------------------------------|----------------|
| MOSK    | 5,000                  | 10 / 20                                        | 5 / 10                                            | 5,015          |
| ...     |                        |                                                |                                                   |                |
| Total   | ...                    |                                                |                                                   |                |

Everything is computed from issue timestamps (`first_detected_at`, `closed_at`,
`ignored_at`) returned by Aikido's `/issues/export` endpoint, so any past week can
be regenerated at any time — no state is kept between runs.

## Repository layout

```
scripts/aikido_weekly_report.py        the report script
.github/workflows/aikido-weekly-report.yml   scheduled GHA workflow
requirements.txt
```

## One-time setup

### 1. Aikido API credentials (one per workspace)

For **each** workspace (Mirantis, MOSK, ...):

1. In Aikido, go to Settings → Integrations → API and create an API key /
   OAuth client. Grant it at least the `issues:read` and `teams:read` scopes.
2. Note the `client_id` and `client_secret`.

### 2. Google service account

1. In Google Cloud Console, create (or reuse) a project and enable the
   **Google Sheets API**.
2. Create a **service account** and generate a **JSON key**.
3. Create the target spreadsheet (or use an existing one) and **share it with the
   service account's email** (`something@project.iam.gserviceaccount.com`) as **Editor**.
4. Note the spreadsheet id — the long token in the URL:
   `https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`.

### 3. GitHub repository secrets

Create these in Settings → Secrets and variables → Actions:

| Secret | Value |
|--------|-------|
| `AIKIDO_WORKSPACES` | JSON list of workspaces, see below |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The full contents of the service-account JSON key |
| `SPREADSHEET_ID` | The spreadsheet id from the URL |

`AIKIDO_WORKSPACES` format (add more entries as new workspaces appear):

```json
[
  {"name": "Mirantis", "client_id": "AIK_...", "client_secret": "..."},
  {"name": "MOSK",     "client_id": "AIK_...", "client_secret": "...", "team_prefix": ""}
]
```

Optional per-workspace keys:

- `"team_prefix"` — overrides the team filter for that workspace. `""` (as for MOSK
  above) disables filtering: every active team is treated as a product under its
  full team name. Leaving the key out keeps the default `Product:<name>` filter.
- `"region"` — `eu` (default), `us`, or `me`.
- `"base_url"` — full custom host, overrides `"region"`.

## Running

- **Scheduled:** the workflow runs every Friday 15:00 UTC (edit the cron in
  `.github/workflows/aikido-weekly-report.yml`).
- **Manual:** Actions → "Aikido weekly security report" → *Run workflow*. You can
  override the week, the baseline date, restrict to one issue type, or do a dry run.
- **Locally:**

  ```bash
  pip install -r requirements.txt
  export AIKIDO_WORKSPACES='[{"name":"Mirantis","client_id":"...","client_secret":"..."}]'
  export DRY_RUN=true          # print the table, don't touch the sheet
  python scripts/aikido_weekly_report.py
  ```

## Configuration reference (environment variables)

| Variable | Default | Meaning |
|----------|---------|---------|
| `TEAM_PREFIX` | `Product:` | Default team filter. Overridable per workspace via `"team_prefix"` in `AIKIDO_WORKSPACES`; an empty string there disables filtering for that workspace so all its active teams become products (e.g. MOSK). |
| `AIKIDO_ISSUE_TYPE` | *(all)* | Limit to one Aikido issue type, e.g. `sast`, `open_source`, `leaked_secret`, `iac`, `cloud`, ... |
| `REPORT_WEEK_START` | Monday of the current week | Report window start (YYYY-MM-DD). |
| `REPORT_WEEK_DAYS` | `5` | Window length: 5 = Mon–Fri like the dashboard header; 7 also counts weekend activity. |
| `REPORT_BASELINE_DATE` | week start | Date for the left "As of … (total)" column. Set e.g. `2026-07-01` to reproduce a month-start baseline. |
| `REPORT_TIMEZONE` | `UTC` | Timezone for day boundaries (e.g. `Europe/Madrid`). |
| `TREAT_IGNORED_AS_RESOLVED` | `true` | Ignored (risk-accepted) issues count as resolved and are excluded from open totals. Set `false` to keep them in the open counts. |
| `EXCLUDE_IGNORED_BY` | *(unset)* | Comma list of ignore sources (`auto`, `rule`, `user`, `api`). Issues currently ignored by one of these are removed from **all** counts — e.g. `auto,rule` keeps findings that Aikido auto-triages on arrival out of the dashboard entirely. |
| `WORKSHEET_PREFIX` | `Week ` | Tab name becomes e.g. `Week 2026-07-06`. |
| `DEBUG_CSV_DIR` | *(unset)* | If set, writes one CSV per workspace/product listing every individual issue counted as added/resolved this week (issue_id, group_id, severity, timestamps, repo). In GHA, tick the `debug_csv` input to get these as a run artifact. |
| `DRY_RUN` | `false` | Print the table to the job log instead of writing to Sheets. |

## Why the numbers differ from the Feed UI

The Feed and this report count different things, so they will rarely match 1:1:

- **Unit**: the Feed lists **issue groups**; the report counts **individual issues**
  (as required). One Feed card can contain dozens of single findings, so e.g.
  "7 groups" in the Feed and "81 issues" in the report can describe the same week.
- **Severity**: a Feed card shows the **group's** severity; the member issues carry
  their own per-issue `severity`, which is what the report's Critical/Other split
  uses. A critical group can consist of high/medium individual issues.
- **First detected**: the Feed's date filter applies to the group; the report uses
  each issue's own `first_detected_at`. New issues joining an old group are counted
  by the report but invisible under the Feed's date filter — and vice versa.
- **Status scope**: the Feed shows open groups only; the report's "added" counts
  every issue detected in the window even if it was already closed/auto-closed
  again by the time you look.
- **Ignored issues are hidden in the UI's default views** (issue lists show open
  statuses like New / To do) but the report counts them: an issue detected and
  auto-ignored in the same week is +1 added and +1 resolved. Include the Ignored
  status in the UI filter to see them — or set `EXCLUDE_IGNORED_BY=auto,rule` to
  keep machine-triaged issues out of the report altogether.
- **Window**: the report defaults to Mon–Fri in UTC. To mirror a UI filter like
  07/06–07/12 set `REPORT_WEEK_DAYS=7` (and `REPORT_TIMEZONE=Europe/Madrid` for
  local-midnight boundaries).

To audit a specific product, run the workflow with the `debug_csv` input ticked
(or set `DEBUG_CSV_DIR` locally) and look up the Feed card's group id in the CSV:
you'll see exactly which member issues were counted, with their individual
severities and first-detected dates.

## Counting rules

- **Critical** = Aikido severity `critical`; **Other** = `high` + `medium` + `low`.
- **Added** = issues whose `first_detected_at` falls inside the report window.
- **Resolved** = issues whose `closed_at` (or `ignored_at`, by default) falls inside
  the window. Snoozed issues remain counted as open. The issue's **current status is
  authoritative**: Aikido keeps stale `closed_at`/`ignored_at` values on issues that
  were closed and later reopened (the export can show `status=open` with a past
  `closed_at`), so an issue that is open or snoozed right now is never counted as
  resolved, whatever its timestamps say.
- **As of \<date\>** = issues detected on/before that date and not yet
  closed/ignored by then.
- With the default baseline (= week start), the numbers reconcile per row:
  `baseline + added − resolved = current`. With a custom baseline (e.g. 7/1 while the
  week starts 7/6), the identity no longer holds by construction — expected.
- A new worksheet is created per week (newest tab first); re-running the same week
  replaces that week's tab.

## Notes & limitations

- If a repository is linked to two different `Product:` teams, its issues count
  toward both products (each product sees its own full picture); the Total row
  would then slightly exceed the true distinct-issue count.
- Aikido keeps stale `closed_at`/`ignored_at` values on issues that get reopened,
  so the script trusts `status` first. Because the export doesn't say *when* such an
  issue reopened, historical "as of" counts treat it as open since first detection —
  the current ("As of today") column always matches Aikido's live state.
- The script retries automatically on Aikido rate limits (HTTP 429) and 5xx errors.
