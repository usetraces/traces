#!/usr/bin/env bash
# Install the traces MCP and everything it needs to run:
#   - .env            (copied from .env.example if missing)
#   - uv              (auto-installed if missing)
#   - Python deps     (uv sync)
#   - Node + agents   (npm install)
#   - local model     (Ollama + the OLLAMA_MODEL pull, only when running fully
#                      local — i.e. OPENROUTER_API_KEY is blank in .env)
set -e
cd "$(dirname "$0")"

ENV_FILE="../.env"
EXAMPLE_ENV="../.env.example"

# --- .env -------------------------------------------------------------------
if [ ! -f "$ENV_FILE" ] && [ -f "$EXAMPLE_ENV" ]; then
    cp "$EXAMPLE_ENV" "$ENV_FILE"
    echo "Created .env from .env.example — fill in keys as needed."
fi

# Is there an OpenRouter key? (blank/absent => fully local via Ollama.)
OPENROUTER_KEY="$(grep -E '^OPENROUTER_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' "')"
# Pick up an OLLAMA_MODEL override from .env, else default to gemma4.
OLLAMA_MODEL="$(grep -E '^OLLAMA_MODEL=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' "')"
OLLAMA_MODEL="${OLLAMA_MODEL:-gemma4}"

# --- uv ---------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found — installing…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Make uv available for the rest of this script.
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { echo "uv install failed; install it manually: https://docs.astral.sh/uv/"; exit 1; }

# --- node -------------------------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
    echo "node not found. Install Node.js 20+ (the sourcing agents run on it):"
    echo "  macOS:  brew install node"
    echo "  other:  https://nodejs.org/  (or use nvm)"
    exit 1
fi

uv sync
npm install

# --- local model (only when running fully local) ----------------------------
if [ -z "$OPENROUTER_KEY" ]; then
    echo
    echo "No OPENROUTER_API_KEY set — setting up the local model ($OLLAMA_MODEL via Ollama)."
    if ! command -v ollama >/dev/null 2>&1; then
        case "$(uname -s)" in
            Darwin)
                if command -v brew >/dev/null 2>&1; then
                    brew install ollama || true
                else
                    echo "Install Ollama from https://ollama.com/download, then re-run ./install.sh"
                fi
                ;;
            Linux)
                curl -fsSL https://ollama.com/install.sh | sh
                ;;
            *)
                echo "Install Ollama from https://ollama.com/download, then re-run ./install.sh"
                ;;
        esac
    fi
    if command -v ollama >/dev/null 2>&1; then
        echo "Pulling tool-capable model: $OLLAMA_MODEL (this can take a while)…"
        ollama pull "$OLLAMA_MODEL" || echo "Could not pull $OLLAMA_MODEL — run 'ollama pull $OLLAMA_MODEL' manually."
    fi
fi

echo
echo "Installed."
if [ -n "$OPENROUTER_KEY" ]; then
    echo "Using OpenRouter (with local $OLLAMA_MODEL as automatic fallback)."
else
    echo "Running fully local with Ollama model: $OLLAMA_MODEL."
fi
echo "Add supplier keys to ../.env as needed (MOUSER_API_KEY / DIGIKEY_*)."
echo
echo "Run as a local HTTP server (for the KiCad extension):"
echo "  uv run traces-serve"
echo
echo "Or add the stdio MCP to your agent (e.g. Claude Code):"
echo "  claude mcp add traces -- uv run --directory \"$(pwd)\" traces-mcp"
