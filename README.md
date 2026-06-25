# traces — supercharged ee tools

Open-source tooling for KiCad: semantic component sourcing and schematic review
that runs entirely on **your** machine against **your** API keys. No accounts, no
hosted backend, no billing.

There are three tools:

1. **mcp** — a local sourcing server you install into your agent
2. **kicad extension** — a KiCad plugin that talks to the local server
3. **semantic rule check (SRC)** — a `/src` skill for Claude Code / Codex

## Setup

One command installs all three tools (server, KiCad plugin, `/src` skill):

```bash
git clone https://github.com/usetraces/traces.git
cd traces
./install.sh           # creates .env, installs deps; pulls a local model if no cloud key
```

`./install.sh` creates `.env` for you. Add keys to it as needed, then re-run if
you like — sourcing works against the local model with **no keys at all**. You
can also install just one piece: `./install.sh mcp | kicad | skill`.

All three tools read keys from the repo-root `.env`:

| Key | Needed for |
|---|---|
| `OPENROUTER_API_KEY` | cloud LLM for sourcing (optional — blank = fully local) |
| `MOUSER_API_KEY` | Mouser sourcing |
| `DIGIKEY_CLIENT_ID` / `DIGIKEY_CLIENT_SECRET` | Digi-Key sourcing |
| _(none)_ | JLCPCB / LCSC sourcing works with no key |

Prerequisites: `./install.sh` auto-installs [uv](https://docs.astral.sh/uv/) and
(for local mode) Ollama. You need Node.js 20+ and KiCad already present.

---

## 1. mcp

A local server that turns natural language ("low-Rds N-MOSFET in SOT-23",
"100nF 0402 X7R") into ranked, in-stock parts across JLCPCB/LCSC, Digi-Key, and
Mouser. The OpenRouter-backed Node agents interpret each request like an
engineer, query the supplier APIs, and the results are merged and ranked
in-stock-first, then by footprint match and price.

```bash
cd mcp
./install.sh        # uv sync + npm install
```

**Run it as a local HTTP server** (what the KiCad extension connects to):

```bash
uv run traces-serve   # serves http://127.0.0.1:8000
```

**Or install the stdio MCP into your agent.** For Claude Code:

```bash
claude mcp add traces -- uv run --directory /path/to/traces/mcp traces-mcp
```

Tools exposed to the agent:

- `source_component(query, footprint?, suppliers?, max_price?, min_qty?, count?)` — ranked in-stock candidates
- `find_datasheet(lcsc)` — datasheet PDF URL for an LCSC C-number
- `netlist_check(xml, check)` — run a semantic rule check over netlist XML

Override the model with `DEFAULT_MODEL` in `.env` (default
`google/gemini-3.1-flash-lite`).

### Running fully local (no OpenRouter key)

If `OPENROUTER_API_KEY` is blank — **or set but broken/unreachable** — traces
automatically falls back to a local [Ollama](https://ollama.com) model. No key,
nothing leaves your machine. The model is `OLLAMA_MODEL` (default `gemma4`).

`./install.sh` sets this up for you: when there's no OpenRouter key it installs
Ollama and pulls `OLLAMA_MODEL`. To do it by hand, or to use a different
tool-capable model (sourcing uses function-calling):

```bash
ollama pull gemma4        # verified; qwen2.5 / llama3.1 also work
# leave OPENROUTER_API_KEY blank in .env (OLLAMA_MODEL=gemma4 is the default)
uv run traces-serve
```

The SRC checks (JSON only) work with any chat model; sourcing needs tool
support. Local runs are slower (tens of seconds per sourcing call) but produce
the same ranked, in-stock results.

### Eval

Smoke-test the pipeline against whichever provider is active:

```bash
uv run traces-eval            # sourcing (all suppliers) + SRC checks
uv run traces-eval --src-only # netlist checks only (no tool-calling needed)
```

Verified end-to-end against local `gemma4` via Ollama — both the SRC checks and
JLCPCB/Digi-Key/Mouser sourcing pass with no OpenRouter key.

---

## 2. kicad extension

A KiCad 10 PCB-editor plugin that reconciles schematic metadata, sources parts
into schematic properties, fetches datasheets, pulls KiCad symbols/footprints
from LCSC, and runs SRC — all against the local server above.

```bash
cd kicad-extension
./install.sh        # copies the plugin into your KiCad plugins dir
```

Then:

1. Start the server: `(cd ../mcp && uv run traces-serve)`
2. Restart KiCad so it loads the plugin.
3. Open a saved PCB → **Tools → External Plugins → traces**.

The plugin points at `http://127.0.0.1:8000`; it does not require sign-in.

---

## 3. semantic rule check (SRC)

A skill for Claude Code / Codex that reviews a local KiCad project for
**semantic** net problems ERC can't catch — net-label typos, inconsistent bus
naming, orphan (single-pin) nets, and near-duplicate net names. The agent does
the reasoning directly, so no server or key is needed.

Install the skill (Claude Code example):

```bash
mkdir -p ~/.claude/skills/src
cp src/SKILL.md ~/.claude/skills/src/
```

Then, from a directory containing a KiCad project:

```
/src
```

See [`src/SKILL.md`](src/SKILL.md) for what each check flags.

---

## Architecture

```
traces/
├── mcp/              # local sourcing server (FastMCP + FastAPI + Node agents)
│   └── src/traces_mcp/
│       ├── mcp_server.py   # stdio MCP  (traces-mcp)
│       ├── app.py          # local HTTP (traces-serve) for the KiCad extension
│       ├── semantic.py     # cross-supplier ranking
│       ├── suppliers/      # JLCPCB / Digi-Key / Mouser clients + sourcing
│       ├── netlist.py      # SRC checks (OpenRouter or local Ollama)
│       ├── library.py      # LCSC symbol/footprint via easyeda2kicad
│       └── agents/*.mjs    # sourcing agents (Node, OpenAI-compatible)
├── kicad-extension/  # KiCad plugin (traces.py)
└── src/              # the /src SRC skill (SKILL.md)
```

The KiCad extension speaks the same `/jobs/*` submit-and-poll contract the old
hosted backend used, so the local server is a drop-in: requests are computed
inline with an in-memory job store — no Redis, no auth, no billing.

## What changed from the hosted version

This repo is the open-source extraction of the original hosted product. Removed:
Supabase auth, Stripe billing, the marketing site / dashboard / stats pages, the
Redis + ARQ job queue, and all account/sign-in flows. The sourcing engine, the
KiCad plugin, and the SRC checks are preserved and rewired to run locally.

## License

MIT — see [LICENSE](LICENSE).
