#!/usr/bin/env bash
# One-shot installer for everything in traces:
#   1. the MCP / local sourcing server  (mcp/install.sh — uv, deps, local model)
#   2. the KiCad plugin                 (kicad-extension/install.sh)
#   3. the /src semantic-rule-check skill (copied into your agent)
#
# Usage (from a fresh clone):
#   ./install.sh                 # install all three
#   ./install.sh mcp             # just the server
#   ./install.sh kicad           # just the KiCad plugin
#   ./install.sh skill           # just the /src skill
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

WHAT="${1:-all}"

do_mcp() {
    echo "=== 1/3  MCP server ==="
    ./mcp/install.sh
}

do_kicad() {
    echo
    echo "=== 2/3  KiCad plugin ==="
    if [ -x ./kicad-extension/install.sh ]; then
        ./kicad-extension/install.sh || echo "KiCad plugin step skipped (KiCad not found — re-run ./install.sh kicad later)."
    fi
}

do_skill() {
    echo
    echo "=== 3/3  /src skill ==="
    local installed=0
    for dir in "$HOME/.claude/skills/src" "$HOME/.agents/skills/src"; do
        parent="$(dirname "$dir")"
        if [ -d "$parent" ] || [ "$parent" = "$HOME/.claude/skills" ]; then
            mkdir -p "$dir"
            cp "$ROOT/src/SKILL.md" "$dir/"
            echo "Installed /src skill to $dir"
            installed=1
        fi
    done
    [ "$installed" = 0 ] && echo "No agent skills dir found; copy src/SKILL.md into your agent's skills dir manually."
}

case "$WHAT" in
    all)   do_mcp; do_kicad; do_skill ;;
    mcp)   do_mcp ;;
    kicad) do_kicad ;;
    skill) do_skill ;;
    *) echo "Unknown target '$WHAT' (use: all | mcp | kicad | skill)"; exit 1 ;;
esac

echo
echo "Done. Start the server with:  (cd \"$ROOT/mcp\" && uv run traces-serve)"
