import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jobhunt.config")

BASE_DIR = Path(__file__).resolve().parent.parent

SECRETS_CANDIDATES = (BASE_DIR / ".venv" / ".secrets", BASE_DIR / ".env")

# Load secrets: real environment variables always win (override=False).
SECRETS_LOADED_FROM = []
for candidate in SECRETS_CANDIDATES:
    if candidate.is_file():
        load_dotenv(candidate, override=False)
        SECRETS_LOADED_FROM.append(str(candidate))
    elif candidate.exists():
        # A directory here means Docker created it for a missing bind-mount
        # source - dotenv cannot read it, so say so loudly.
        log.warning(
            "%s exists but is not a file (Docker created a directory for a "
            "missing bind-mount source?). Secrets NOT loaded from it.",
            candidate,
        )

if SECRETS_LOADED_FROM:
    log.info("Secrets loaded from: %s", ", ".join(SECRETS_LOADED_FROM))
else:
    log.warning(
        "No secrets file found. Looked for: %s. Create .venv/.secrets next to "
        "docker-compose.yml on the host, then restart the container.",
        ", ".join(str(c) for c in SECRETS_CANDIDATES),
    )


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _missing(*keys: str) -> list:
    """Names of the given env keys that are unset or empty.

    Only key NAMES are ever reported - values are never logged or exposed.
    """
    return [k for k in keys if not _env(k)]


def _read_version() -> str:
    version_file = BASE_DIR / "VERSION"
    return version_file.read_text().strip() if version_file.exists() else "0.1.5"


class Settings:
    VERSION = _read_version()
    PORT = int(_env("PORT", "8503"))
    TIMEZONE = _env("APP_TIMEZONE", "Asia/Singapore")

    DATA_DIR = Path(_env("DATA_DIR", str(BASE_DIR / "data")))
    OUTPUT_DIR = Path(_env("OUTPUT_DIR", str(BASE_DIR / "output")))

    # Google Sheets
    GOOGLE_APPLICATION_CREDENTIALS = _env("GOOGLE_APPLICATION_CREDENTIALS")
    GOOGLE_SHEET_ID = _env("GOOGLE_SHEET_ID", "1yspWca6Fqp_YuSB9ZkPPtyxO3RZSCCNNamVHTy7kbSE")
    GOOGLE_SHEET_NAME = _env("GOOGLE_SHEET_NAME", "Job_Applications_v3")

    # LLM (OpenAI-compatible endpoint)
    OPENAI_API_KEY = _env("OPENAI_API_KEY")
    OPENAI_ENDPOINT = _env("OPENAI_ENDPOINT") or None
    OPENAI_MODEL_SCORE = _env("OPENAI_MODEL_SCORE", "gpt-4o-mini")
    OPENAI_MODEL_GENERATE = _env("OPENAI_MODEL_GENERATE", "gpt-4o")
    DEFAULT_JD_ASSESSMENT_PROMPT = _env("DEFAULT_JD_ASSESSMENT_PROMPT") or _env("DEFAULT_ASSESSMENT_PROMPT")

    # Telegram
    TELEGRAM_BOT = _env("TELEGRAM_BOT")
    TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")

    # Cloudflare Access (Google IdP). Empty -> dev mode (single local user).
    CF_TEAM_DOMAIN = _env("CF_TEAM_DOMAIN")
    CF_ACCESS_AUD = _env("CF_ACCESS_AUD")
    # Public URL of this app behind Access, e.g. https://jobhunt.example.com.
    # Only used to render a "continue" link on the direct-access 401 page.
    CF_APP_URL = _env("CF_APP_URL")

    SCORE_THRESHOLD = int(_env("SCORE_THRESHOLD", "65"))

    # Scraper defaults (prefill the UI; everything is editable per run)
    DEFAULT_KEYWORDS = [
        "Technical Project Manager",
        "IT Project Manager",
        "Fintech Project Manager",
        "Digital Transformation Program Manager",
        "Fintech/Payments Program Manager",
        "Healthcare IT/Digital Health Program Manager",
        "PMO Director/Head of PMO",
        "Independent Management Consultant (Digital/PMO)",
    ]
    DEFAULT_LOCATION = "Singapore"
    DEFAULT_OFFSETS = [0]
    DEFAULT_LIMIT = 50
    DEFAULT_EMPLOYMENT_TYPES = ["Full-time", "Contract", "Part-time", "Temporary"]
    DEFAULT_EXPERIENCE_LEVELS = ["Executive", "Director", "Mid-Senior level"]
    DEFAULT_FORMATS = ["docx", "md"]

    def ensure_dirs(self):
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def secrets_loaded_from(self) -> list:
        return list(SECRETS_LOADED_FROM)

    @property
    def cloudflare_enabled(self) -> bool:
        return bool(self.CF_TEAM_DOMAIN and self.CF_ACCESS_AUD)

    @property
    def llm_configured(self) -> bool:
        return bool(self.OPENAI_API_KEY)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.TELEGRAM_BOT and self.TELEGRAM_CHAT_ID)

    @property
    def sheets_configured(self) -> bool:
        return bool(self.GOOGLE_APPLICATION_CREDENTIALS)

    @property
    def sheets_credentials_file_exists(self) -> bool:
        """Whether the service-account JSON file is actually present."""
        if not self.GOOGLE_APPLICATION_CREDENTIALS:
            return False
        return Path(self.GOOGLE_APPLICATION_CREDENTIALS).is_file()

    @property
    def missing_keys(self) -> dict:
        """Per-integration list of unset/empty env key names (never values)."""
        return {
            "llm": _missing("OPENAI_API_KEY"),
            "sheets": _missing("GOOGLE_APPLICATION_CREDENTIALS"),
            "telegram": _missing("TELEGRAM_BOT", "TELEGRAM_CHAT_ID"),
            "cloudflare": _missing("CF_TEAM_DOMAIN", "CF_ACCESS_AUD"),
        }


settings = Settings()
settings.ensure_dirs()


def _fmt_status(name: str, missing: list) -> str:
    if not missing:
        return f"{name}=True"
    return f"{name}=False (missing: {', '.join(missing)})"


_missing_map = settings.missing_keys
if settings.cloudflare_enabled:
    _cf_status = "cloudflare=True"
elif len(_missing_map["cloudflare"]) == 2:
    _cf_status = "cloudflare=dev-mode (CF_TEAM_DOMAIN/CF_ACCESS_AUD unset)"
else:
    # Only one of the two CF vars set - a broken half-configuration.
    _cf_status = _fmt_status("cloudflare", _missing_map["cloudflare"])

log.info(
    "Config status: %s | %s | %s | %s",
    _fmt_status("llm", _missing_map["llm"]),
    _fmt_status("sheets", _missing_map["sheets"]),
    _fmt_status("telegram", _missing_map["telegram"]),
    _cf_status,
)

# The credentials env var can be set while the file itself is absent (e.g. the
# host path was never copied into ./data). Surface that distinctly.
if settings.sheets_configured:
    if settings.sheets_credentials_file_exists:
        log.info("Config status: sheets-creds-file=OK (%s)", settings.GOOGLE_APPLICATION_CREDENTIALS)
    else:
        log.warning(
            "Config status: sheets-creds-file=MISSING (%s). Place the service-account "
            "JSON key at ./data/credentials.json on the host (it is mounted to /app/data) "
            "and restart.",
            settings.GOOGLE_APPLICATION_CREDENTIALS,
        )
