import asyncio
import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
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

app = FastAPI(title="Job Hunt App", version=settings.VERSION)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
            "distance_miles": None,
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


@app.get("/api/runs")
def list_runs(user: dict = Depends(get_current_user)):
    return db.list_runs(user["email"])


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, user: dict = Depends(get_current_user)):
    run = db.get_run(run_id)
    if not run or run["user_email"] != user["email"]:
        raise HTTPException(status_code=404, detail="Run not found")
    run["jobs"] = db.list_jobs(run_id)
    return run


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: int, user: dict = Depends(get_current_user)):
    run = db.get_run(run_id)
    if not run or run["user_email"] != user["email"]:
        raise HTTPException(status_code=404, detail="Run not found")
    if not job_manager.cancel(run_id):
        raise HTTPException(status_code=400, detail="Run is not active")
    return {"status": "cancelling"}


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
