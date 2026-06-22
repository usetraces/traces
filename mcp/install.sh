#!/usr/bin/env bash
# Install the traces MCP: Python deps (uv) + Node sourcing agents (npm).
set -e
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
if ! command -v node >/dev/null 2>&1; then
    echo "node not found. Install Node.js 20+ (the sourcing agents run on it)."
    exit 1
fi

uv sync
npm install

echo
echo "Installed. Make sure ../.env has OPENROUTER_API_KEY (+ MOUSER_API_KEY / DIGIKEY_* as needed)."
echo
echo "Run as a local HTTP server (for the KiCad extension):"
echo "  uv run traces-serve"
echo
echo "Or add the stdio MCP to your agent (e.g. Claude Code):"
echo "  claude mcp add traces -- uv run --directory \"$(pwd)\" traces-mcp"
