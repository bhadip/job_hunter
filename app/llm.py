import json
import logging
import time

from openai import APIConnectionError, APIError, OpenAI, RateLimitError

from .config import settings

log = logging.getLogger("jobhunt.llm")

MAX_JD_CHARS = 12000  # token-cost guard for scoring

BUILTIN_ASSESSMENT_PROMPT = """You are an expert career coach evaluating how well a candidate's
master resume matches a job description.

Assess honestly across: domain/industry alignment, seniority fit, required
vs preferred skills, and transferable experience. Do not inflate the score.
A score of 65+ means the candidate is a credible applicant worth tailoring
a resume for.

=== CANDIDATE MASTER RESUME ===
{resume}

=== JOB DESCRIPTION ===
{job_description}

Respond with ONLY valid JSON (no markdown fences, no commentary):
{{
  "score": <integer 0-100>,
  "skill_gaps": "<1-2 sentences: what the JD requires that the resume lacks>",
  "tailored_bullets": "<2-4 sentences: concrete strategy for tailoring the resume to this JD>"
}}"""

RESUME_PROMPT = """You are an expert resume writer. Rewrite the candidate's master resume,
tailored to the job description below.

Rules:
- Output Markdown only.
- Never invent experience, skills, employers, dates, or certifications.
- Reorder, rephrase, and emphasize existing real experience to match the JD.
- Use the assessment's tailoring strategy.
- Keep it to one page worth of content where possible.

=== MASTER RESUME ===
{resume}

=== JOB DESCRIPTION ===
{job_description}

=== ASSESSMENT ===
Score: {score}
Skill gaps: {skill_gaps}
Tailoring strategy: {tailored_bullets}"""

COVER_LETTER_PROMPT = """You are an expert cover letter writer. Write a cover letter for the
candidate below, for the job description below.

Rules:
- Output Markdown only.
- 3-4 short paragraphs. Professional, direct, no fluff.
- Reference 1-2 specific requirements from the JD and map them to real
  experience from the resume. Never invent experience.
- If the assessment notes a gap, address it honestly as transferable
  experience rather than claiming the skill.

=== MASTER RESUME ===
{resume}

=== JOB DESCRIPTION ===
{job_description}

=== ASSESSMENT ===
Score: {score}
Skill gaps: {skill_gaps}
Tailoring strategy: {tailored_bullets}"""


def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_ENDPOINT)


def fill_template(template: str, resume: str, job_description: str) -> str:
    out = template
    for key, val in (("resume", resume), ("job_description", job_description)):
        out = out.replace("{" + key + "}", val).replace("{{" + key + "}}", val)
    return out


def load_default_assessment_prompt() -> str:
    path = settings.resolved_assessment_prompt_path
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read().strip()
                if content:
                    return content
            log.warning("Assessment prompt file %s is empty; using built-in default.", path)
        except OSError as exc:
            log.warning(
                "Cannot read assessment prompt file %s (%s); using built-in default. "
                "Note: inside Docker the path must be a container path, e.g. /app/data/...",
                path, exc,
            )
    return BUILTIN_ASSESSMENT_PROMPT


def _chat(model: str, user_prompt: str, max_tokens: int, log=print) -> tuple:
    """Returns (text, total_tokens). Retries with backoff on transient errors."""
    client = _client()
    for attempt in range(1, 5):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": user_prompt}],
                max_tokens=max_tokens,
                temperature=0.3,
            )
            usage = getattr(resp, "usage", None)
            tokens = (usage.prompt_tokens + usage.completion_tokens) if usage else 0
            return resp.choices[0].message.content.strip(), tokens
        except (RateLimitError, APIConnectionError, APIError) as exc:
            if attempt == 4:
                raise
            delay = 10 * attempt
            log(f"[LLM] {type(exc).__name__}, attempt {attempt}/4. Waiting {delay}s...")
            time.sleep(delay)


def _parse_json(text: str) -> dict:
    cleaned = text.strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                cleaned = part
                break
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("LLM response did not contain JSON")
    return json.loads(cleaned[start : end + 1])


def assess_job(prompt_template: str, resume: str, job_description: str, log=print) -> dict:
    prompt = fill_template(prompt_template, resume, job_description[:MAX_JD_CHARS])
    text, tokens = _chat(settings.OPENAI_MODEL_SCORE, prompt, max_tokens=800, log=log)
    data = _parse_json(text)
    score = data.get("score")
    try:
        score = max(0, min(100, int(round(float(score)))))
    except (TypeError, ValueError):
        score = None
    return {
        "score": score,
        "skill_gaps": str(data.get("skill_gaps", "")).strip(),
        "tailored_bullets": str(data.get("tailored_bullets", "")).strip(),
        "tokens": tokens,
    }


def generate_resume(resume: str, job_description: str, assessment: dict, log=print) -> tuple:
    prompt = RESUME_PROMPT.format(
        resume=resume,
        job_description=job_description[:MAX_JD_CHARS],
        score=assessment.get("score"),
        skill_gaps=assessment.get("skill_gaps", ""),
        tailored_bullets=assessment.get("tailored_bullets", ""),
    )
    return _chat(settings.OPENAI_MODEL_GENERATE, prompt, max_tokens=3000, log=log)


def generate_cover_letter(resume: str, job_description: str, assessment: dict, log=print) -> tuple:
    prompt = COVER_LETTER_PROMPT.format(
        resume=resume,
        job_description=job_description[:MAX_JD_CHARS],
        score=assessment.get("score"),
        skill_gaps=assessment.get("skill_gaps", ""),
        tailored_bullets=assessment.get("tailored_bullets", ""),
    )
    return _chat(settings.OPENAI_MODEL_GENERATE, prompt, max_tokens=1500, log=log)
