---
name: src
description: Semantic Rule Check (SRC) for KiCad projects — reviews a local KiCad schematic/netlist for net-label typos, naming inconsistencies, orphan (single-pin) nets, and near-duplicate net names. Use when the user runs /src or asks to semantically review a KiCad schematic in the working directory.
---

# SRC — Semantic Rule Check for KiCad

SRC reviews the KiCad project in the current working directory for **semantic**
net problems that KiCad's own ERC cannot catch — the kind of mistakes that pass
electrical rules but are still bugs (a misspelled signal name, an inconsistent
bus, a wire that connects to only one pin). You (the agent) do the reasoning
directly; no server or API key is required.

## When to run

Trigger on `/src`, or when the user asks to "run SRC", "semantically check this
schematic", or "review the netlist".

## Procedure

### 1. Locate the design and produce a netlist

Find the schematic(s):

```bash
find . -name '*.kicad_sch' -not -path '*/backups/*'
```

Prefer a real netlist over raw `.kicad_sch` parsing. If `kicad-cli` is
available, export one (it expands hierarchy and resolves nets):

```bash
kicad-cli sch export netlist --format kicadxml -o /tmp/src-netlist.xml <root>.kicad_sch
```

If `kicad-cli` is not installed, fall back to reading the `.kicad_sch` files
directly and reasoning over the `(label ...)`, `(global_label ...)`, and wire
connectivity. Tell the user which path you took.

Collect the set of **net names** and, for each net, **how many pins/nodes** it
connects to.

### 2. Run the four checks

Apply each of these over the net list. Only report genuine issues; an empty
result for a check is a good outcome.

**a. Typos** — net names that are likely misspellings/transpositions of standard
signal names (TDX→TXD, SWIDO→SWDIO, CAHN→CANH, MOSIO→MOSI). Do **not** flag power
rails (GND, AGND, +3V3, +5V, VBUS, VCC, VDD, VBAT) or conventional abbreviations
(CLK, RST, EN, CS). Severity `error` for clear transpositions, `warning`
otherwise.

**b. Naming consistency** — groups of related nets using mixed conventions:
mixed separators (`CAN_H` + `CANL`), mixed prefix styles (`SPI_MOSI` + `SPIMISO`),
mixed casing (`usbDP` + `USB_DM`), mixed suffixes (`I2C_SCL` + `I2CSDA`). Do not
flag intentional complementary pairs (`CLK`/`CLK_N`, `DATA`/`DATA_B`). Severity
`warning`.

**c. Orphan nets** — any net connecting to exactly **one** pin (a single `<node>`
in the XML). These are almost always a dangling wire or a misnamed net. Do not
flag `PWR_FLAG`/`PWRFLAG` nets or KiCad auto-named nets (`Net-(...`). Severity
`error` for clear dangling connections, `warning` for plausible testpoints/stubs.
Include the component + pin in the message.

**d. Near-duplicates** — pairs of net names within edit distance 1–2 that look
like an accidental naming split (`VIN_GOOD`/`VIN_GOD`, `RESET`/`RESSET`,
`+3V3`/`+3V3A`). Do not flag intentional complementary pairs or genuinely
distinct rails (`+3V3` vs `+5V`). Severity `warning`. Format the location as
`NET_A / NET_B`.

### 3. Report

Print a grouped summary. For each issue: severity, the net name(s), and a
one-line explanation. End with a count, e.g. `SRC: 2 errors, 3 warnings`. If
everything is clean, say so explicitly.

```
SRC results for <project>
  [error]   typo          SWIDO        likely "SWDIO" (SWD I/O)
  [warning] consistency   CAN_H, CANL  mix _ separator within the CAN bus
  [error]   orphan        VBUS_SENSE   connects only to U3 pin 14
  ...
SRC: 2 errors, 1 warning
```

## Notes

- This is a read-only review. Do not modify the schematic unless the user asks.
- These checks mirror the semantic checks built into the traces KiCad extension,
  so results should be consistent between `/src` and the plugin's SRC panel.
