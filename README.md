# Job Hunt App

Version: 0.1.5 (see VERSION; scheme is major.minor.bugfix — minor bumps for
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

IMPORTANT: the credentials file must live at ./data/credentials.json on the
host. The container only mounts ./data, ./output and ./.venv - a key placed
anywhere else (e.g. the repo root) is invisible inside the container. Also,
docker-compose sets GOOGLE_APPLICATION_CREDENTIALS=/app/data/credentials.json
as a container env var, which OVERRIDES any value in .venv/.secrets - so do
not rely on .secrets to point at the key; just put the file at ./data/.

### 2. Secrets file (.venv/.secrets)
KEY=VALUE lines, e.g.:
    OPENAI_API_KEY=sk-...
    OPENAI_ENDPOINT=https://api.openai.com/v1
    TELEGRAM_BOT=123456:ABC-...
    TELEGRAM_CHAT_ID=123456789
    DEFAULT_JD_ASSESSMENT_PROMPT=/app/data/JD_Assessment_Prompt_Template.md
    CF_TEAM_DOMAIN=yourteam.cloudflareaccess.com
    CF_ACCESS_AUD=xxxxxxxx
    CF_APP_URL=https://jobhunt.yourdomain.com
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
BOTH TELEGRAM_BOT and TELEGRAM_CHAT_ID must be set for notifications to work.

### 4. Cloudflare Access (Google IdP)
A tunnel route alone is NOT enough - you also need an Access Application
covering the hostname, otherwise no valid identity token is issued.
1. Cloudflare Zero Trust -> Settings -> Authentication -> add Google as a
   login method.
2. Access -> Applications -> Add -> Self-hosted. Set the application domain
   to the exact hostname (e.g. subdomain "jobhunter", domain "prasanti.com").
   Add an Allow policy (e.g. your email) and select Google as the IdP.
3. Copy the team domain (Settings -> General, e.g. yourteam.cloudflareaccess.com)
   into CF_TEAM_DOMAIN, and THIS application's App AUD tag (Application ->
   Overview) into CF_ACCESS_AUD.
4. Optionally set CF_APP_URL to the app's public URL - it is shown as a
   "continue" link on the page served to browsers that hit the app directly.
If CF_TEAM_DOMAIN / CF_ACCESS_AUD are empty the app runs in dev mode with a
single local user — do not expose it publicly in that state.

IMPORTANT: once Cloudflare auth is enabled, the app is ONLY reachable via the
Cloudflare-protected hostname. Cloudflare injects the identity token at its
edge, so requests that bypass Cloudflare (e.g. http://<server-ip>:8503) carry
no identity and are rejected with 401 - in every browser, incognito or not.
This is intentional fail-closed behavior.

## Build & run (psth1)

    docker compose build
    docker compose up -d

App listens on port 8503. Volumes:
    ./data    -> SQLite DB, credentials.json
    ./output  -> generated resumes & cover letters (per user / per run)
    ./.venv   -> read-only, only .secrets is read from it

## Troubleshooting

### "Google credentials file not found" / Errno 2 on credentials.json
The app reads the service-account key inside the container at
/app/data/credentials.json. Fix:
1. Copy the key into the mounted data dir on the host:
       cp /path/to/your-key.json ./data/credentials.json
2. Restart:
       docker compose restart
Confirm at startup:
       docker logs jobhunt | grep -i "sheets-creds-file"
   -> should print "sheets-creds-file=OK".
Note: docker-compose hard-sets GOOGLE_APPLICATION_CREDENTIALS to that
container path, overriding any host path in .venv/.secrets.

### "Missing Cloudflare Access credentials" (401) when opening the app
You are opening the app by IP:port (e.g. http://192.168.50.2:8503), which
bypasses Cloudflare entirely - no identity token exists on that path, so the
app rejects the request. Open it via the Cloudflare-protected hostname from
your Access application instead (e.g. https://jobhunter.prasanti.com). This
applies to all browsers; incognito vs regular makes no difference. If you
want direct LAN access without auth, remove CF_TEAM_DOMAIN / CF_ACCESS_AUD
from .venv/.secrets and restart (dev mode - LAN only).

### "Invalid Cloudflare Access token" (403) even via the tunnel hostname
A token WAS sent but failed verification. Two usual causes:
1. No Access Application covers the hostname. A tunnel "Published
   Application Route" only forwards traffic; it does not enable Access.
   Create the Self-hosted application for the exact hostname (see setup #4).
2. CF_ACCESS_AUD is wrong. It must be the App AUD of the application that
   covers this hostname, not another app's AUD and not your team domain.
Check the specific reason in the logs:
    docker logs jobhunt | grep -i "access token"

### "X is not configured" (or the UI warning banner)
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
   The status line names the exact missing keys, e.g.:
       telegram=False (missing: TELEGRAM_CHAT_ID)
The UI also shows a warning banner listing exactly which keys are missing
per integration (LLM / Sheets / Telegram).

### telegram=False despite Telegram settings in .secrets
Both TELEGRAM_BOT and TELEGRAM_CHAT_ID must be set and non-empty. Check:
- Key names are exactly TELEGRAM_BOT and TELEGRAM_CHAT_ID
  (not TELEGRAM_BOT_TOKEN, TELEGRAM_TOKEN, TELEGRAM_CHATID, ...).
- Lines are KEY=VALUE format (a line without "=" parses as empty).
- No earlier empty duplicate line (the first occurrence wins).
- The container was restarted after the edit.
List the key names in your file without exposing any values:
    cut -d= -f1 .venv/.secrets

## Notes & limitations
- The assessment prompt template may use {resume} and {job_description}
  placeholders and must ask for JSON with keys: score, skill_gaps,
  tailored_bullets. A built-in default is used if no file is configured.
- PDF output uses built-in fonts; non-latin characters are transliterated.
  docx and md preserve full unicode.
- LinkedIn's guest endpoint is unofficial and rate-limited; the scraper is
  polite (sleeps between requests) and skips dead/404 postings.
