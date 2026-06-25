"""Multi-model sourcing benchmark for traces.

Runs the JLCPCB sourcing agent against the local model + a set of OpenRouter
models, N trials each over a small fixed case set, and prints a simple terminal
report ranking the models by:

  1. accuracy  (did it return a correct in-stock part for the request)
  2. speed     (median LLM tool-loop latency)
  3. price     (avg $ per run, from token usage * per-model pricing)

JLCPCB/LCSC needs no supplier key, so the only cost is the LLM calls. The local
model is free ($0). Edit MODELS / CASES / TRIALS below, or override on the CLI.

Usage:
    uv run traces-bench                       # local + 5 OpenRouter models, 3 trials
    uv run traces-bench --trials 1            # quick pass
    uv run traces-bench --models ollama/gemma4 google/gemini-3.5-flash
    uv run traces-bench --no-local            # OpenRouter models only
"""

import argparse
import asyncio
import json
import os
import statistics
import time
from pathlib import Path

from .config import OLLAMA_BASE_URL, OPENROUTER_API_KEY

_AGENTS_DIR = Path(__file__).resolve().parent / "agents"
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# --- Models under test ------------------------------------------------------
# Prefix "ollama/" routes to the local Ollama server; everything else goes to
# OpenRouter. `price` is ($/1M prompt, $/1M completion) — pulled live from the
# OpenRouter /models API. Local is free. Edit freely.
MODELS = [
    {"id": "ollama/gemma4",                      "price": (0.00, 0.00)},
    {"id": "google/gemini-3.5-flash",            "price": (1.50, 9.00)},
    {"id": "openai/gpt-5-mini",                  "price": (0.25, 2.00)},
    {"id": "anthropic/claude-haiku-4.5",         "price": (1.00, 5.00)},
    {"id": "google/gemini-3.1-flash-lite",       "price": (0.25, 1.50)},  # what the extension ships
    {"id": "qwen/qwen3-235b-a22b-2507",          "price": (0.09, 0.10)},  # one Chinese model
]

# --- Cases (JLCPCB search) --------------------------------------------------
# `footprint` must match the returned candidate's footprint. `must` is a list of
# requirement groups; a group passes if ANY of its alternatives appears across
# the winning part's name + specs (normalised). Alternatives cover equivalent
# spellings so a correct part isn't marked wrong over formatting — e.g. a model
# that returns "0.1µF" instead of "100nF" still counts.
CASES = [
    {"desc": "100nF capacitor X7R", "footprint": "0402",
     "must": [["100n", "0.1u", "0.1µ", ".1u", ".1µ"], ["x7r"]]},
    {"desc": "10k ohm resistor 1%", "footprint": "0603",
     "must": [["10k", "10000", "10 k", "10kohm", "10kω"]]},
    {"desc": "low Rds N-channel MOSFET", "footprint": "SOT-23",
     "must": [["mosfet", "n-ch", "nch", "n-channel", "nmos"]]},
]

TRIALS = 3


def _norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _provider(model_id: str) -> tuple[str, str, str]:
    """(base_url, api_key, model_name) for a bench model id."""
    if model_id.startswith("ollama/"):
        return OLLAMA_BASE_URL, os.environ.get("OLLAMA_API_KEY", "ollama"), model_id.split("/", 1)[1]
    return _OPENROUTER_BASE, OPENROUTER_API_KEY, model_id


async def _run_case(model_id: str, case: dict) -> dict:
    """Run one JLCPCB search and return {ok, score, elapsed_ms, cost, error}."""
    base_url, api_key, model_name = _provider(model_id)
    args = ["search", case["desc"], case["footprint"], "", "", "1"]
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "node", str(_AGENTS_DIR / "jlcpcb_agent.mjs"), *args,
        cwd=str(_AGENTS_DIR),
        env={**os.environ, "LLM_BASE_URL": base_url, "LLM_API_KEY": api_key, "LLM_MODEL": model_name},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    wall_ms = (time.monotonic() - t0) * 1000

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()[-160:]
        return {"ok": False, "score": 0.0, "elapsed_ms": wall_ms, "cost": 0.0, "error": err or f"exit {proc.returncode}"}

    payload = _last_json(stdout.decode(errors="replace"))
    if not payload:
        return {"ok": False, "score": 0.0, "elapsed_ms": wall_ms, "cost": 0.0, "error": "no JSON output"}

    result = payload.get("result") or {}
    cands = result.get("candidates") or []
    usage = payload.get("usage") or {}
    elapsed = payload.get("elapsed_ms", wall_ms)

    # --- accuracy: 3 equal checks (returned / footprint / keyword groups) ---
    c = cands[0] if cands else None
    if isinstance(c, str):           # some models emit a bare part-number string
        c = {"part_number": c}
    score, returned = 0.0, bool(isinstance(c, dict) and c.get("part_number"))
    if returned:
        score += 1 / 3
        if _norm(case["footprint"]) in _norm(c.get("footprint", "")):
            score += 1 / 3
        hay = _norm(c.get("name", "") + " " + " ".join(c.get("specs", []) or []))
        if all(any(_norm(alt) in hay for alt in group) for group in case["must"]):
            score += 1 / 3

    price = next((m["price"] for m in MODELS if m["id"] == model_id), (0.0, 0.0))
    cost = (usage.get("prompt_tokens", 0) / 1e6) * price[0] + (usage.get("completion_tokens", 0) / 1e6) * price[1]
    return {"ok": returned, "score": score, "elapsed_ms": elapsed, "cost": cost, "error": ""}


def _last_json(text: str):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


async def _bench_model(model_id: str, cases: list[dict], trials: int) -> dict:
    runs = []
    for trial in range(trials):
        for case in cases:
            r = await _run_case(model_id, case)
            runs.append(r)
            tag = f"{r['score']*100:3.0f}%" if r["error"] == "" else "FAIL"
            note = f"  ({r['error']})" if r["error"] else ""
            print(f"    [{model_id:34}] t{trial+1} {case['desc'][:26]:26} {tag} "
                  f"{r['elapsed_ms']/1000:5.1f}s  ${r['cost']:.5f}{note}")
    accs = [r["score"] for r in runs]
    speeds = [r["elapsed_ms"] for r in runs if r["error"] == ""]
    costs = [r["cost"] for r in runs if r["error"] == ""]
    fails = sum(1 for r in runs if r["error"] != "")
    return {
        "model": model_id,
        "accuracy": statistics.mean(accs) if accs else 0.0,
        "speed_ms": statistics.median(speeds) if speeds else float("inf"),
        "cost": statistics.mean(costs) if costs else 0.0,
        "runs": len(runs),
        "fails": fails,
    }


def _table(rows: list[dict]):
    print("\n" + "=" * 78)
    print("SUMMARY (per model)")
    print("-" * 78)
    print(f"{'model':36} {'acc':>6} {'med speed':>11} {'avg $/run':>11} {'fails':>6}")
    for r in rows:
        sp = "—" if r["speed_ms"] == float("inf") else f"{r['speed_ms']/1000:.1f}s"
        print(f"{r['model']:36} {r['accuracy']*100:5.0f}% {sp:>11} "
              f"${r['cost']:>10.5f} {r['fails']:>4}/{r['runs']}")


def _ranking(title: str, rows: list[dict], key, fmt, reverse=False):
    print(f"\n{title}")
    ranked = sorted(rows, key=key, reverse=reverse)
    for i, r in enumerate(ranked, 1):
        print(f"  {i}. {r['model']:36} {fmt(r)}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="traces-bench")
    ap.add_argument("--trials", type=int, default=TRIALS, help=f"trials per case (default {TRIALS})")
    ap.add_argument("--models", nargs="*", help="override the model list (ids)")
    ap.add_argument("--no-local", action="store_true", help="skip the local ollama model")
    args = ap.parse_args()

    model_ids = args.models or [m["id"] for m in MODELS]
    if args.no_local:
        model_ids = [m for m in model_ids if not m.startswith("ollama/")]
    if any(not m.startswith("ollama/") for m in model_ids) and not OPENROUTER_API_KEY:
        print("OPENROUTER_API_KEY is not set in .env — only local (ollama/*) models can run.")
        model_ids = [m for m in model_ids if m.startswith("ollama/")]
        if not model_ids:
            return 1

    print(f"traces-bench  |  {len(model_ids)} models  x  {len(CASES)} cases  x  {args.trials} trials  "
          f"=  {len(model_ids)*len(CASES)*args.trials} runs  (JLCPCB, no supplier key)\n")

    rows = []
    for mid in model_ids:
        print(f"  {mid}")
        rows.append(asyncio.run(_bench_model(mid, CASES, args.trials)))

    _table(rows)
    _ranking("RANK BY ACCURACY (high → low)", rows, lambda r: (r["accuracy"], -r["speed_ms"]),
             lambda r: f"{r['accuracy']*100:.0f}%", reverse=True)
    _ranking("RANK BY SPEED (fast → slow)", rows, lambda r: r["speed_ms"],
             lambda r: "—" if r["speed_ms"] == float("inf") else f"{r['speed_ms']/1000:.1f}s median")
    _ranking("RANK BY PRICE (cheap → dear)", rows, lambda r: r["cost"],
             lambda r: f"${r['cost']:.5f}/run")
    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
