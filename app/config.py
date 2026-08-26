import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load secrets: real environment variables always win (override=False).
for candidate in (BASE_DIR / ".venv" / ".secrets", BASE_DIR / ".env"):
    if candidate.exists():
        load_dotenv(candidate, override=False)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _read_version() -> str:
    version_file = BASE_DIR / "VERSION"
    return version_file.read_text().strip() if version_file.exists() else "0.1.0"


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


settings = Settings()
settings.ensure_dirs()
