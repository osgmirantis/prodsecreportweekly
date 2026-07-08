#!/usr/bin/env python3
"""
Aikido -> Google Sheets weekly findings report, broken down by product.

For every Aikido workspace configured (Mirantis, MOSK, ...), the script:
  1. Authenticates with the workspace's OAuth client credentials.
  2. Lists teams and keeps only the ones named "Product:<name>".
  3. Exports all issues for each of those teams.
  4. Aggregates, per product (merged across workspaces):
       - open issues as of the baseline date        -> "As of <baseline> (total)"
       - issues added during the report week        -> Critical / Other
       - issues resolved during the report week     -> Critical / Other
       - open issues right now                      -> "As of <today>"
  5. Writes one worksheet per report week into a Google Spreadsheet,
     matching the agreed dashboard layout (merged yellow header, Total row).

All counts are derived from issue timestamps (first_detected_at, closed_at,
ignored_at), so the report is stateless and can be rebuilt for any past week.

Required environment variables
------------------------------
AIKIDO_WORKSPACES            JSON list of workspaces, e.g.
                             [{"name": "Mirantis", "client_id": "...", "client_secret": "..."},
                              {"name": "MOSK", "client_id": "...", "client_secret": "...", "team_prefix": ""}]
                             Optional per-workspace keys:
                               "team_prefix"  overrides TEAM_PREFIX for that workspace.
                                              "" disables the filter: every active team
                                              counts as a product (e.g. MOSK, where all
                                              teams are products). Key absent = default.
                               "region"       eu (default) | us | me
                               "base_url"     custom host, overrides "region"
SPREADSHEET_ID               The Google Sheets id (the long token in the sheet URL).
GOOGLE_SERVICE_ACCOUNT_JSON  Full service-account key JSON, as a string.

Optional environment variables
------------------------------
TEAM_PREFIX                  Default team-name filter. Default: "Product:".
                             Overridable per workspace with "team_prefix" in
                             AIKIDO_WORKSPACES ("" = report all active teams).
AIKIDO_ISSUE_TYPE            Limit to one Aikido issue type, e.g. "sast".
                             Default: empty = all issue types.
REPORT_WEEK_START            YYYY-MM-DD (a Monday). Default: Monday of the current week.
REPORT_WEEK_DAYS             Length of the report window in days. Default: 5 (Mon-Fri).
REPORT_BASELINE_DATE         YYYY-MM-DD for the left "As of" column.
                             Default: the report week start (so totals reconcile:
                             baseline + added - resolved == current).
REPORT_TIMEZONE              IANA timezone used for week boundaries. Default: "UTC".
TREAT_IGNORED_AS_RESOLVED    "true"/"false". Default "true": issues marked as
                             ignored (risk accepted) count as resolved and are
                             excluded from open totals.
WORKSHEET_PREFIX             Prefix for the worksheet tab name. Default: "Week ".
DRY_RUN                      "true" to print the table to stdout and skip Google Sheets.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

import requests

REGION_HOSTS = {
    "eu": "https://app.aikido.dev",
    "us": "https://app.us.aikido.dev",
    "me": "https://app.me.aikido.dev",
}

TEAMS_PER_PAGE = 100  # API maximum
MAX_RETRIES = 6


def log(message: str) -> None:
    print(message, flush=True)


def env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    value = value.strip()
    return value if value else default


def env_bool(name: str, default: bool) -> bool:
    value = env_str(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def ts_or_none(value) -> int | None:
    """Normalize API timestamp fields: null / 0 / "" -> None."""
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Aikido API client
# --------------------------------------------------------------------------

class AikidoClient:
    def __init__(self, name: str, client_id: str, client_secret: str, base_url: str):
        self.name = name
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Request with retries on 429 / 5xx, honouring Retry-After."""
        response = None
        for attempt in range(MAX_RETRIES):
            response = self.session.request(method, url, timeout=120, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = int(retry_after) if retry_after else 0
                except ValueError:
                    wait = 0
                wait = wait or min(2 ** attempt * 2, 60)
                log(f"[{self.name}] HTTP {response.status_code} from {url}, retrying in {wait}s "
                    f"({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            if response.status_code in (401, 403):
                raise SystemExit(
                    f"[{self.name}] HTTP {response.status_code} calling {url}. "
                    "Check the workspace client_id/client_secret and make sure the API key "
                    "has the 'issues:read' and 'teams:read' scopes."
                )
            response.raise_for_status()
            return response
        response.raise_for_status()
        return response

    def authenticate(self) -> None:
        response = self._request(
            "POST",
            f"{self.base_url}/api/oauth/token",
            auth=(self.client_id, self.client_secret),
            json={"grant_type": "client_credentials"},
        )
        token = response.json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {token}"
        log(f"[{self.name}] authenticated against {self.base_url}")

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        return self._request("GET", f"{self.base_url}/api/public/v1{path}", params=params)

    def list_teams(self) -> list[dict]:
        teams: list[dict] = []
        page = 0
        while True:
            batch = self._get("/teams", {"page": page, "per_page": TEAMS_PER_PAGE}).json()
            if not isinstance(batch, list):
                raise SystemExit(f"[{self.name}] unexpected /teams response: {batch!r}")
            teams.extend(batch)
            if len(batch) < TEAMS_PER_PAGE:
                return teams
            page += 1

    def export_issues(self, team_id: int, issue_type: str | None) -> list[dict]:
        """Export INDIVIDUAL issues: one list element per single finding.

        Uses /issues/export, which returns ungrouped issues — not the
        /issue_groups "Feed" view where Aikido bundles related findings into
        one group. Each element here has its own id, group_id (informational
        only), first_detected_at, closed_at and ignored_at, so every finding
        is counted separately. Leaving 'page' unset makes the API return the
        complete list in one response (no pagination truncation).
        """
        params = {
            "format": "json",
            "filter_status": "all",  # need closed/ignored too, for "resolved" counts
            "filter_team_id": team_id,
        }
        if issue_type:
            params["filter_issue_type"] = issue_type
        issues = self._get("/issues/export", params).json()
        if not isinstance(issues, list):
            raise SystemExit(f"[{self.name}] unexpected /issues/export response: {issues!r}")
        return issues


# --------------------------------------------------------------------------
# Counting logic
# --------------------------------------------------------------------------

def is_critical(issue: dict) -> bool:
    return (issue.get("severity") or "").lower() == "critical"


def resolution_ts(issue: dict, treat_ignored_as_resolved: bool) -> int | None:
    """Timestamp when the issue stopped being open, or None if still open."""
    closed = ts_or_none(issue.get("closed_at"))
    ignored = ts_or_none(issue.get("ignored_at")) if treat_ignored_as_resolved else None
    if closed and ignored:
        return min(closed, ignored)
    return closed or ignored


def is_open_at(issue: dict, ts: int, treat_ignored_as_resolved: bool) -> bool:
    first_detected = ts_or_none(issue.get("first_detected_at"))
    if first_detected is None or first_detected > ts:
        return False
    resolved = resolution_ts(issue, treat_ignored_as_resolved)
    return resolved is None or resolved > ts


def new_product_stats() -> dict:
    return {
        "baseline": 0,
        "added_critical": 0,
        "added_other": 0,
        "resolved_critical": 0,
        "resolved_other": 0,
        "current": 0,
    }


def tally_issues(issues: list[dict], stats: dict, baseline_ts: int,
                 week_start_ts: int, week_end_ts: int, now_ts: int,
                 treat_ignored_as_resolved: bool) -> None:
    for issue in issues:
        critical = is_critical(issue)
        first_detected = ts_or_none(issue.get("first_detected_at"))
        resolved = resolution_ts(issue, treat_ignored_as_resolved)

        if is_open_at(issue, baseline_ts, treat_ignored_as_resolved):
            stats["baseline"] += 1
        if first_detected is not None and week_start_ts <= first_detected < week_end_ts:
            stats["added_critical" if critical else "added_other"] += 1
        if resolved is not None and week_start_ts <= resolved < week_end_ts:
            stats["resolved_critical" if critical else "resolved_other"] += 1
        if is_open_at(issue, now_ts, treat_ignored_as_resolved):
            stats["current"] += 1


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def us_date(d: dt.date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def build_table(per_product: dict[str, dict], baseline_date: dt.date,
                week_start: dt.date, week_last_day: dt.date,
                today: dt.date) -> list[list]:
    week_label = f"{week_start:%m/%d} {week_last_day:%m/%d} {week_start.year}"
    header_row_1 = [
        "Product",
        f"As of {us_date(baseline_date)} (total)",
        f"Week {week_label} added", "",
        f"Week {week_label} resolved", "",
        f"As of {us_date(today)}",
    ]
    header_row_2 = ["", "", "Critical", "Other", "Critical", "Other", ""]

    rows: list[list] = []
    total = new_product_stats()
    for product in sorted(per_product, key=str.lower):
        s = per_product[product]
        rows.append([product, s["baseline"], s["added_critical"], s["added_other"],
                     s["resolved_critical"], s["resolved_other"], s["current"]])
        for key in total:
            total[key] += s[key]

    total_row = ["Total", total["baseline"], total["added_critical"], total["added_other"],
                 total["resolved_critical"], total["resolved_other"], total["current"]]
    return [header_row_1, header_row_2] + rows + [total_row]


def print_table(values: list[list]) -> None:
    widths = [max(len(str(row[i])) for row in values) for i in range(len(values[0]))]
    for row in values:
        log("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def write_to_google_sheets(values: list[list], worksheet_title: str,
                           now: dt.datetime, tz_name: str) -> str:
    import gspread
    from google.oauth2.service_account import Credentials

    service_account_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(credentials)
    spreadsheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])

    # Recreate the worksheet so stale merges/formatting never linger.
    try:
        old = spreadsheet.worksheet(worksheet_title)
        spreadsheet.del_worksheet(old)
    except gspread.WorksheetNotFound:
        pass
    worksheet = spreadsheet.add_worksheet(
        title=worksheet_title, rows=max(50, len(values) + 10), cols=10, index=0
    )

    worksheet.update(values=values, range_name="A1")

    last_row = len(values)
    for merge_range in ("A1:A2", "B1:B2", "C1:D1", "E1:F1", "G1:G2"):
        worksheet.merge_cells(merge_range)

    yellow = {"red": 1.0, "green": 1.0, "blue": 0.0}
    worksheet.format("A1:G1", {
        "backgroundColor": yellow,
        "textFormat": {"bold": True},
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
    })
    worksheet.format("A2:G2", {
        "textFormat": {"bold": True},
        "horizontalAlignment": "CENTER",
    })
    worksheet.format(f"A{last_row}:G{last_row}", {"textFormat": {"bold": True}})
    worksheet.freeze(rows=2)

    spreadsheet.batch_update({
        "requests": [
            {"updateDimensionProperties": {
                "range": {"sheetId": worksheet.id, "dimension": "COLUMNS",
                          "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 170}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": worksheet.id, "dimension": "COLUMNS",
                          "startIndex": 1, "endIndex": 7},
                "properties": {"pixelSize": 120}, "fields": "pixelSize"}},
        ]
    })

    footer_row = last_row + 2
    worksheet.update(
        values=[[f"Generated {now:%Y-%m-%d %H:%M} ({tz_name}) by aikido_weekly_report.py"]],
        range_name=f"A{footer_row}",
    )
    worksheet.format(f"A{footer_row}", {
        "textFormat": {"italic": True, "fontSize": 9,
                       "foregroundColor": {"red": 0.5, "green": 0.5, "blue": 0.5}},
    })
    return spreadsheet.url


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_workspaces() -> list[dict]:
    raw = env_str("AIKIDO_WORKSPACES")
    if not raw:
        raise SystemExit("AIKIDO_WORKSPACES is not set.")
    try:
        workspaces = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"AIKIDO_WORKSPACES is not valid JSON: {exc}") from exc
    if not isinstance(workspaces, list) or not workspaces:
        raise SystemExit("AIKIDO_WORKSPACES must be a non-empty JSON list.")
    for ws in workspaces:
        for key in ("name", "client_id", "client_secret"):
            if not ws.get(key):
                raise SystemExit(f"Workspace entry {ws.get('name', ws)!r} is missing '{key}'.")
    return workspaces


def workspace_base_url(workspace: dict) -> str:
    if workspace.get("base_url"):
        return workspace["base_url"]
    region = (workspace.get("region") or "eu").lower()
    if region not in REGION_HOSTS:
        raise SystemExit(f"Unknown region {region!r} for workspace {workspace['name']!r}. "
                         f"Use one of: {', '.join(REGION_HOSTS)}, or set 'base_url'.")
    return REGION_HOSTS[region]


def effective_team_prefix(workspace: dict, default_prefix: str) -> str:
    """Per-workspace filter: '' disables it (every active team is a product,
    e.g. MOSK); a missing "team_prefix" key falls back to TEAM_PREFIX."""
    if "team_prefix" in workspace:
        return (workspace["team_prefix"] or "").strip()
    return default_prefix


def select_product_teams(teams: list[dict], prefix: str) -> list[tuple[str, int]]:
    """Map Aikido teams to (product_name, team_id) pairs.

    With a prefix, only active teams named '<prefix><product>' are kept and the
    prefix is stripped; with an empty prefix, every active team is a product
    under its full team name.
    """
    selected: list[tuple[str, int]] = []
    for team in teams:
        name = (team.get("name") or "").strip()
        if not name or not team.get("active", True):
            continue
        if not prefix:
            selected.append((name, team["id"]))
        elif name.lower().startswith(prefix.lower()):
            product = name[len(prefix):].strip() or name
            selected.append((product, team["id"]))
    return selected


def main() -> int:
    tz_name = env_str("REPORT_TIMEZONE", "UTC")
    tz = ZoneInfo(tz_name)
    now = dt.datetime.now(tz)
    today = now.date()

    week_start_raw = env_str("REPORT_WEEK_START")
    if week_start_raw:
        week_start = dt.date.fromisoformat(week_start_raw)
    else:
        week_start = today - dt.timedelta(days=today.weekday())  # Monday of current week

    week_days = int(env_str("REPORT_WEEK_DAYS", "5"))
    week_last_day = week_start + dt.timedelta(days=week_days - 1)

    baseline_raw = env_str("REPORT_BASELINE_DATE")
    baseline_date = dt.date.fromisoformat(baseline_raw) if baseline_raw else week_start

    week_start_ts = int(dt.datetime.combine(week_start, dt.time.min, tz).timestamp())
    week_end_ts = int(dt.datetime.combine(week_start + dt.timedelta(days=week_days),
                                          dt.time.min, tz).timestamp())
    baseline_ts = int(dt.datetime.combine(baseline_date, dt.time.min, tz).timestamp())
    now_ts = int(now.timestamp())

    team_prefix = env_str("TEAM_PREFIX", "Product:")
    issue_type = env_str("AIKIDO_ISSUE_TYPE") or None
    treat_ignored_as_resolved = env_bool("TREAT_IGNORED_AS_RESOLVED", True)
    dry_run = env_bool("DRY_RUN", False)

    log(f"Report week: {week_start} .. {week_last_day} ({week_days} days, tz={tz_name})")
    log(f"Baseline date: {baseline_date} | Current: {now:%Y-%m-%d %H:%M}")
    log(f"Default team prefix: {team_prefix!r} | Issue type: {issue_type or 'all'} | "
        f"ignored counts as resolved: {treat_ignored_as_resolved}")

    per_product: dict[str, dict] = defaultdict(new_product_stats)

    for workspace in parse_workspaces():
        client = AikidoClient(
            name=workspace["name"],
            client_id=workspace["client_id"],
            client_secret=workspace["client_secret"],
            base_url=workspace_base_url(workspace),
        )
        client.authenticate()

        prefix = effective_team_prefix(workspace, team_prefix)
        log(f"[{workspace['name']}] team filter: "
            + (f"prefix {prefix!r}" if prefix
               else "none (every active team counts as a product)"))

        teams = client.list_teams()
        product_teams = select_product_teams(teams, prefix)

        if not product_teams:
            reason = (f"no active teams matching prefix {prefix!r}" if prefix
                      else "no active teams found")
            log(f"[{workspace['name']}] WARNING: {reason}. Teams seen: "
                f"{', '.join(sorted((t.get('name') or '?') for t in teams)) or '(none)'}")
            continue

        log(f"[{workspace['name']}] product teams: "
            f"{', '.join(name for name, _ in product_teams)}")

        for product, team_id in product_teams:
            issues = client.export_issues(team_id, issue_type)
            tally_issues(issues, per_product[product], baseline_ts,
                         week_start_ts, week_end_ts, now_ts,
                         treat_ignored_as_resolved)
            groups = {i.get("group_id") for i in issues if i.get("group_id") is not None}
            log(f"[{workspace['name']}]   {product}: {len(issues)} individual issues "
                f"fetched ({len(groups)} Aikido groups, all statuses)")

    if not per_product:
        log("ERROR: no reportable teams found in any workspace; nothing to report.")
        return 1

    values = build_table(per_product, baseline_date, week_start, week_last_day, today)
    log("")
    print_table(values)

    if dry_run:
        log("\nDRY_RUN=true -> skipping Google Sheets update.")
        return 0

    for var in ("SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON"):
        if not env_str(var):
            raise SystemExit(f"{var} is not set (required unless DRY_RUN=true).")

    worksheet_title = f"{env_str('WORKSHEET_PREFIX', 'Week ')}{week_start:%Y-%m-%d}"
    url = write_to_google_sheets(values, worksheet_title, now, tz_name)
    log(f"\nWrote worksheet {worksheet_title!r} -> {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
