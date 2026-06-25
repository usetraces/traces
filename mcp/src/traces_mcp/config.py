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


def _openrouter_settings(model: str | None) -> tuple[str, str, str]:
    return "https://openrouter.ai/api/v1", OPENROUTER_API_KEY, model or DEFAULT_MODEL


def _ollama_settings(model: str | None) -> tuple[str, str, str]:
    return OLLAMA_BASE_URL, os.environ.get("OLLAMA_API_KEY", "ollama"), model or OLLAMA_MODEL


def llm_settings(model: str | None = None) -> tuple[str, str, str]:
    """Return (base_url, api_key, model) for the preferred provider."""
    if OPENROUTER_API_KEY:
        return _openrouter_settings(model)
    return _ollama_settings(model)


def llm_provider_chain(model: str | None = None) -> list[tuple[str, str, str]]:
    """Providers to try, in order: OpenRouter first if a key is set, then the
    local Ollama fallback. Call sites walk this list so a missing *or broken*
    OpenRouter key transparently falls back to the local model."""
    if OPENROUTER_API_KEY:
        return [_openrouter_settings(model), _ollama_settings(model)]
    return [_ollama_settings(model)]


def describe_model(base_url: str, model: str) -> dict:
    """Human-readable summary of which model answered a request, so the MCP and
    KiCad extension can show 'cloud (openrouter): gemini-3.1-flash-lite' or
    'local (ollama): gemma4'."""
    is_local = "openrouter" not in base_url
    service = "ollama" if is_local else "openrouter"
    return {
        "location": "local" if is_local else "cloud",
        "service": service,
        "model": model,
        "label": f"{'local' if is_local else 'cloud'} ({service}): {model}",
    }


def active_model() -> dict:
    """The model that *would* answer right now (the chain's first choice). Used
    for status display before any call runs (e.g. the extension header)."""
    base_url, _, model = llm_settings()
    return describe_model(base_url, model)

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
