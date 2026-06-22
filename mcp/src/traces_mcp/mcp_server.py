"""The traces MCP server — install this into your agent (Claude Code, Codex…).

Exposes electronics-sourcing tools backed by JLCPCB/LCSC, Digi-Key, and Mouser,
with semantic interpretation via OpenRouter. No auth, no billing — it runs
locally against your own API keys in `.env`.
"""

from typing import Literal

from fastmcp import FastMCP

from . import netlist
from .semantic import source_component as _source_component
from .suppliers import sourcing

Supplier = Literal["jlcpcb", "digikey", "mouser"]


def create_mcp() -> FastMCP:
    mcp = FastMCP(
        "traces",
        instructions=(
            "Semantic electronics component sourcing across JLCPCB/LCSC, Digi-Key, "
            "and Mouser, plus KiCad netlist semantic rule checks (SRC)."
        ),
    )

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def source_component(
        query: str,
        footprint: str = "",
        suppliers: list[Supplier] | None = None,
        max_price: float | None = None,
        min_qty: int | None = None,
        count: int = 5,
    ) -> dict:
        """Find ranked, in-stock supplier candidates from a natural-language part request.

        `query` can be a value ("100nF 0805 X7R"), a generic part ("low-Rds N-MOSFET
        SOT-23"), or an exact MPN. Results are ranked in-stock-first, then by footprint
        match and price, merged across the selected suppliers.
        """
        return await _source_component(query, footprint, suppliers, max_price, min_qty, count)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def find_datasheet(lcsc: str) -> dict:
        """Return the datasheet PDF URL for an LCSC C-number (e.g. C2040)."""
        return await sourcing.datasheet_fetch(lcsc)

    @mcp.tool(annotations={"readOnlyHint": True})
    def netlist_check(
        xml: str,
        check: Literal["typo", "consistency", "orphan", "nearduplicate"],
    ) -> dict:
        """Run a semantic rule check over KiCad netlist XML.

        `typo` finds misspelled signal names, `consistency` finds mixed naming
        conventions, `orphan` finds single-pin nets, `nearduplicate` finds
        accidentally-split net names. Returns {"issues": [...]}.
        """
        return netlist.CHECKS[check](xml)

    return mcp


mcp = create_mcp()


def main() -> None:
    """stdio entry point — `traces-mcp`."""
    mcp.run()


if __name__ == "__main__":
    main()
