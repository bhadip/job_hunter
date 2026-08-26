import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import settings

_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.DATA_DIR / "jobhunt.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_CONN = _connect()


def init_db():
    with _LOCK, _CONN:
        _CONN.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                name TEXT DEFAULT '',
                master_resume TEXT DEFAULT '',
                assessment_prompt TEXT DEFAULT '',
                default_params TEXT DEFAULT '{}',
                default_formats TEXT DEFAULT '[]',
                created_at TEXT,
                last_seen TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT,
                started_at TEXT,
                finished_at TEXT,
                status TEXT,
                params TEXT DEFAULT '{}',
                stats TEXT DEFAULT '{}',
                error TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                user_email TEXT,
                url TEXT,
                company TEXT,
                position TEXT,
                location TEXT,
                posted_date TEXT,
                score INTEGER,
                skill_gaps TEXT DEFAULT '',
                tailored_bullets TEXT DEFAULT '',
                status TEXT DEFAULT 'Scraped',
                sheet_row INTEGER,
                resume_path TEXT DEFAULT '',
                cover_letter_path TEXT DEFAULT '',
                created_at TEXT
            );
            """
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else None


# --- users ---
def get_or_create_user(email: str, name: str = "") -> dict:
    with _LOCK, _CONN:
        row = _CONN.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            _CONN.execute(
                "INSERT INTO users (email, name, created_at, last_seen) VALUES (?,?,?,?)",
                (email, name, _now(), _now()),
            )
        else:
            _CONN.execute("UPDATE users SET last_seen = ? WHERE email = ?", (_now(), email))
        row = _CONN.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return _row_to_dict(row)


def update_user(email: str, fields: dict) -> dict:
    allowed = {"name", "master_resume", "assessment_prompt", "default_params", "default_formats"}
    sets, vals = [], []
    for key, val in fields.items():
        if key not in allowed:
            continue
        if key in ("default_params", "default_formats") and not isinstance(val, str):
            val = json.dumps(val)
        sets.append(f"{key} = ?")
        vals.append(val)
    if sets:
        vals.append(email)
        with _LOCK, _CONN:
            _CONN.execute(f"UPDATE users SET {', '.join(sets)} WHERE email = ?", vals)
    return get_or_create_user(email)


# --- runs ---
def create_run(user_email: str, params: dict) -> int:
    with _LOCK, _CONN:
        cur = _CONN.execute(
            "INSERT INTO runs (user_email, started_at, status, params) VALUES (?,?,?,?)",
            (user_email, _now(), "running", json.dumps(params)),
        )
        return cur.lastrowid


def finish_run(run_id: int, status: str, stats: dict = None, error: str = ""):
    with _LOCK, _CONN:
        _CONN.execute(
            "UPDATE runs SET finished_at = ?, status = ?, stats = ?, error = ? WHERE id = ?",
            (_now(), status, json.dumps(stats or {}), error, run_id),
        )


def list_runs(user_email: str, limit: int = 50) -> list:
    with _LOCK:
        rows = _CONN.execute(
            "SELECT * FROM runs WHERE user_email = ? ORDER BY id DESC LIMIT ?",
            (user_email, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_run(run_id: int) -> dict:
    with _LOCK:
        return _row_to_dict(_CONN.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


# --- jobs ---
def add_job(run_id: int, user_email: str, job: dict) -> int:
    with _LOCK, _CONN:
        cur = _CONN.execute(
            """INSERT INTO jobs
               (run_id, user_email, url, company, position, location, posted_date,
                status, sheet_row, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, user_email, job.get("url"), job.get("company"), job.get("position"),
                job.get("location"), job.get("posted_date"), job.get("status", "Scraped"),
                job.get("sheet_row"), _now(),
            ),
        )
        return cur.lastrowid


def update_job(job_id: int, fields: dict):
    allowed = {"score", "skill_gaps", "tailored_bullets", "status", "sheet_row",
               "resume_path", "cover_letter_path"}
    sets, vals = [], []
    for key, val in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            vals.append(val)
    if sets:
        vals.append(job_id)
        with _LOCK, _CONN:
            _CONN.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", vals)


def list_jobs(run_id: int) -> list:
    with _LOCK:
        rows = _CONN.execute(
            "SELECT * FROM jobs WHERE run_id = ? ORDER BY score DESC NULLS LAST, id ASC",
            (run_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


init_db()
