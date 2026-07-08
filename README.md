# Aikido → Google Sheets weekly security report

Weekly findings report per product, pulled from the Aikido REST API and written
to a Google Spreadsheet by a GitHub Actions workflow.

For each Aikido workspace (Mirantis, MOSK, and any added later), the script keeps
only teams named `Product:<name>`, merges products across workspaces, and produces
one worksheet per week with this layout:

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
  {"name": "MOSK",     "client_id": "AIK_...", "client_secret": "..."}
]
```

Optional per-workspace keys: `"region"` (`eu` default, `us`, or `me`) or a full
`"base_url"` if Aikido ever gives you a custom host.

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
| `TEAM_PREFIX` | `Product:` | Only teams whose name starts with this are reported; the rest of the prefix-less teams are ignored. |
| `AIKIDO_ISSUE_TYPE` | *(all)* | Limit to one Aikido issue type, e.g. `sast`, `open_source`, `leaked_secret`, `iac`, `cloud`, ... |
| `REPORT_WEEK_START` | Monday of the current week | Report window start (YYYY-MM-DD). |
| `REPORT_WEEK_DAYS` | `5` | Window length: 5 = Mon–Fri like the dashboard header; 7 also counts weekend activity. |
| `REPORT_BASELINE_DATE` | week start | Date for the left "As of … (total)" column. Set e.g. `2026-07-01` to reproduce a month-start baseline. |
| `REPORT_TIMEZONE` | `UTC` | Timezone for day boundaries (e.g. `Europe/Madrid`). |
| `TREAT_IGNORED_AS_RESOLVED` | `true` | Ignored (risk-accepted) issues count as resolved and are excluded from open totals. Set `false` to keep them in the open counts. |
| `WORKSHEET_PREFIX` | `Week ` | Tab name becomes e.g. `Week 2026-07-06`. |
| `DRY_RUN` | `false` | Print the table to the job log instead of writing to Sheets. |

## Counting rules

- **Critical** = Aikido severity `critical`; **Other** = `high` + `medium` + `low`.
- **Added** = issues whose `first_detected_at` falls inside the report window.
- **Resolved** = issues whose `closed_at` (or `ignored_at`, by default) falls inside
  the window. Snoozed issues remain counted as open.
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
- If an ignored issue is later un-ignored, Aikido clears `ignored_at`, so a
  regenerated historical week may differ slightly from what was true at the time.
- The script retries automatically on Aikido rate limits (HTTP 429) and 5xx errors.
