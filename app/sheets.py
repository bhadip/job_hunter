import re
import time

import gspread
from google.oauth2.service_account import Credentials

from .config import settings

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Per-user database tab layout (A..Q)
HEADERS = [
    "Company", "Position", "Location", "Source", "Job URL", "Job Description",
    "Age", "Score", "Skill Gaps", "Tailored Bullets", "Resume", "Cover Letter",
    "Status", "Timestamp", "Posted Date", "Dupe?", "Note",
]
LOG_HEADERS = ["Timestamp", "Search Parameters", "Records Added", "Start Row", "End Row"]

COL_URL = 5          # E
COL_POSTED = "O:O"   # Posted Date column

# Retry on rate-limit (429) and transient Google server errors (5xx).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def write_with_backoff(func, *args, max_attempts=5, base_delay=20, log=print, **kwargs):
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in _RETRYABLE_STATUS and attempt < max_attempts:
                delay = base_delay * attempt
                log(
                    f"[SHEETS] HTTP {status}, attempt {attempt}/{max_attempts}. "
                    f"Retrying in {delay}s..."
                )
                time.sleep(delay)
                continue
            raise


def parse_updated_range(api_response):
    try:
        updated_range = api_response["updates"]["updatedRange"]
        match = re.search(r"![A-Z]+(\d+):[A-Z]+(\d+)", updated_range)
        if match:
            return int(match.group(1)), int(match.group(2))
    except Exception:
        pass
    return None, None


def dupe_formula(row: int) -> str:
    return (
        f"=IF(OR(COUNTIFS(A:A,A{row},B:B,B{row})>1,"
        f"COUNTIF(E:E,E{row})>1),TRUE,FALSE)"
    )


def db_tab_name(user_email: str) -> str:
    return f"DB-{user_email}"


def log_tab_name(user_email: str) -> str:
    return f"Job Searching Logs-{user_email}"


class SheetWriter:
    """Writes to per-user tabs: DB-<email> and Job Searching Logs-<email>.
    Both tabs are created (with headers) on first use."""

    def __init__(self, user_email: str, log=print):
        self.log = log
        self.user_email = user_email
        creds = Credentials.from_service_account_file(
            settings.resolved_google_credentials, scopes=SCOPES
        )
        gc = gspread.authorize(creds)
        self.sh = gc.open_by_key(settings.GOOGLE_SHEET_ID)
        self.ws = self._get_or_create(db_tab_name(user_email), HEADERS)
        self.log_ws = self._get_or_create(log_tab_name(user_email), LOG_HEADERS)

    def _get_or_create(self, title: str, headers: list):
        try:
            return self.sh.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            self.log(f"[SHEETS] Creating missing tab '{title}'.")
            ws = self.sh.add_worksheet(title=title, rows=1000, cols=len(headers))
            ws.append_row(headers)
            return ws

    def existing_urls(self) -> set:
        return set(self.ws.col_values(COL_URL))

    def append_jobs(self, rows: list):
        """Append rows (lists of 17 values). Returns (start_row, end_row)."""
        response = write_with_backoff(
            self.ws.append_rows, rows, value_input_option="USER_ENTERED", log=self.log
        )
        start_row, end_row = parse_updated_range(response)
        write_with_backoff(
            self.ws.format,
            COL_POSTED,
            {"numberFormat": {"type": "DATE", "pattern": "YYYY-MM-DD"}},
            log=self.log,
        )
        return start_row, end_row

    def update_assessments(self, updates: list):
        """updates: list of (row, score_str, skill_gaps, tailored_bullets)."""
        if not updates:
            return
        data = [
            {"range": f"H{row}:J{row}", "values": [[score, gaps, bullets]]}
            for row, score, gaps, bullets in updates
        ]
        write_with_backoff(
            self.ws.batch_update, data, value_input_option="USER_ENTERED", log=self.log
        )

    def update_generated(self, updates: list):
        """updates: list of (row, resume_paths, cover_letter_paths, status)."""
        if not updates:
            return
        data = [
            {"range": f"K{row}:M{row}", "values": [[resume, cover, status]]}
            for row, resume, cover, status in updates
        ]
        write_with_backoff(
            self.ws.batch_update, data, value_input_option="USER_ENTERED", log=self.log
        )

    def log_run(self, timestamp, params_desc, added, start_row, end_row):
        write_with_backoff(
            self.log_ws.append_row,
            [timestamp, params_desc, added, start_row or "", end_row or ""],
            value_input_option="USER_ENTERED",
            log=self.log,
        )
