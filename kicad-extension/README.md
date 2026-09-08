# traces for KiCad

Component sourcing, schematic properties, datasheets, library imports, and
semantic netlist checks, launched from the KiCad PCB Editor.

The first Plugin and Content Manager (PCM) package targets **KiCad 10 on macOS**
and is a testing release. The local server is installed separately.

## Install the local server

Install Node.js 20+ and Git, then run:

```sh
git clone https://github.com/usetraces/traces.git
cd traces
git checkout kicad-v2.0.0
./install.sh mcp
```

The installer sets up uv and Python dependencies (Python 3.11+). Configure the
clone's `.env` with your API keys. With no OpenRouter key, the installer attempts
to install Ollama and download the default model. Model downloads can be large.
See the repository README for provider settings and manual Ollama setup.

Run the server in a terminal and leave it running:

```sh
cd mcp
uv run traces-serve
```

The plugin connects to `http://127.0.0.1:8000`. PCM installs only the plugin;
it does not run the backend installer. Automatic server discovery is a convenience
for existing checkouts, not required when the server is already running.

## Install the plugin

1. Download `traces-kicad-2.0.0.zip` from the GitHub release.
2. In KiCad's project manager, open **Plugin and Content Manager**, select
   **Install from File**, choose the ZIP, and apply pending changes if prompted.
3. Restart the PCB Editor. Open a saved PCB belonging to your project.
4. Choose **Tools → External Plugins → traces** (or its toolbar button).

Once accepted into the official repository, traces will also be installable by
name in PCM. Until then, use the release ZIP.

If you previously ran `./install.sh kicad`, move the old `traces.py` and
`traces.png` out of `~/Documents/KiCad/10.0/scripting/plugins/` before restarting
KiCad to avoid duplicate plugin registrations. Keep a backup outside that folder.
Do not run the manual plugin installer on top of a PCM installation.

## Use and updates

The plugin edits schematic files on disk. Save and close the schematic editor
before applying edits, then reopen it to load the changes. Start with a copy of
your project. A running server is required for sourcing and semantic checks.

Local Ollama inference keeps model processing local; component sourcing and
library downloads still contact external suppliers. OpenRouter mode sends model
requests, including the netlist when running semantic checks, to that service.
Digi-Key and Mouser features may require your own supplier credentials.

Install subsequent plugin releases through PCM. Update the separate server to
the corresponding release as described in its release notes. Report problems at
https://github.com/usetraces/traces/issues with your KiCad version and OS, without
including API keys or private design files.
