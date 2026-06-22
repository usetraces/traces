// Mouser sourcing via OpenRouter Agent SDK. Mirrors mouser_source().
// Usage: node agents/mouser_agent.mjs "<search terms>" "<footprint>"
// Emits: {result:{part_number,qty,price,datasheet_url}, usage, elapsed_ms, model}.

import { clientAndModel, runAgent, submitTool, tool, z, emit, loadEnv } from "./lib.mjs";

loadEnv();

const API_KEY = process.env.MOUSER_PART_API_KEY || process.env.MOUSER_API_KEY;
const API_BASE = (process.env.MOUSER_API_BASE_URL || "https://api.mouser.com/api/v1").replace(/\/$/, "");

async function post(path, body) {
  if (!API_KEY) throw new Error("MOUSER_PART_API_KEY or MOUSER_API_KEY must be set");
  const url = `${API_BASE}/${path}?${new URLSearchParams({ apiKey: API_KEY })}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Mouser API error: ${res.status} - ${await res.text()}`);
  const data = await res.json();
  if ((data.Errors || []).length) throw new Error(`Mouser API errors: ${JSON.stringify(data.Errors)}`);
  return data;
}

function trimPart(p) {
  const breaks = (p.PriceBreaks || []).map((b) => ({
    quantity: b.Quantity,
    price: b.Price ? Number(String(b.Price).replace(/[^0-9.]/g, "")) : null,
  }));
  return {
    part_number: String(p.MouserPartNumber || ""),
    manufacturer_part_number: String(p.ManufacturerPartNumber || ""),
    description: String(p.Description || ""),
    availability: String(p.Availability || ""),
    datasheet_url: String(p.DataSheetUrl || ""),
    price_breaks: breaks,
  };
}

async function keywordSearch(keyword, records = 5) {
  const data = await post("search/keyword", {
    SearchByKeywordRequest: { keyword, records, startingRecord: 0 },
  });
  return (data.SearchResults?.Parts || []).map(trimPart);
}

async function partSearch(mouserPartNumber) {
  const data = await post("search/partnumber", {
    SearchByPartRequest: { mouserPartNumber },
  });
  return (data.SearchResults?.Parts || []).map(trimPart);
}

const keywordTool = tool({
  name: "keyword_search",
  description: "Search Mouser by keyword. Returns parts with availability, price breaks, datasheet.",
  inputSchema: z.object({ keyword: z.string() }),
  execute: async ({ keyword }) => ({ parts: await keywordSearch(keyword) }),
});

const partTool = tool({
  name: "part_search",
  description: "Search Mouser by exact Mouser or manufacturer part number.",
  inputSchema: z.object({ part_number: z.string() }),
  execute: async ({ part_number }) => ({ parts: await partSearch(part_number) }),
});

const interpret = `Interpret the input as an electronics engineer would:
  - Bare resistor values ("100k", "4.7k", "10R", "1M") → search e.g. "100k ohm resistor 0805" (use given package).
  - Bare capacitor values ("100n", "10u", "100pF") → search e.g. "100nF capacitor 0805 X5R".
  - Bare inductor values ("10uH") → search similarly.
  - Expand abbreviations: k→kohm, u/µ→uF or uH (context), n→nF, p→pF, M→Mohm, R→ohm.
- Include the footprint/package in the query to disambiguate variants.
- If the input is an MPN, prefer exact matches via part_search.
- Prefer active, in-stock, RoHS-compliant products.`;

async function sourceMode([searchTerms, footprint = ""]) {
  await keywordSearch(searchTerms, 1); // fail fast on bad credentials
  const { client, model } = clientAndModel();
  return runAgent(client, {
    model,
    instructions: `Use keyword_search (and part_search for exact MPNs) to find an in-stock Mouser supplier part, then call submit exactly once.

Input:
- Search description or MPN: ${searchTerms}
- Footprint/package: ${footprint}

Rules:
- ${interpret}

Output (submit):
- part_number must be the Mouser supplier part number (the part_number field).
- qty is stock parsed from availability (e.g. "12345 In Stock" → 12345); price is best unit price from price_breaks or null.
- datasheet_url is the datasheet_url, or "".
- If no credible match, submit part_number="", qty=0, price=null, datasheet_url="".`,
    input: `Search: ${searchTerms}, package: ${footprint}`,
    tools: [keywordTool, partTool, submitTool({
      part_number: z.string(),
      qty: z.number().int().min(0),
      price: z.number().min(0).nullable(),
      datasheet_url: z.string(),
    })],
  });
}

async function searchMode([description, footprint = "", maxPrice = "", minQty = "", count = "1"]) {
  await keywordSearch(description, 1);
  const n = Math.max(1, parseInt(count, 10) || 1);
  const constraints = [
    footprint && `- Package/footprint must match: ${footprint}`,
    maxPrice && `- Unit price must be ≤ $${maxPrice}`,
    minQty && `- Stock must be ≥ ${minQty} units`,
  ].filter(Boolean).join("\n") || "None.";
  const { client, model } = clientAndModel();
  return runAgent(client, {
    model,
    instructions: `Use keyword_search (and part_search for exact MPNs) to find in-stock Mouser parts, then call submit exactly once.

User request: ${description}

Constraints:
${constraints}

Rules:
- ${interpret}
- Return up to ${n} distinct candidate(s), best-to-worst.
- Each candidate: part_number is the Mouser supplier part number, qty is stock (parse availability), price is best unit price or null, datasheet_url is the datasheet or "", justification is a concise one-line reason.
- Also fill: name (short human-readable part name), manufacturer, footprint (package), and specs (array of key electrical parameters as short strings, e.g. ["Vds: 30V", "Id: 5.7A"]). Prioritize any parameter the user explicitly asked for.
- If no credible match, submit an empty candidates array.`,
    input: `Request: ${description}, package: ${footprint}`,
    tools: [keywordTool, partTool, submitTool({
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

async function main() {
  const [mode, ...rest] = process.argv.slice(2);
  const fn = { source: sourceMode, search: searchMode }[mode];
  if (!fn) {
    console.error("usage: node mouser_agent.mjs <source|search> ...");
    process.exit(2);
  }
  emit(await fn(rest));
}

main().catch((e) => {
  console.error("AGENT_ERROR:", e?.message || e);
  process.exit(1);
});
