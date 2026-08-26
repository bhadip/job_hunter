import datetime
import re
import time

import pytz
import requests
from bs4 import BeautifulSoup

from .config import settings

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

EMPLOYMENT_TYPE_CODES = {
    "Full-time": "F", "Part-time": "P", "Contract": "C",
    "Temporary": "T", "Internship": "I", "Volunteer": "V",
}
EXPERIENCE_LEVEL_CODES = {
    "Internship": "1", "Entry level": "2", "Associate": "3",
    "Mid-Senior level": "4", "Director": "5", "Executive": "6",
}


def get_sgt_now() -> datetime.datetime:
    return datetime.datetime.now(pytz.timezone(settings.TIMEZONE))


def format_sgt_timestamp(dt: datetime.datetime = None) -> str:
    return (dt or get_sgt_now()).strftime("%Y-%m-%d %H:%M:%S")


def build_filter_params(params) -> dict:
    out = {}
    if params.easy_apply:
        out["f_AL"] = "true"
    codes = [EMPLOYMENT_TYPE_CODES[t] for t in params.employment_types if t in EMPLOYMENT_TYPE_CODES]
    if codes:
        out["f_JT"] = ",".join(codes)
    codes = [EXPERIENCE_LEVEL_CODES[e] for e in params.experience_levels if e in EXPERIENCE_LEVEL_CODES]
    if codes:
        out["f_E"] = ",".join(codes)
    # LinkedIn's guest API only accepts distance in miles; the UI collects km.
    if params.distance_km:
        out["distance"] = str(max(1, round(params.distance_km * 0.621371)))
    return out


def extract_applicant_count(text: str):
    match = re.search(r"(\d+)\s+applicant", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _get_with_retry(url: str, params: dict = None, max_attempts: int = 3, base_delay: int = 5, log=print):
    """GET with retry+backoff on 5xx responses and network errors.

    Returns None if the request never succeeded at the network level;
    otherwise returns the last response (caller checks status_code).
    """
    response = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=15)
        except requests.RequestException as exc:
            if attempt < max_attempts:
                delay = base_delay * attempt
                log(f"Network error ({exc}); retrying in {delay}s (attempt {attempt}/{max_attempts})...")
                time.sleep(delay)
                continue
            log(f"Request to {url} failed after {max_attempts} attempts: {exc}")
            return None
        if response.status_code < 500 or attempt == max_attempts:
            return response
        delay = base_delay * attempt
        log(
            f"HTTP {response.status_code} from LinkedIn; retrying in {delay}s "
            f"(attempt {attempt}/{max_attempts})..."
        )
        time.sleep(delay)
    return response


def _parse_posted_date(time_tag) -> datetime.date:
    if not time_tag:
        return None
    raw = time_tag.get_text(strip=True).lower()
    now = get_sgt_now()

    if re.search(r"(\d+)\s+hour(s)?\s+ago", raw) or "today" in raw:
        return now.date()
    if "yesterday" in raw:
        return (now - datetime.timedelta(days=1)).date()

    rel = re.search(r"(\d+)\s+(day|week|month)s?\s+ago", raw)
    if rel:
        value, unit = int(rel.group(1)), rel.group(2)
        delta = None
        if "day" in unit:
            delta = datetime.timedelta(days=value)
        elif "week" in unit:
            delta = datetime.timedelta(weeks=value)
        elif "month" in unit:
            delta = datetime.timedelta(days=value * 30)
        if delta:
            return (now - delta).date()

    for pattern, fmt in (
        (r"[a-zA-Z]{3,}\s+\d{1,2},\s+\d{4}", "%B %d, %Y"),
        (r"[a-zA-Z]{3}\s+\d{1,2},\s+\d{4}", "%b %d, %Y"),
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
    ):
        if re.search(pattern, raw, re.IGNORECASE):
            try:
                return datetime.datetime.strptime(raw.title() if "%" not in fmt else raw, fmt).date()
            except ValueError:
                continue
    # datetime attribute fallback: <time datetime="2026-07-28">
    dt_attr = time_tag.get("datetime", "")
    if re.match(r"\d{4}-\d{2}-\d{2}", dt_attr):
        try:
            return datetime.datetime.strptime(dt_attr[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def _is_dead_posting(html: str, description: str) -> bool:
    if "trk=404_page" in html or "trk=%7Berror-page%7D" in html:
        return True
    lowered = description.lower()
    return "page not found" in lowered and "help center" in lowered


def fetch_jobs(params, keyword: str, offset: int, log=print) -> list:
    """Fetch job dicts for one keyword/offset combo. Ported from scraper v5."""
    jobs = []
    crawl_time = format_sgt_timestamp()
    try:
        query = {"keywords": keyword, "location": params.location, "start": offset}
        query.update(build_filter_params(params))
        res = _get_with_retry(SEARCH_URL, params=query, log=log)
        if res is None:
            return []
        if res.status_code != 200:
            log(f"Search HTTP {res.status_code} for '{keyword}' (offset {offset}) after retries")
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        cards = soup.select("li")

        for card in cards[: params.limit]:
            link_tag = card.find(
                "a", class_=lambda x: x and ("result-card" in x or "card__full-link" in x)
            )
            if not link_tag or not link_tag.get("href"):
                continue
            job_url = link_tag["href"].split("?")[0].strip()
            if not job_url:
                continue

            title_tag = card.select_one(".base-search-card__title")
            company_tag = card.select_one(".base-search-card__subtitle")
            location_tag = card.select_one(".job-search-card__location")
            title = title_tag.get_text(strip=True) if title_tag else "Unknown"
            company = company_tag.get_text(strip=True) if company_tag else "Unknown"
            location = (
                location_tag.get_text(strip=True) if location_tag else params.location
            )

            posted_date = _parse_posted_date(card.select_one("time"))
            posted_str = posted_date.strftime("%Y-%m-%d") if posted_date else ""

            job_id = job_url.split("-")[-1].split("/")[-1]
            desc_res = _get_with_retry(POSTING_URL.format(job_id=job_id), log=log)
            description = "Manual review required."
            if desc_res is not None and desc_res.status_code == 200:
                desc_soup = BeautifulSoup(desc_res.text, "html.parser")
                desc_tag = desc_soup.select_one(
                    ".description__text, .show-more-less-html__markup"
                )
                if desc_tag:
                    description = desc_tag.get_text(separator="\n", strip=True)
                if _is_dead_posting(desc_res.text, description):
                    log(f"Skipping '{title}' ({company}) - posting no longer exists (404).")
                    continue
            elif desc_res is None:
                description = "Manual review required (network error on description fetch)."
            else:
                description = (
                    f"Manual review required (HTTP {desc_res.status_code} "
                    "error on description fetch)."
                )

            if "no longer accepting applications" in description.lower():
                log(f"Skipping '{title}' ({company}) - no longer accepting applications.")
                continue
            if description.startswith("Manual review required"):
                log(f"Skipping '{title}' ({company}) - no meaningful description found.")
                continue

            if posted_date:
                one_month_ago = (get_sgt_now() - datetime.timedelta(days=30)).date()
                if posted_date < one_month_ago:
                    log(f"Skipping '{title}' ({company}) - posted over a month ago ({posted_str}).")
                    continue

            if params.under_10_applicants:
                count = extract_applicant_count(description)
                if count is not None and count >= 10:
                    log(f"Skipping '{title}' ({company}) - {count} applicants (>=10).")
                    continue

            jobs.append(
                {
                    "url": job_url,
                    "title": title,
                    "company": company,
                    "location": location,
                    "description": (
                        f"TITLE: {title}\nCOMPANY: {company}\n"
                        f"SOURCE: linkedin-guest\n\n{description}"
                    ),
                    "posted_date": posted_str,
                    "crawl_time": crawl_time,
                }
            )
            time.sleep(1.0)  # be polite to LinkedIn's guest endpoint
    except Exception as exc:  # keep one bad keyword from killing the run
        log(f"Scraper error for '{keyword}' (offset {offset}): {exc}")
    return jobs
