# Job Hunt App

Version: 0.1.1 (see VERSION; scheme is major.minor.bugfix — minor bumps for
new features, bugfix bumps for fixes, major stays 0 until you say otherwise).

Web app that replaces the Colab workflow:
1. Enter LinkedIn search parameters & filter toggles in the UI.
2. Run the pipeline:
   a. Scrape LinkedIn guest API and append new jobs to the
      "Job_Applications_v3" Google Sheet (Database tab, columns A–Q).
   b. Score every new job against your master resume via an
      OpenAI-compatible LLM endpoint (Score / Skill Gaps / Tailored Bullets).
   c. For jobs at or above the score threshold, generate a tailored resume
      and cover letter (docx / pdf / md — selectable) into ./output and flip
      the sheet Status to "Ready to Apply".
   d. Send a Telegram summary of the run.

Auth is via Cloudflare Access (Google identity provider). Each authenticated
user gets their own profile: master resume, assessment-prompt override,
saved search defaults, and run history.

## One-time setup

### 1. Google service account
1. Google Cloud Console -> create/select a project -> enable "Google Sheets API".
2. Create a service account, download its JSON key.
3. Save the key as ./data/credentials.json (docker-compose points
   GOOGLE_APPLICATION_CREDENTIALS at /app/data/credentials.json).
4. Share the "Job_Applications_v3" sheet with the service account's
   client_email as Editor. Afterwards you can switch the sheet's link
   sharing back to "Restricted" so only you and the app can access it.

### 2. Secrets file (.venv/.secrets)
KEY=VALUE lines, e.g.:
    OPENAI_API_KEY=sk-...
    OPENAI_ENDPOINT=https://api.openai.com/v1
    TELEGRAM_BOT=123456:ABC-...
    TELEGRAM_CHAT_ID=123456789
    DEFAULT_JD_ASSESSMENT_PROMPT=/app/data/JD_Assessment_Prompt_Template.md
    CF_TEAM_DOMAIN=yourteam.cloudflareaccess.com
    CF_ACCESS_AUD=xxxxxxxx
See .env.example for the full list. Real environment variables override
values from this file.

IMPORTANT: .venv/ is gitignored, so this file is NOT copied by git
clone/pull. You must create it by hand on every machine that runs the app
(including psth1), next to docker-compose.yml. Because the file is only
read inside the container, any file paths in it must be container paths
(e.g. put the prompt template in ./data/ and reference /app/data/...).

### 3. Telegram
Create a bot via @BotFather to get TELEGRAM_BOT. Send the bot any message,
then read your chat id from:
    https://api.telegram.org/bot<TELEGRAM_BOT>/getUpdates

### 4. Cloudflare Access (Google IdP)
1. Cloudflare Zero Trust -> Settings -> Authentication -> add Google as a
   login method.
2. Access -> Applications -> add a self-hosted application covering the
   hostname you expose for this app (e.g. via a Cloudflare Tunnel on psth1).
3. Copy the team domain (Settings -> General, e.g. yourteam.cloudflareaccess.com)
   into CF_TEAM_DOMAIN and the application's AUD tag into CF_ACCESS_AUD.
If CF_TEAM_DOMAIN / CF_ACCESS_AUD are empty the app runs in dev mode with a
single local user — do not expose it publicly in that state.

## Build & run (psth1)

    docker compose build
    docker compose up -d

App listens on port 8503. Volumes:
    ./data    -> SQLite DB, credentials.json
    ./output  -> generated resumes & cover letters (per user / per run)
    ./.venv   -> read-only, only .secrets is read from it

## Troubleshooting

### "OPENAI_API_KEY is not configured" (or the UI warning banner)
The app reads .venv/.secrets once at process start. Check, on psth1:
1. The file exists next to docker-compose.yml:
       ls -la .venv/.secrets
   (.venv/ is gitignored - it is never copied by git clone/pull.)
2. It is a FILE, not a directory. If Docker created a directory named
   .secrets (happens when a bind-mount source is missing at first start),
   remove it and recreate the container:
       docker compose down
       rmdir .venv/.secrets        # only if it is an empty directory
       nano .venv/.secrets         # create the real file
       docker compose up -d
3. Restart after any edit to .secrets:
       docker compose restart
4. Confirm what the app actually loaded (values are never logged):
       docker logs jobhunt | grep -i -E "secrets|config status"
The UI also shows a warning banner listing exactly which integrations
(LLM / Sheets / Telegram) are not configured.

## Notes & limitations
- The assessment prompt template may use {resume} and {job_description}
  placeholders and must ask for JSON with keys: score, skill_gaps,
  tailored_bullets. A built-in default is used if no file is configured.
- PDF output uses built-in fonts; non-latin characters are transliterated.
  docx and md preserve full unicode.
- LinkedIn's guest endpoint is unofficial and rate-limited; the scraper is
  polite (sleeps between requests) and skips dead/404 postings.
