"""Cross-supplier semantic sourcing.

Fan out a natural-language part request to every selected supplier in parallel,
normalize the candidates, and rank them (in-stock first, then footprint match,
then price). This is the engine behind the MCP `source_component` tool.
"""

import asyncio
from typing import Literal

from .suppliers import sourcing

Supplier = Literal["jlcpcb", "digikey", "mouser"]
DEFAULT_SUPPLIERS: tuple[Supplier, ...] = ("jlcpcb", "digikey", "mouser")


async def source_component(
    query: str,
    footprint: str = "",
    suppliers: list[str] | None = None,
    max_price: float | None = None,
    min_qty: int | None = None,
    count: int = 5,
    model: str | None = None,
) -> dict:
    query = query.strip()
    footprint = footprint.strip()
    if not query:
        raise ValueError("query must not be blank")
    count = max(1, min(int(count or 5), 5))
    selected = normalize_suppliers(suppliers)

    tasks = [
        asyncio.create_task(_run_supplier(s, query, footprint, max_price, min_qty, count, model))
        for s in selected
    ]
    results = await asyncio.gather(*tasks)

    candidates: list[dict] = []
    errors: list[dict] = []
    for supplier, payload, error in results:
        if error:
            errors.append({"supplier": supplier, "message": error})
            continue
        for candidate in (payload or {}).get("candidates", []):
            candidates.append({"supplier": supplier, **candidate})

    candidates.sort(key=lambda c: _rank_key(c, footprint))
    candidates = candidates[:count]

    if not candidates and errors:
        raise RuntimeError(
            "All selected suppliers failed: "
            + "; ".join(f"{e['supplier']}: {e['message']}" for e in errors)
        )
    return {"candidates": candidates, "errors": errors}


def normalize_suppliers(suppliers: list[str] | None) -> list[str]:
    if not suppliers:
        return list(DEFAULT_SUPPLIERS)
    normalized: list[str] = []
    for supplier in suppliers:
        value = str(supplier).strip().lower()
        if value not in DEFAULT_SUPPLIERS:
            raise ValueError(f"Unsupported supplier: {supplier}")
        if value not in normalized:
            normalized.append(value)
    return normalized or list(DEFAULT_SUPPLIERS)


async def _run_supplier(supplier, query, footprint, max_price, min_qty, count, model):
    try:
        fn = {
            "jlcpcb": sourcing.jlcpcb_search,
            "digikey": sourcing.digikey_search,
            "mouser": sourcing.mouser_search,
        }[supplier]
        return supplier, await fn(query, footprint, max_price, min_qty, count, model), None
    except Exception as exc:
        return supplier, None, str(exc)


def _rank_key(candidate: dict, requested_footprint: str) -> tuple:
    in_stock = 0 if int(candidate.get("qty") or 0) > 0 else 1
    footprint_match = 0
    if requested_footprint:
        haystack = " ".join([
            str(candidate.get("footprint") or ""),
            str(candidate.get("name") or ""),
            " ".join(candidate.get("specs") or []),
            str(candidate.get("justification") or ""),
        ]).lower()
        footprint_match = 0 if requested_footprint.lower() in haystack else 1
    price = candidate.get("price")
    price_key = float(price) if price is not None else float("inf")
    return (in_stock, footprint_match, price_key, -int(candidate.get("qty") or 0))
