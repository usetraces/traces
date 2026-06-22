"""Fetch KiCad symbols/footprints for LCSC components via easyeda2kicad."""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

_LCSC_RE = re.compile(r"^C\d+$", re.IGNORECASE)
_JLCPCB_SEARCH = (
    "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/"
    "selectSmtComponentList/v2"
)


def search_lcsc(mpn: str) -> dict:
    """Search JLCPCB for an MPN and return the first matching LCSC C-number."""
    body = json.dumps({"currentPage": 1, "pageSize": 5, "keyword": mpn.strip(), "searchType": 1}).encode()
    req = Request(
        _JLCPCB_SEARCH,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    components = data.get("data", {}).get("componentPageInfo", {}).get("list", [])
    if not components:
        return {"lcsc_id": None}
    lcsc_id = str(components[0].get("componentCode") or "")
    return {"lcsc_id": lcsc_id if _LCSC_RE.match(lcsc_id) else None}


def get_symbol(lcsc_id: str) -> dict:
    """Fetch the KiCad symbol + footprint for an LCSC component."""
    lcsc_id = lcsc_id.strip().upper()
    if not _LCSC_RE.match(lcsc_id):
        raise ValueError("Invalid LCSC ID — expected format: C12345")

    with tempfile.TemporaryDirectory() as tmp:
        out_sym = str(Path(tmp) / "traces.kicad_sym")
        result = subprocess.run(
            [sys.executable, "-m", "easyeda2kicad", "--full", "--lcsc_id", lcsc_id,
             "--output", out_sym, "--overwrite"],
            capture_output=True, text=True, timeout=30,
        )
        sym_path = Path(out_sym)
        if not sym_path.exists():
            detail = result.stderr.strip()[:300] or result.stdout.strip()[:300]
            raise FileNotFoundError(f"No symbol found for {lcsc_id}. {detail}")

        symbol_content = sym_path.read_text(encoding="utf-8")
        m = re.search(r'^\s*\(symbol\s+"([^"]+)"', symbol_content, re.MULTILINE)
        symbol_name = m.group(1) if m else lcsc_id

        fp_dir = Path(tmp) / "traces.pretty"
        footprint_name = ""
        footprint_content = ""
        if fp_dir.exists():
            mods = list(fp_dir.glob("*.kicad_mod"))
            if mods:
                footprint_name = mods[0].stem
                footprint_content = mods[0].read_text(encoding="utf-8")

        return {
            "lcsc_id": lcsc_id,
            "symbol_name": symbol_name,
            "footprint_name": footprint_name,
            "lib_name": "traces",
            "symbol_content": symbol_content,
            "footprint_content": footprint_content,
        }
