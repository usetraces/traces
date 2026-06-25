"""Supplier sourcing: natural-language → ranked in-stock parts.

Each `*_search` / `*_source` spawns the matching OpenRouter Node agent
(`agents/<supplier>_agent.mjs`), which interprets the request like an engineer,
queries the supplier API, and returns structured candidates. Datasheet URLs are
backfilled directly from the supplier APIs when the agent leaves them blank.
"""

import json
from urllib.request import Request, urlopen

from ..node_agent import run_node_agent, search_args
from . import digikey_api, mouser_api

JLCPCB_SEARCH_API = (
    "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/"
    "selectSmtComponentList/v2"
)


def _candidate(c: dict, datasheet_fallback=None) -> dict:
    part_number = str(c.get("part_number") or "")
    url = str(c.get("datasheet_url", "") or "")
    if not url and part_number and datasheet_fallback:
        try:
            url = datasheet_fallback(part_number)
        except Exception:
            url = ""
    return {
        "part_number": part_number,
        "qty": int(c.get("qty") or 0),
        "price": None if c.get("price") is None else float(c["price"]),
        "datasheet_url": url,
        "justification": str(c.get("justification") or ""),
        "name": str(c.get("name") or ""),
        "manufacturer": str(c.get("manufacturer") or ""),
        "footprint": str(c.get("footprint") or ""),
        "specs": [str(s) for s in (c.get("specs") or [])],
    }


# --- JLCPCB / LCSC ----------------------------------------------------------

async def jlcpcb_search(description, footprint, max_price, min_qty, count=1, model=None):
    result = await run_node_agent(
        "jlcpcb_agent.mjs",
        ["search", *search_args(description, footprint, max_price, min_qty, count)],
        "jlcpcb_search",
        model,
    )
    return {"candidates": [_candidate(c) for c in result.get("candidates", [])], "model": result.get("_model")}


async def jlcpcb_source(identifier, footprint, id_type="MPN", model=None):
    result = await run_node_agent(
        "jlcpcb_agent.mjs", ["source", identifier, footprint, id_type], "jlcpcb_source", model
    )
    return {
        "part_number": str(result.get("part_number") or ""),
        "qty": int(result.get("qty") or 0),
        "price": None if result.get("price") is None else float(result["price"]),
        "datasheet_url": str(result.get("datasheet_url", "") or ""),
        "model": result.get("_model"),
    }


def jlcpcb_datasheet_direct(lcsc: str) -> dict:
    body = json.dumps({"currentPage": 1, "pageSize": 5, "keyword": lcsc, "searchType": 2}).encode()
    req = Request(
        JLCPCB_SEARCH_API,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 200:
        raise RuntimeError(f"JLCPCB API error: {data.get('message') or data}")
    for component in data.get("data", {}).get("componentPageInfo", {}).get("list", []):
        if str(component.get("componentCode") or "").upper() != lcsc.upper():
            continue
        url = str(component.get("dataManualUrl") or "") or f"https://www.lcsc.com/datasheet/{lcsc}.pdf"
        return {"url": url, "lcsc": lcsc}
    return {"url": "", "lcsc": lcsc}


async def datasheet_fetch(lcsc: str, model=None) -> dict:
    direct = jlcpcb_datasheet_direct(lcsc)
    if direct["url"]:
        return direct
    result = await run_node_agent("jlcpcb_agent.mjs", ["datasheet", lcsc], "datasheet_fetch", model)
    return {"url": str(result.get("url") or ""), "lcsc": str(result.get("lcsc") or lcsc), "model": result.get("_model")}


# --- Digi-Key ---------------------------------------------------------------

async def digikey_search(description, footprint, max_price, min_qty, count=1, model=None):
    result = await run_node_agent(
        "digikey_agent.mjs",
        ["search", *search_args(description, footprint, max_price, min_qty, count)],
        "digikey_search",
        model,
    )
    return {"candidates": [_candidate(c, digikey_api.datasheet_url) for c in result.get("candidates", [])], "model": result.get("_model")}


async def digikey_source(search_terms, footprint, model=None):
    result = await run_node_agent("digikey_agent.mjs", ["source", search_terms, footprint], "digikey_source", model)
    url = str(result.get("datasheet_url", "") or "")
    part_number = str(result.get("part_number") or "")
    if not url and part_number:
        try:
            url = digikey_api.datasheet_url(part_number)
        except Exception:
            url = ""
    return {
        "part_number": part_number,
        "qty": int(result.get("qty") or 0),
        "price": None if result.get("price") is None else float(result["price"]),
        "datasheet_url": url,
        "model": result.get("_model"),
    }


async def digikey_datasheet(product_number: str) -> dict:
    return {"part_number": product_number, "url": digikey_api.datasheet_url(product_number)}


# --- Mouser -----------------------------------------------------------------

async def mouser_search(description, footprint, max_price, min_qty, count=1, model=None):
    result = await run_node_agent(
        "mouser_agent.mjs",
        ["search", *search_args(description, footprint, max_price, min_qty, count)],
        "mouser_search",
        model,
    )
    return {"candidates": [_candidate(c, mouser_api.datasheet_url) for c in result.get("candidates", [])], "model": result.get("_model")}


async def mouser_source(search_terms, footprint, model=None):
    result = await run_node_agent("mouser_agent.mjs", ["source", search_terms, footprint], "mouser_source", model)
    url = str(result.get("datasheet_url", "") or "")
    part_number = str(result.get("part_number") or "")
    if not url and part_number:
        try:
            url = mouser_api.datasheet_url(part_number)
        except Exception:
            url = ""
    return {
        "part_number": part_number,
        "qty": int(result.get("qty") or 0),
        "price": None if result.get("price") is None else float(result["price"]),
        "datasheet_url": url,
        "model": result.get("_model"),
    }


async def mouser_datasheet(part_number: str) -> dict:
    return {"part_number": part_number, "url": mouser_api.datasheet_url(part_number)}
