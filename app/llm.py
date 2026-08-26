import json
import logging
import re
import time

from openai import APIConnectionError, APIError, OpenAI, RateLimitError

from .config import settings

log = logging.getLogger("jobhunt.llm")

MAX_JD_CHARS = 12000  # token-cost guard for scoring

# Appended to every assessment prompt (built-in default or custom template) so
# the pipeline always gets machine-readable output back, even when the template
# itself was written for interactive use and specifies no output format.
JSON_OUTPUT_INSTRUCTION = """

---
OUTPUT FORMAT (mandatory - overrides any other formatting instruction above):
After completing your assessment, respond with ONLY a single valid JSON object
- no markdown fences, no commentary before or after - with exactly these keys:
{
  "score": <integer 0-100>,
  "skill_gaps": "<1-2 sentences: what the JD requires that the resume lacks>",
  "tailored_bullets": "<2-4 sentences: concrete strategy for tailoring the resume to this JD>"
}"""

BUILTIN_ASSESSMENT_PROMPT = """You are an expert career coach evaluating how well a candidate's
master resume matches a job description.

Assess honestly across: domain/industry alignment, seniority fit, required
vs preferred skills, and transferable experience. Do not inflate the score.
A score of 65+ means the candidate is a credible applicant worth tailoring
a resume for."""

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
    """Fill {resume}/{job_description} placeholders if present, then ALWAYS
    append both documents in clearly-marked sections.

    Templates written for interactive use often lack placeholders or phrase
    them differently, which previously meant the model received no content at
    all. Appending unconditionally guarantees the model always sees both.
    """
    out = template
    for key, val in (("resume", resume), ("job_description", job_description)):
        out = out.replace("{" + key + "}", val).replace("{{" + key + "}}", val)
    out += (
        "\n\n=== CANDIDATE MASTER RESUME ===\n"
        + resume
        + "\n\n=== JOB DESCRIPTION ===\n"
        + job_description
    )
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


def _chat(model: str, user_prompt: str, max_tokens: int, log=print, json_mode: bool = False) -> tuple:
    """Returns (text, total_tokens). Retries with backoff on transient errors.

    json_mode asks the API for guaranteed JSON output; if the endpoint rejects
    response_format, it is dropped automatically and the call retried plainly.
    """
    client = _client()
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    for attempt in range(1, 5):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": user_prompt}],
                max_tokens=max_tokens,
                temperature=0.3,
                **kwargs,
            )
            usage = getattr(resp, "usage", None)
            tokens = (usage.prompt_tokens + usage.completion_tokens) if usage else 0
            return resp.choices[0].message.content.strip(), tokens
        except (RateLimitError, APIConnectionError, APIError) as exc:
            if json_mode and "response_format" in str(exc):
                log("[LLM] Endpoint rejected response_format; retrying without JSON mode.")
                json_mode = False
                kwargs.pop("response_format", None)
                continue
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


def _fallback_parse(text: str, log=print) -> dict:
    """Last-resort extraction when the model ignored the JSON instruction.

    Surfaces the raw response in the run log so the prompt template can be
    debugged, then tries to salvage at least the score via regex.
    """
    log(
        "[LLM] Response was not valid JSON; attempting score extraction. "
        f"Raw response (first 400 chars): {text[:400]!r}"
    )
    match = re.search(r'"score"\s*:\s*"?(\d{1,3})', text, re.IGNORECASE)
    if not match:
        match = re.search(r"\bscore\b[^\d]{0,25}(\d{1,3})\s*(?:%|/100|percent)?", text, re.IGNORECASE)
    if match:
        value = int(match.group(1))
        if 0 <= value <= 100:
            log(f"[LLM] Fallback extracted score={value} from non-JSON response.")
            return {
                "score": value,
                "skill_gaps": "(LLM returned a non-JSON response; only the score could be extracted)",
                "tailored_bullets": "",
            }
    raise ValueError("LLM response did not contain JSON or a recognizable score")


def assess_job(prompt_template: str, resume: str, job_description: str, log=print) -> dict:
    prompt = fill_template(prompt_template, resume, job_description[:MAX_JD_CHARS])
    prompt += JSON_OUTPUT_INSTRUCTION
    text, tokens = _chat(
        settings.OPENAI_MODEL_SCORE,
        prompt,
        max_tokens=2000,
        log=log,
        json_mode=settings.OPENAI_JSON_MODE,
    )
    try:
        data = _parse_json(text)
    except ValueError:
        data = _fallback_parse(text, log)
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
