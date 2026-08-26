import datetime
import time
from pathlib import Path

from . import db, llm, resume as resume_mod, scraper, sheets, telegram
from .config import settings


class Cancelled(Exception):
    pass


def _describe_params(params) -> str:
    return " | ".join(
        [
            f"Keywords={params.keywords}",
            f"Location={params.location}",
            f"Offsets={params.offsets}",
            f"EasyApply={params.easy_apply}",
            f"EmploymentTypes={params.employment_types or 'Any'}",
            f"ExperienceLevels={params.experience_levels or 'Any'}",
            f"DistanceKm={params.distance_km or 'Any'}",
            f"Under10Applicants={params.under_10_applicants}",
            f"ScoreThreshold={params.score_threshold}",
            f"Formats={params.formats}",
            f"DryRun={params.dry_run}",
        ]
    )


def _compute_age(posted_str: str, fetch_dt: datetime.datetime):
    if not posted_str:
        return "NO DATE"
    try:
        posted = datetime.datetime.strptime(posted_str, "%Y-%m-%d").date()
    except ValueError:
        return "NO DATE"
    days = (fetch_dt.date() - posted).days
    return str(days) if days >= 0 else ""


def _build_row(job: dict, fetch_dt: datetime.datetime, sheet_row_placeholder: int) -> list:
    """Build a Database-tab row (A..Q). Dupe? formula is fixed up after append
    once real row numbers are known."""
    return [
        job["company"],                                  # A Company
        job["title"],                                    # B Position
        job["location"],                                 # C Location
        "linkedin.com",                                  # D Source
        job["url"],                                      # E Job URL
        job["description"],                              # F Job Description
        _compute_age(job["posted_date"], fetch_dt),      # G Age
        "",                                              # H Score
        "",                                              # I Skill Gaps
        "",                                              # J Tailored Bullets
        "",                                              # K Resume
        "",                                              # L Cover Letter
        "Scraped",                                       # M Status
        job["crawl_time"],                               # N Timestamp
        job["posted_date"],                              # O Posted Date
        "",                                              # P Dupe? (formula added post-append)
        "",                                              # Q Note
    ]


def execute(run_id: int, params, user: dict, emit, cancel_event):
    started = time.time()
    log = lambda msg: emit(msg)
    stats = {
        "keywords": len(params.keywords),
        "location": params.location,
        "threshold": params.score_threshold,
        "dry_run": params.dry_run,
        "user_email": user.get("email", ""),
        "scraped": 0, "added": 0, "dupes": 0, "scored": 0,
        "qualified": 0, "generated": 0, "tokens": 0, "errors": 0,
        "error_details": [],
        "top_matches": [],
    }

    master_resume = (user.get("master_resume") or "").strip()
    if not master_resume:
        raise RuntimeError("No master resume on your profile. Go to the Profile tab and paste it first.")
    prompt_template = (user.get("assessment_prompt") or "").strip() or llm.load_default_assessment_prompt()

    # --- Stage 1: scrape ---
    emit("Stage 1/5: Scraping LinkedIn...", stage="scrape")
    found = []
    for kw in params.keywords:
        for offset in params.offsets:
            if cancel_event.is_set():
                raise Cancelled()
            emit(f"Fetching '{kw}' (offset {offset})...")
            found.extend(scraper.fetch_jobs(params, kw, offset, log=log))
            time.sleep(2)
    stats["scraped"] = len(found)
    emit(f"Scrape complete: {len(found)} candidate job(s).", stage="scrape")

    # --- Stage 2: dedupe + write sheet ---
    emit("Stage 2/5: Writing to Google Sheet...", stage="sheet")
    if not settings.sheets_credentials_file_exists:
        raise RuntimeError(
            "Google service-account credentials not found. The app tries "
            f"'{settings.GOOGLE_APPLICATION_CREDENTIALS}' and then auto-discovers any "
            "service-account JSON key in ./data on the host (mounted at /app/data). "
            "Copy your key into ./data - any .json filename works - then run "
            "'docker compose restart'."
        )
    writer = sheets.SheetWriter(log=log)
    existing = writer.existing_urls()
    new_jobs, seen = [], set()
    for job in found:
        if job["url"] in existing or job["url"] in seen:
            stats["dupes"] += 1
            continue
        seen.add(job["url"])
        new_jobs.append(job)
    stats["added"] = len(new_jobs)
    emit(f"{len(new_jobs)} new job(s) after dedupe ({stats['dupes']} skipped).", stage="sheet")

    fetch_dt = scraper.get_sgt_now()
    start_row = end_row = None
    if new_jobs and not params.dry_run:
        rows = [_build_row(j, fetch_dt, 0) for j in new_jobs]
        start_row, end_row = writer.append_jobs(rows)
        emit(f"Appended {len(rows)} row(s) -> sheet rows {start_row}-{end_row}.", stage="sheet")
        if start_row is not None:
            # Fix up Dupe? formulas with real row numbers (one batch write).
            writer.ws.batch_update(
                [
                    {"range": f"P{r}", "values": [[sheets.dupe_formula(r)]]}
                    for r in range(start_row, end_row + 1)
                ],
                value_input_option="USER_ENTERED",
            )
        for i, job in enumerate(new_jobs):
            job["sheet_row"] = (start_row + i) if start_row is not None else None
            job["db_id"] = db.add_job(run_id, user["email"], job)
    else:
        for job in new_jobs:
            job["sheet_row"] = None
            job["db_id"] = db.add_job(run_id, user["email"], job)
        if params.dry_run:
            emit("DRY RUN - skipping sheet writes.", stage="sheet")

    # --- Stage 3: LLM scoring ---
    emit(f"Stage 3/5: Scoring {len(new_jobs)} job(s) against your master resume...", stage="score")
    sheet_updates = []
    for job in new_jobs:
        if cancel_event.is_set():
            raise Cancelled()
        try:
            result = llm.assess_job(prompt_template, master_resume, job["description"], log=log)
            job.update(result)
            stats["scored"] += 1
            stats["tokens"] += result.get("tokens", 0)
            score_str = f"{result['score']}%" if result["score"] is not None else ""
            emit(f"  {score_str or '??'} - {job['company']}: {job['title']}")
            db.update_job(job["db_id"], {
                "score": result["score"],
                "skill_gaps": result["skill_gaps"],
                "tailored_bullets": result["tailored_bullets"],
            })
            if job.get("sheet_row") and not params.dry_run:
                sheet_updates.append(
                    (job["sheet_row"], score_str, result["skill_gaps"], result["tailored_bullets"])
                )
        except Exception as exc:
            stats["errors"] += 1
            stats["error_details"].append(f"Scoring {job['company']}: {exc}")
            emit(f"  Scoring failed for {job['url']}: {exc}")
    if sheet_updates:
        writer.update_assessments(sheet_updates)
        emit(f"Wrote {len(sheet_updates)} assessment(s) to the sheet.", stage="score")

    qualified = [j for j in new_jobs if j.get("score") is not None and j["score"] >= params.score_threshold]
    stats["qualified"] = len(qualified)
    stats["top_matches"] = [
        {"score": j["score"], "company": j["company"], "position": j["title"], "url": j["url"]}
        for j in sorted(qualified, key=lambda x: x["score"], reverse=True)
    ]

    # --- Stage 4: generate resume + cover letter ---
    emit(f"Stage 4/5: Generating documents for {len(qualified)} qualifying job(s)...", stage="generate")
    gen_updates = []
    run_dir = (
        Path(settings.OUTPUT_DIR)
        / user["email"]
        / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    for job in qualified:
        if cancel_event.is_set():
            raise Cancelled()
        try:
            resume_md, tokens = llm.generate_resume(master_resume, job["description"], job, log=log)
            stats["tokens"] += tokens
            cover_md, tokens = llm.generate_cover_letter(master_resume, job["description"], job, log=log)
            stats["tokens"] += tokens

            if params.dry_run:
                emit(f"  DRY RUN - would generate files for {job['company']}: {job['title']}")
                continue

            base = resume_mod.sanitize_filename(f"{job['company']}_{job['title']}")
            paths = resume_mod.save_documents(
                resume_md, cover_md, params.formats, run_dir / base, base
            )
            resume_paths = "\n".join(paths["resume"])
            cover_paths = "\n".join(paths["cover_letter"])
            stats["generated"] += 1
            db.update_job(job["db_id"], {
                "status": "Ready to Apply",
                "resume_path": resume_paths,
                "cover_letter_path": cover_paths,
            })
            if job.get("sheet_row"):
                gen_updates.append((job["sheet_row"], resume_paths, cover_paths, "Ready to Apply"))
            emit(f"  Generated {', '.join(params.formats)} for {job['company']}: {job['title']}")
        except Exception as exc:
            stats["errors"] += 1
            stats["error_details"].append(f"Generation {job['company']}: {exc}")
            emit(f"  Generation failed for {job['url']}: {exc}")
    if gen_updates:
        writer.update_generated(gen_updates)
        emit(f"Sheet updated: {len(gen_updates)} job(s) marked 'Ready to Apply'.", stage="generate")
    if stats["generated"]:
        stats["output_dir"] = str(run_dir)

    # --- Stage 5: log + notify ---
    emit("Stage 5/5: Logging run & sending Telegram summary...", stage="notify")
    if not params.dry_run:
        try:
            writer.log_run(
                scraper.format_sgt_timestamp(), _describe_params(params),
                stats["added"], start_row, end_row,
            )
        except Exception as exc:
            emit(f"Failed to write run log tab: {exc}")
    elapsed = int(time.time() - started)
    stats["duration"] = f"{elapsed // 60}m {elapsed % 60}s"
    stats["status"] = "complete"
    telegram.notify_run(stats, log=log)
    db.finish_run(run_id, "completed", stats=stats)
    emit(
        f"Done: {stats['added']} new, {stats['scored']} scored, "
        f"{stats['generated']} document set(s) generated in {stats['duration']}.",
        stage="notify",
    )
