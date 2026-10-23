"""Runtime configuration (read from environment / .env)."""
import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# --- Liara ---
LIARA_API_TOKEN = os.getenv("LIARA_API_TOKEN", "")
# Same base URL the official liara-cli uses (src/constants.ts -> REGIONS_API_URL)
LIARA_API_BASE = os.getenv("LIARA_API_BASE", "https://api.liara.ir")
LIARA_TEAM_ID = os.getenv("LIARA_TEAM_ID", "")
# Resource metrics are served from the Iran region API (observed in console.liara.ir network traffic)
LIARA_METRICS_BASE = os.getenv("LIARA_METRICS_BASE", "https://api.iran.liara.ir")
# Mock mode: simulates Liara locally (for tests / offline rehearsal). Auto-on when no token.
MOCK_MODE = _bool("MOCK_MODE", default=not bool(LIARA_API_TOKEN))

# --- LLM (any OpenAI-compatible endpoint: Liara AI, OpenAI, OpenRouter, ...) ---
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")  # e.g. https://ai.liara.ir/api/v1/<id>
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")

# --- GitHub (optional, links log stack traces to source lines) ---
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # owner/repo
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# --- Safety ---
# "strict": every write needs approval. "normal": low-risk writes auto-run.
APPROVAL_MODE = os.getenv("APPROVAL_MODE", "normal")
AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "audit.log.jsonl")

PORT = int(os.getenv("PORT", "8000"))

# Where conversation history and app->project mapping are stored
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))

# Speech-to-text for the mic button (any OpenAI-compatible /audio/transcriptions endpoint, e.g. Liara AI).
# If empty, the browser's built-in speech recognition is used instead.
STT_API_KEY = os.getenv("STT_API_KEY", "")
STT_BASE_URL = os.getenv("STT_BASE_URL", "")
STT_MODEL = os.getenv("STT_MODEL", "whisper-1")
