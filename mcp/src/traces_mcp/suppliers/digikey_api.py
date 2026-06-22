"""Thin Digi-Key Product Information v4 client (OAuth client_credentials)."""

import logging
import time
from urllib.parse import quote

import requests

from ..config import DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET

logger = logging.getLogger(__name__)

API_BASE = "https://api.digikey.com"
TOKEN_URL = f"{API_BASE}/v1/oauth2/token"

_access_token: str | None = None
_expires_at: float = 0.0


def _get_token() -> str:
    global _access_token, _expires_at
    if not DIGIKEY_CLIENT_ID or not DIGIKEY_CLIENT_SECRET:
        raise ValueError("DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET must be set in .env")
    if _access_token and time.time() < _expires_at:
        return _access_token
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": DIGIKEY_CLIENT_ID,
            "client_secret": DIGIKEY_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Digi-Key OAuth error: {resp.status_code} - {resp.text}")
    payload = resp.json()
    _access_token = payload["access_token"]
    _expires_at = time.time() + int(payload.get("expires_in", 1800)) - 60
    return _access_token


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "X-DIGIKEY-Client-Id": DIGIKEY_CLIENT_ID,
        "Content-Type": "application/json",
        "X-DIGIKEY-Locale-Site": "US",
        "X-DIGIKEY-Locale-Language": "en",
        "X-DIGIKEY-Locale-Currency": "USD",
    }


def _request(method: str, url: str, data: dict | None = None) -> dict:
    global _access_token, _expires_at
    for attempt in range(2):
        if method == "GET":
            resp = requests.get(url, headers=_headers(), timeout=30)
        else:
            resp = requests.post(url, headers=_headers(), json=data, timeout=30)
        if resp.status_code == 401 and attempt == 0:
            _access_token = None
            _expires_at = 0.0
            continue
        break
    if resp.status_code != 200:
        raise RuntimeError(f"Digi-Key API error: {resp.status_code} - {resp.text}")
    return resp.json()


def keyword_search(keywords: str, limit: int = 5) -> dict:
    return _request("POST", f"{API_BASE}/products/v4/search/keyword", {"Keywords": keywords, "Limit": limit})


def product_details(product_number: str) -> dict:
    return _request("GET", f"{API_BASE}/products/v4/search/{quote(product_number)}/productdetails")


def product_media(product_number: str) -> dict:
    return _request("GET", f"{API_BASE}/products/v4/search/{quote(product_number)}/media")


def datasheet_url(product_number: str) -> str:
    try:
        details = product_details(product_number)
        product = details.get("Product", details)
        url = str(product.get("DatasheetUrl") or "")
        if url:
            return url
    except Exception:
        pass
    try:
        for media in product_media(product_number).get("MediaLinks", []):
            if str(media.get("MediaType", "")).lower() == "datasheets":
                return str(media.get("Url") or "")
    except Exception:
        pass
    return ""
