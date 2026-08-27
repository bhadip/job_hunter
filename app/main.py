import asyncio
import io
import json
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, llm, pipeline
from .auth import get_current_user
from .config import settings
from .jobs import job_manager
from .models import ProfileUpdate, RunRequest

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

SECRETS_HINT = (
    "On the server, create .venv/.secrets next to docker-compose.yml "
    "(it is gitignored, so git does not copy it), then run: docker compose restart"
)

app = FastAPI(title="Job Hunter", version=settings.VERSION)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_PAGE_STYLE = """
    body { background:#0f1419; color:#e6e9ef; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }
    .card { background:#1a2230; border:1px solid #2a3547; border-radius:10px; padding:32px; max-width:560px; margin:16px; }
    h1 { font-size:20px; margin-top:0; }
    p, li { color:#8b98ab; font-size:14px; line-height:1.6; }
    code { background:rgba(0,0,0,.35); padding:1px 5px; border-radius:4px; }
    .btn { display:inline-block; background:#4f8ef7; color:#fff; text-decoration:none; padding:10px 22px; border-radius:6px; font-size:14px; margin-top:8px; }
    .detail { font-size:12px; color:#5a6678; margin-top:24px; }
"""


def _page(title: str, body: str, detail: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} - Job Hunter</title>
  <style>{_PAGE_STYLE}</style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    {body}
    <p class="detail">{detail} &middot; Job Hunter v{settings.VERSION}</p>
  </div>
</body>
</html>"""


def _direct_access_page(detail: str) -> str:
    """401: no token at all -> the request bypassed Cloudflare."""
    if settings.CF_APP_URL:
        link_block = (
            f'<p><a class="btn" href="{settings.CF_APP_URL}">'
            f"Continue to {settings.CF_APP_URL}</a></p>"
        )
    else:
        link_block = (
            "<p>Open this app via its Cloudflare-protected URL instead "
            "(the hostname configured in your Cloudflare Access application). "
            "Set <code>CF_APP_URL</code> in <code>.venv/.secrets</code> to show "
            "a direct link here.</p>"
        )
    body = f"""
    <p>You reached this app directly (by IP/port), which bypasses Cloudflare,
    so there is no identity token to verify. Direct access is blocked on
    purpose &mdash; this is the app failing closed, not an error.</p>
    {link_block}
    <p>If you are the admin and want direct LAN access instead, remove
    <code>CF_TEAM_DOMAIN</code> / <code>CF_ACCESS_AUD</code> from
    <code>.venv/.secrets</code> and restart &mdash; the app then runs in dev
    mode (single user, no auth; do not expose it beyond your LAN).</p>"""
    return _page("Sign in via Cloudflare Access required", body, detail)


def _invalid_token_page(detail: str) -> str:
    """403: a token WAS present but failed verification -> config problem."""
    body = f"""
    <p>A Cloudflare Access token <b>was</b> sent with your request, but it
    failed verification. This is a configuration issue, not a bypass. The two
    usual causes:</p>
    <ol>
      <li><b>No Access Application covers this hostname.</b> A tunnel
      <i>Published Application Route</i> only forwards traffic &mdash; it does
      not put the hostname behind Access. In Zero Trust go to
      <b>Access &rarr; Applications &rarr; Add &rarr; Self-hosted</b>, set the
      application domain to this exact hostname, add an Allow policy with the
      Google identity provider, and save.</li>
      <li><b>Wrong <code>CF_ACCESS_AUD</code>.</b> It must be the
      <b>App AUD</b> of the application that covers <i>this</i> hostname
      (Application &rarr; Overview), not another app's AUD and not your team
      domain. Update <code>.venv/.secrets</code> and run
      <code>docker compose restart</code>.</li>
    </ol>
    <p>Check <code>docker logs jobhunt</code> for the specific verification
    failure (expired / audience / signature).</p>"""
    return _page("Cloudflare Access token rejected", body, detail)


def _logout_url():
    """Cloudflare Access logout URL with redirect back to the app (which
    triggers a fresh login), or None in dev mode."""
    if not settings.cloudflare_enabled:
        return None
    team = (settings.CF_TEAM_DOMAIN or "").strip()
    if not team:
        return None
    if not team.startswith("http"):
        team = "https://" + team
    url = team.rstrip("/") + "/cdn-cgi/access/logout"
    app_url = (settings.CF_APP_URL or "").strip()
    if app_url:
        url += "?redirect=" + quote(app_url, safe="")
    return url


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Browsers hitting auth errors get a friendly HTML page; API calls get JSON."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        if exc.status_code == 401:
            return HTMLResponse(content=_direct_access_page(exc.detail), status_code=401)
        if exc.status_code == 403:
            return HTMLResponse(content=_invalid_token_page(exc.detail), status_code=403)
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


@app.on_event("startup")
async def _startup():
    job_manager.set_loop(asyncio.get_event_loop())


@app.get("/")
def index(user: dict = Depends(get_current_user)):
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": settings.VERSION}


@app.get("/api/config")
def get_config(user: dict = Depends(get_current_user)):
    return {
        "version": settings.VERSION,
        "sheet_id": settings.GOOGLE_SHEET_ID,
        "sheet_name": settings.GOOGLE_SHEET_NAME,
        "auth_mode": "cloudflare" if settings.cloudflare_enabled else "dev",
        "logout_url": _logout_url(),
        "llm_configured": settings.llm_configured,
        "telegram_configured": settings.telegram_configured,
        "sheets_configured": settings.sheets_configured,
        "secrets_loaded_from": settings.secrets_loaded_from,
        "missing_keys": settings.missing_keys,
        "default_assessment_prompt": llm.load_default_assessment_prompt(),
        "defaults": {
            "keywords": settings.DEFAULT_KEYWORDS,
            "location": settings.DEFAULT_LOCATION,
            "offsets": settings.DEFAULT_OFFSETS,
            "limit": settings.DEFAULT_LIMIT,
            "easy_apply": True,
            "employment_types": settings.DEFAULT_EMPLOYMENT_TYPES,
            "experience_levels": settings.DEFAULT_EXPERIENCE_LEVELS,
            "distance_km": None,
            "under_10_applicants": False,
            "score_threshold": settings.SCORE_THRESHOLD,
            "formats": settings.DEFAULT_FORMATS,
            "dry_run": False,
        },
    }


@app.get("/api/me")
def get_me(user: dict = Depends(get_current_user)):
    return user


@app.put("/api/me")
def put_me(update: ProfileUpdate, user: dict = Depends(get_current_user)):
    fields = {k: v for k, v in update.dict().items() if v is not None}
    return db.update_user(user["email"], fields)


@app.post("/api/runs")
def start_run(params: RunRequest, user: dict = Depends(get_current_user)):
    if not params.keywords:
        raise HTTPException(status_code=400, detail="At least one keyword is required.")
    if not settings.llm_configured:
        raise HTTPException(
            status_code=400,
            detail=f"OPENAI_API_KEY is not configured. {SECRETS_HINT}",
        )
    if not settings.sheets_configured:
        raise HTTPException(
            status_code=400,
            detail=f"GOOGLE_APPLICATION_CREDENTIALS is not configured. {SECRETS_HINT}",
        )
    if not (user.get("master_resume") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="No master resume on your profile. Save one in the Profile tab first.",
        )
    run_id = db.create_run(user["email"], params.dict())

    def emit_wrapper(msg, stage=None):
        job_manager.emit(run_id, msg, stage=stage)

    def wrapped(state):
        pipeline.execute(run_id, params, user, emit_wrapper, state.cancel)

    job_manager.start(run_id, wrapped)
    return {"run_id": run_id}


def _get_owned_run(run_id: int, user: dict) -> dict:
    run = db.get_run(run_id)
    if not run or run["user_email"] != user["email"]:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/runs")
def list_runs(user: dict = Depends(get_current_user)):
    return db.list_runs(user["email"])


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, user: dict = Depends(get_current_user)):
    run = _get_owned_run(run_id, user)
    run["jobs"] = db.list_jobs(run_id)
    return run


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: int, user: dict = Depends(get_current_user)):
    _get_owned_run(run_id, user)
    if not job_manager.cancel(run_id):
        raise HTTPException(status_code=400, detail="Run is not active")
    return {"status": "cancelling"}


# ---------- generated file downloads ----------

def _user_output_dir(email: str) -> Path:
    """Resolved output dir for a user, guaranteed to stay under OUTPUT_DIR."""
    root = Path(settings.OUTPUT_DIR).resolve()
    user_dir = (root / email).resolve()
    try:
        user_dir.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid output directory")
    return user_dir


def _resolve_user_file(path_str: str, user_dir: Path) -> Path:
    """Resolve a stored path and verify it lives inside the user's output dir."""
    p = Path(path_str).resolve()
    try:
        p.relative_to(user_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="File is outside your output directory")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return p


def _job_file_paths(job: dict, kind: str) -> list:
    raw = job.get("resume_path" if kind == "resume" else "cover_letter_path") or ""
    return [p.strip() for p in raw.splitlines() if p.strip()]


@app.get("/api/runs/{run_id}/jobs/{job_id}/download")
def download_job_file(
    run_id: int,
    job_id: int,
    kind: str = "resume",
    fmt: str = "",
    user: dict = Depends(get_current_user),
):
    _get_owned_run(run_id, user)
    if kind not in ("resume", "cover"):
        raise HTTPException(status_code=400, detail="kind must be 'resume' or 'cover'")
    job = next((j for j in db.list_jobs(run_id) if j.get("id") == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    paths = _job_file_paths(job, kind)
    if not paths:
        raise HTTPException(status_code=404, detail=f"No {kind} files for this job")
    if fmt:
        chosen = next((p for p in paths if p.lower().endswith("." + fmt.lower())), None)
        if not chosen:
            raise HTTPException(status_code=404, detail=f"No {kind} file with format '{fmt}'")
    else:
        chosen = paths[0]
    file_path = _resolve_user_file(chosen, _user_output_dir(user["email"]))
    return FileResponse(file_path, filename=file_path.name)


@app.get("/api/runs/{run_id}/download")
def download_run_zip(run_id: int, user: dict = Depends(get_current_user)):
    """Zip all generated files for a run. Built in memory on demand - the zip
    itself is never written to disk."""
    _get_owned_run(run_id, user)
    user_dir = _user_output_dir(user["email"])
    buf = io.BytesIO()
    count = 0
    seen_arcnames = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for job in db.list_jobs(run_id):
            for kind in ("resume", "cover"):
                for p in _job_file_paths(job, kind):
                    try:
                        file_path = _resolve_user_file(p, user_dir)
                    except HTTPException:
                        continue  # skip missing/moved files
                    arcname = str(file_path.relative_to(user_dir))
                    if arcname in seen_arcnames:
                        continue
                    seen_arcnames.add(arcname)
                    zf.write(file_path, arcname)
                    count += 1
    if not count:
        raise HTTPException(status_code=404, detail="No generated files found for this run")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="run_{run_id}_documents.zip"'},
    )


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: int, user: dict = Depends(get_current_user)):
    queue = job_manager.subscribe(run_id)

    async def stream():
        try:
            for event in job_manager.backlog(run_id):
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "done":
                    return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") == "done":
                        return
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            job_manager.unsubscribe(run_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream")
