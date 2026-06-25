"""Run the OpenRouter sourcing agents (Node.js) and parse their JSON output.

Each agent (`agents/*.mjs`) hits a supplier API directly and uses OpenRouter to
pick the best in-stock part, emitting one JSON line: {result, usage, elapsed_ms,
model}. We only care about `result`.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from .config import describe_model, llm_provider_chain

logger = logging.getLogger(__name__)

_AGENTS_DIR = Path(__file__).resolve().parent / "agents"


async def run_node_agent(script: str, args: list[str], label: str, model: str | None = None) -> dict:
    """Run a supplier sourcing agent, trying each provider in the chain in turn
    (OpenRouter first if a key is set, then the local Ollama fallback). The
    result dict is stamped with `_model` describing which provider answered."""
    script_path = _AGENTS_DIR / script
    providers = llm_provider_chain(model)
    last_err: Exception | None = None
    for i, (base_url, api_key, env_model) in enumerate(providers):
        try:
            result = await _run_once(script_path, args, label, base_url, api_key, env_model)
            if isinstance(result, dict):
                result["_model"] = describe_model(base_url, env_model)
            return result
        except Exception as exc:  # noqa: BLE001 — try the next provider in the chain
            last_err = exc
            if i + 1 < len(providers):
                logger.warning("[%s] provider %s failed (%s); falling back to local model", label, base_url, exc)
    raise RuntimeError(f"{label} agent failed on all providers: {last_err}")


async def _run_once(script_path: Path, args: list[str], label: str, base_url: str, api_key: str, env_model: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "node",
        str(script_path),
        *args,
        cwd=str(_AGENTS_DIR),
        env={
            **os.environ,
            "LLM_BASE_URL": base_url,
            "LLM_API_KEY": api_key,
            "LLM_MODEL": env_model,
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    err = stderr.decode(errors="replace").strip()
    if err:
        logger.info("[%s] node stderr: %s", label, err[-500:])

    if proc.returncode != 0:
        raise RuntimeError(f"{label} agent exited {proc.returncode}: {err[-300:] or 'no stderr'}")

    payload = _last_json_line(stdout.decode(errors="replace"))
    if payload is None:
        raise RuntimeError(f"{label} agent produced no JSON output")

    result = payload.get("result")
    if result is None:
        raise RuntimeError(f"{label} agent did not return a structured result")
    return result


def search_args(description: str, footprint: str, max_price: float | None, min_qty: int | None, count: int) -> list[str]:
    """Positional args for an agent's `search` mode."""
    return [
        description,
        footprint,
        "" if max_price is None else str(max_price),
        "" if min_qty is None else str(min_qty),
        str(count),
    ]


def _last_json_line(text: str):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None
