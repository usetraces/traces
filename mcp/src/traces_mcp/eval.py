"""Smoke eval for the traces sourcing + SRC pipeline.

Runs against whatever provider is configured (OpenRouter if OPENROUTER_API_KEY
is set, else local Ollama). Two parts:

  1. Sourcing — natural-language part requests per supplier. Requires the model
     to support tool-calling (verified locally with gemma4; qwen2.5/llama3.1 too).
  2. SRC — the JSON-only netlist checks. Works with any chat model.

Usage:
    uv run traces-eval                 # all checks
    uv run traces-eval --suppliers jlcpcb   # sourcing on one supplier
    uv run traces-eval --src-only      # netlist checks only (no tools needed)
"""

import argparse
import asyncio
import time

from . import netlist
from .config import OLLAMA_MODEL, llm_provider, llm_settings
from .semantic import DEFAULT_SUPPLIERS
from .suppliers import sourcing

# (description, footprint) requests that should resolve to a real in-stock part.
SOURCING_CASES = [
    ("100nF capacitor X7R", "0402"),
    ("10k ohm resistor 1%", "0603"),
    ("low Rds N-channel MOSFET", "SOT-23"),
]

# A tiny netlist with a deliberate orphan net (single node) for the SRC check.
SAMPLE_NETLIST = """<export version="E">
  <nets>
    <net code="1" name="+3V3"><node ref="U1" pin="1"/><node ref="C1" pin="1"/></net>
    <net code="2" name="GND"><node ref="U1" pin="2"/><node ref="C1" pin="2"/></net>
    <net code="3" name="SWIDO"><node ref="U1" pin="7"/><node ref="J1" pin="3"/></net>
    <net code="4" name="VBUS_SENSE"><node ref="U1" pin="14"/></net>
  </nets>
</export>"""


async def run_sourcing(suppliers: list[str]) -> bool:
    ok = True
    search = {"jlcpcb": sourcing.jlcpcb_search, "digikey": sourcing.digikey_search,
              "mouser": sourcing.mouser_search}
    for supplier in suppliers:
        for desc, fp in SOURCING_CASES:
            t0 = time.monotonic()
            try:
                res = await search[supplier](desc, fp, None, None, 1)
                cands = res.get("candidates", [])
                dt = time.monotonic() - t0
                if cands:
                    c = cands[0]
                    print(f"  PASS  {supplier:8} {desc!r:38} -> {c['part_number'] or '(no PN)'} "
                          f"qty={c['qty']} ${c['price']} ({dt:.1f}s)")
                else:
                    ok = False
                    print(f"  EMPTY {supplier:8} {desc!r:38} -> no candidates ({dt:.1f}s)")
            except Exception as exc:
                ok = False
                print(f"  FAIL  {supplier:8} {desc!r:38} -> {exc}")
    return ok


def run_src() -> bool:
    ok = True
    for name, fn in netlist.CHECKS.items():
        t0 = time.monotonic()
        try:
            issues = fn(SAMPLE_NETLIST).get("issues", [])
            dt = time.monotonic() - t0
            summary = "; ".join(f"{i['severity']}:{i['location']}" for i in issues) or "clean"
            print(f"  OK    netlist/{name:13} -> {len(issues)} issue(s) [{summary}] ({dt:.1f}s)")
        except Exception as exc:
            ok = False
            print(f"  FAIL  netlist/{name:13} -> {exc}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(prog="traces-eval")
    ap.add_argument("--suppliers", nargs="*", default=list(DEFAULT_SUPPLIERS))
    ap.add_argument("--src-only", action="store_true")
    ap.add_argument("--sourcing-only", action="store_true")
    args = ap.parse_args()

    base_url, _, model = llm_settings()
    print(f"provider: {llm_provider()}  model: {model}  base: {base_url}\n")

    results = []
    if not args.src_only:
        print("Sourcing (needs tool-calling):")
        results.append(asyncio.run(run_sourcing(args.suppliers)))
        print()
    if not args.sourcing_only:
        print("SRC / netlist checks (JSON only):")
        results.append(run_src())
        print()

    passed = all(results)
    print("EVAL PASSED" if passed else "EVAL FAILED (see above)")
    if llm_provider() == "ollama" and not passed:
        print(f"note: local model {OLLAMA_MODEL!r} — sourcing needs a tool-capable model "
              "(gemma4, qwen2.5, llama3.1). Check `ollama list` and set OLLAMA_MODEL.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
