// JLCPCB agent via OpenRouter Agent SDK. Modes: source | search | datasheet.
//   node jlcpcb_agent.mjs source  <identifier> <footprint> [MPN|LCSC]
//   node jlcpcb_agent.mjs search  <description> <footprint> <maxPrice|""> <minQty|""> <count>
//   node jlcpcb_agent.mjs datasheet <lcsc>
// Emits one JSON line: {result, usage, elapsed_ms, model}.

import { clientAndModel, runAgent, submitTool, tool, z, emit } from "./lib.mjs";

const JLCPCB_SEARCH_API =
  "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2";

async function jlcpcbSearch(keyword) {
  const res = await fetch(JLCPCB_SEARCH_API, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ currentPage: 1, pageSize: 5, keyword, searchType: 2 }),
  });
  const data = await res.json();
  if (data.code !== 200) throw new Error(`JLCPCB API error: ${data.message || data.code}`);
  const list = data?.data?.componentPageInfo?.list || [];
  return list.map((c) => {
    const prices = c.componentPrices || [];
    return {
      part_number: String(c.componentCode || ""),
      mpn: String(c.componentModelEn || c.componentModel || ""),
      package: String(c.componentSpecificationEn || c.componentSpecification || ""),
      qty: Number(c.stockCount || 0),
      price: prices.length ? Number(prices[0].productPrice) : null,
      datasheet_url: String(c.dataManualUrl || ""),
    };
  });
}

const searchTool = tool({
  name: "component_search",
  description:
    "Search JLCPCB/LCSC for in-stock components by C-number, MPN, or 'value package'. Returns up to 5 candidates.",
  inputSchema: z.object({
    keyword: z.string().describe("C-number, MPN, or 'value package' e.g. 'STM32G491C LQFP48'"),
  }),
  execute: async ({ keyword }) => ({ candidates: await jlcpcbSearch(keyword) }),
});

const interpret = `Interpret the request as an electronics engineer would. If it looks like a component value (e.g. "2.2k", "100nF", "10uH"), search by value + package. If it looks like an MPN, prefer exact MPN matches. If it looks like a C-number (e.g. C2040), look it up directly.`;

async function sourceMode([identifier, footprint = "", idType = "MPN"]) {
  const { client, model } = clientAndModel();
  return runAgent(client, {
    model,
    instructions: `Use component_search to find an in-stock JLCPCB/LCSC supplier part, then call submit exactly once.

Input:
- ${idType}: ${identifier}
- Footprint/package: ${footprint}

Search strategy:
- C-number (e.g. C2040): search that exact C-number. One search is enough.
- MPN: search the exact MPN first. If empty or no package match, try once more with MPN + package. Pick the variant whose package matches. Stop after two searches.
- Component value: search by value + package in one query.

Output rules (submit):
- part_number must be the JLC/LCSC C-number (e.g. C2040).
- qty is current stock; price is best unit price or null.
- If no credible match, submit part_number="", qty=0, price=null.`,
    input: `${idType}: ${identifier}, package: ${footprint}`,
    tools: [searchTool, submitTool({
      part_number: z.string().describe("JLC/LCSC C-number, or '' if no credible match"),
      qty: z.number().int().min(0),
      price: z.number().min(0).nullable(),
    })],
  });
}

async function searchMode([description, footprint = "", maxPrice = "", minQty = "", count = "1"]) {
  const n = Math.max(1, parseInt(count, 10) || 1);
  const constraints = [
    footprint && `- Package/footprint must match: ${footprint}`,
    maxPrice && `- Unit price must be ≤ $${maxPrice}`,
    minQty && `- Stock must be ≥ ${minQty} units`,
  ].filter(Boolean).join("\n") || "None.";
  const { client, model } = clientAndModel();
  return runAgent(client, {
    model,
    instructions: `Use component_search to find in-stock JLCPCB/LCSC parts, then call submit exactly once.

User request: ${description}

Constraints:
${constraints}

Rules:
- ${interpret}
- Prefer basic/preferred JLCPCB parts; extended parts are acceptable.
- Return up to ${n} distinct candidate(s), best-to-worst.
- Each candidate: part_number is the C-number, qty is current stock, price is best unit price or null, datasheet_url is the datasheet URL or "", justification is a concise one-line reason.
- Also fill: name (short human-readable part name, e.g. "10kΩ ±1% 0603 resistor"), manufacturer, footprint (package, e.g. "0603", "SOT-23"), and specs (array of the key electrical parameters as short strings, e.g. ["Vds: 30V", "Id: 5.7A", "Rds(on): 28mΩ"]). Prioritize any parameter the user explicitly asked for.
- If no credible match, submit an empty candidates array.`,
    input: `Request: ${description}, package: ${footprint}`,
    tools: [searchTool, submitTool({
      candidates: z.array(z.object({
        part_number: z.string(),
        qty: z.number().int().min(0),
        price: z.number().min(0).nullable(),
        datasheet_url: z.string(),
        justification: z.string(),
        name: z.string().optional(),
        manufacturer: z.string().optional(),
        footprint: z.string().optional(),
        specs: z.array(z.string()).optional(),
      })),
    })],
  });
}

async function datasheetMode([lcsc]) {
  const { client, model } = clientAndModel();
  return runAgent(client, {
    model,
    instructions: `Use component_search to find the datasheet PDF URL for LCSC part ${lcsc}, then call submit exactly once.
- Search the exact LCSC part number ${lcsc}.
- Extract the datasheet_url from the matching candidate.
- If none is found, submit url="https://www.lcsc.com/datasheet/${lcsc}.pdf".
- url must be a complete absolute URL to a PDF; lcsc must be exactly ${lcsc}.`,
    input: `LCSC: ${lcsc}`,
    tools: [searchTool, submitTool({
      url: z.string(),
      lcsc: z.string(),
    })],
  });
}

async function main() {
  const [mode, ...rest] = process.argv.slice(2);
  const fn = { source: sourceMode, search: searchMode, datasheet: datasheetMode }[mode];
  if (!fn) {
    console.error("usage: node jlcpcb_agent.mjs <source|search|datasheet> ...");
    process.exit(2);
  }
  emit(await fn(rest));
}

main().catch((e) => {
  console.error("AGENT_ERROR:", e?.message || e);
  process.exit(1);
});
