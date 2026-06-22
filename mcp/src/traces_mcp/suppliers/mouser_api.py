"""Thin Mouser Search API client (keyword + part-number search)."""

import json
import logging
from urllib.parse import urlencode

import requests

from ..config import MOUSER_API_KEY

logger = logging.getLogger(__name__)

API_BASE = "https://api.mouser.com/api/v1"


def _post(path: str, body: dict) -> dict:
    if not MOUSER_API_KEY:
        raise ValueError("MOUSER_API_KEY must be set in .env")
    url = f"{API_BASE}/{path}?{urlencode({'apiKey': MOUSER_API_KEY})}"
    logger.info("POST %s", url.split("?")[0])
    resp = requests.post(url, json=body, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Mouser API error: {resp.status_code} - {resp.text}")
    data = resp.json()
    errors = data.get("Errors") or []
    if errors:
        raise RuntimeError(f"Mouser API errors: {json.dumps(errors)}")
    return data


def keyword_search(keyword: str, records: int = 5) -> dict:
    """Search Mouser products by keyword."""
    return _post("search/keyword", {
        "SearchByKeywordRequest": {"keyword": keyword, "records": records, "startingRecord": 0}
    })


def part_search(mouser_part_number: str) -> dict:
    """Search Mouser by Mouser or manufacturer part number."""
    return _post("search/partnumber", {"SearchByPartRequest": {"mouserPartNumber": mouser_part_number}})


def datasheet_url(mouser_part_number: str) -> str:
    if not mouser_part_number:
        return ""
    result = part_search(mouser_part_number)
    for part in result.get("SearchResults", {}).get("Parts", []):
        url = str(part.get("DataSheetUrl") or "")
        if url:
            return url
    return ""
