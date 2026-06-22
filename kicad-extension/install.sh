#!/usr/bin/env bash
# Install the traces KiCad plugin into your KiCad scripting/plugins directory.
# The plugin talks to the local traces server (http://127.0.0.1:8000), so make
# sure `uv run traces-serve` is running in ../mcp before you use it.
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Find the highest-versioned KiCad plugins dir across the known locations.
PLUGIN_DIR=""
BEST=0
for base in "$HOME/Documents/KiCad" "$HOME/.local/share/kicad"; do
    [ -d "$base" ] || continue
    for entry in "$base"/*; do
        name="$(basename "$entry")"
        if [[ "$name" =~ ^([0-9]+)\.([0-9]+)$ ]] && [ -d "$entry" ]; then
            score=$(( ${BASH_REMATCH[1]} * 100 + ${BASH_REMATCH[2]} ))
            if [ "$score" -gt "$BEST" ]; then
                BEST=$score
                PLUGIN_DIR="$entry/scripting/plugins"
            fi
        fi
    done
done

if [ -z "$PLUGIN_DIR" ]; then
    echo "KiCad not found under ~/Documents/KiCad or ~/.local/share/kicad."
    echo "Install KiCad first, or pass a target dir: ./install.sh /path/to/scripting/plugins"
    [ -n "$1" ] && PLUGIN_DIR="$1" || exit 1
fi
[ -n "$1" ] && PLUGIN_DIR="$1"

mkdir -p "$PLUGIN_DIR"
cp "$SCRIPT_DIR/traces.py" "$SCRIPT_DIR/traces.png" "$PLUGIN_DIR"
rm -rf "$PLUGIN_DIR/__pycache__"

echo "Installed traces KiCad plugin to $PLUGIN_DIR"
echo
echo "Next steps:"
echo "  1. Start the server:  (cd ../mcp && uv run traces-serve)"
echo "  2. Restart KiCad so it loads the plugin."
echo "  3. PCB Editor -> Tools -> External Plugins -> traces."
