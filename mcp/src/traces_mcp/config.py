"""Configuration for the local traces MCP / sourcing server.

All secrets live in the repo-root `.env` (see `.env.example`). No Supabase,
Stripe, auth, or billing — this runs entirely on your machine.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load the repo-root .env (traces/.env). Walk up from this file until we find
# one, so the package works whether run from the repo or installed editable.
_HERE = Path(__file__).resolve()
for _candidate in (
    _HERE.parents[3] / ".env",   # traces/.env
    _HERE.parents[2] / ".env",   # mcp/.env
):
    if _candidate.exists():
        load_dotenv(_candidate)
        break
else:
    load_dotenv()  # fall back to CWD / process env

# --- AI provider ------------------------------------------------------------
# If OPENROUTER_API_KEY is set we use OpenRouter; otherwise we fall back to a
# local Ollama instance (OpenAI-compatible API). Everything downstream — the
# Node sourcing agents and the netlist checks — talks the OpenAI chat-completions
# shape, so the only difference is the base URL, key, and model.
OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")
DEFAULT_MODEL: str = os.environ.get("DEFAULT_MODEL", "google/gemini-3.1-flash-lite")

OLLAMA_BASE_URL: str = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
# Default local model. Must support tool-calling for sourcing (verified with
# gemma4; qwen2.5 / llama3.1 also work). Override with OLLAMA_MODEL.
OLLAMA_MODEL: str = os.environ.get("OLLAMA_MODEL", "gemma4")


def llm_provider() -> str:
    return "openrouter" if OPENROUTER_API_KEY else "ollama"


def llm_settings(model: str | None = None) -> tuple[str, str, str]:
    """Return (base_url, api_key, model) for the active provider."""
    if OPENROUTER_API_KEY:
        return "https://openrouter.ai/api/v1", OPENROUTER_API_KEY, model or DEFAULT_MODEL
    return OLLAMA_BASE_URL, os.environ.get("OLLAMA_API_KEY", "ollama"), model or OLLAMA_MODEL

# --- Supplier credentials ---------------------------------------------------
MOUSER_API_KEY: str = os.environ.get("MOUSER_API_KEY") or os.environ.get("MOUSER_PART_API_KEY", "")
DIGIKEY_CLIENT_ID: str = os.environ.get("DIGIKEY_CLIENT_ID", "")
DIGIKEY_CLIENT_SECRET: str = os.environ.get("DIGIKEY_CLIENT_SECRET", "")
# JLCPCB/LCSC search needs no key.

# --- Local HTTP server (used by the KiCad extension) ------------------------
HOST: str = os.environ.get("TRACES_HOST", "127.0.0.1")
PORT: int = int(os.environ.get("TRACES_PORT", "8000"))


def resolve_model(model: str | None = None) -> str:
    return model or DEFAULT_MODEL
