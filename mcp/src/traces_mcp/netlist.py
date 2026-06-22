"""Semantic netlist checks (the Semantic Rule Check / SRC engine).

These are LLM reviews of KiCad netlist XML: net-label typos, naming
consistency, orphan (single-pin) nets, and near-duplicate names. They run
through OpenRouter's OpenAI-compatible chat-completions endpoint in JSON mode,
so the only dependency is `requests`.
"""

import json

import requests

from .config import llm_settings

_SCHEMA_HINT = (
    'Respond with ONLY a JSON object of the form '
    '{"issues": [{"severity": "warning"|"error", "location": str, "message": str}]}. '
    "No prose, no code fences."
)


def _run_check(prompt: str, model: str | None = None) -> dict:
    base_url, api_key, resolved = llm_settings(model)
    resp = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": resolved,
            "messages": [{"role": "user", "content": f"{prompt}\n\n{_SCHEMA_HINT}"}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"LLM error: {resp.status_code} - {resp.text}")
    content = resp.json()["choices"][0]["message"]["content"]
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Tolerate a stray code fence.
        data = json.loads(content.strip().strip("`").lstrip("json").strip())
    issues = data.get("issues") or []
    return {"issues": [
        {
            "severity": str(i.get("severity") or "warning"),
            "location": str(i.get("location") or ""),
            "message": str(i.get("message") or ""),
        }
        for i in issues
    ]}


def netlist_typo(xml: str, model: str | None = None) -> dict:
    return _run_check(f"""You are a KiCad schematic reviewer. Analyze this netlist XML for net label typos.

Flag net names that are likely transpositions or misspellings of standard electronics signal names.
- Character transpositions: TDX→TXD, SWIDO→SWDIO, CAHN→CANH, MOSIO→MOSI
- Missing or extra characters that make a name resemble a known signal
- Do NOT flag power rail nets (GND, AGND, DGND, +3V3, +5V, VBUS, VCC, VDD, VBAT, etc.)
- Do NOT flag intentional abbreviations (CLK, RST, EN, CS, etc.)
- severity "error" for obvious transpositions, "warning" for uncertain cases
- location must be the exact net name from the XML

Return an empty issues list if no typos are found.

<netlist>
{xml}
</netlist>""", model)


def netlist_consistency(xml: str, model: str | None = None) -> dict:
    return _run_check(f"""You are a KiCad schematic reviewer. Analyze this netlist XML for net naming consistency issues.

Flag groups of nets in the same signal family that use inconsistent naming conventions.
- Mixed separators: CAN_H and CANL
- Mixed prefix styles: SPI_MOSI and SPIMISO
- Mixed casing: usbDP and USB_DM
- Mixed suffix styles: I2C_SCL and I2CSDA

Rules:
- Only flag inconsistencies within clearly related signal groups
- Complementary pairs like CLK/CLK_N or DATA/DATA_B are intentional — do not flag
- location should list the inconsistent net names comma-separated
- severity "warning"

Return an empty issues list if naming is consistent.

<netlist>
{xml}
</netlist>""", model)


def netlist_orphan(xml: str, model: str | None = None) -> dict:
    return _run_check(f"""You are a KiCad schematic reviewer. Analyze this netlist XML for orphan nets.

An orphan net is a <net> element with exactly one <node> child — it connects to only one pin, which usually indicates a dangling wire or misnamed net.
- Count <node> elements under each <net>; flag any net with exactly one <node>
- Do NOT flag nets whose name contains "PWR_FLAG" or "PWRFLAG"
- Do NOT flag KiCad auto-named internal nets (names like "Net-(...")
- severity "error" for unambiguous dangling connections, "warning" for testpoints/stubs
- location should be the net name; message should include the component and pin

Return an empty issues list if no orphan nets are found.

<netlist>
{xml}
</netlist>""", model)


def netlist_nearduplicate(xml: str, model: str | None = None) -> dict:
    return _run_check(f"""You are a KiCad schematic reviewer. Analyze this netlist XML for near-duplicate net names.

Flag pairs of net names that are suspiciously similar (edit distance 1–2) and may be an accidental naming split.
- Single char difference: VIN_GOOD vs VIN_GOD
- Extra/missing suffix: +3V3 and +3V3A both present
- Swapped characters: RESET vs RESSET

Rules:
- Do NOT flag intentional complementary pairs: CLK/CLK_N, DATA/DATA_B, TX/RX, CANH/CANL
- Do NOT flag genuinely distinct rails (+3V3 and +5V)
- location should be "NET_A / NET_B"
- severity "warning"

Return an empty issues list if no suspicious near-duplicates are found.

<netlist>
{xml}
</netlist>""", model)


CHECKS = {
    "typo": netlist_typo,
    "consistency": netlist_consistency,
    "orphan": netlist_orphan,
    "nearduplicate": netlist_nearduplicate,
}
