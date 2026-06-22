// DigiKey sourcing via OpenRouter Agent SDK. Mirrors digikey_source().
// Ports the DigiKey OAuth (client_credentials) + keyword/product-details calls.
// Usage: node agents/digikey_agent.mjs "<search terms>" "<footprint>"
// Emits: {result:{part_number,qty,price,datasheet_url}, usage, elapsed_ms, model}.

import { clientAndModel, runAgent, submitTool, tool, z, emit, loadEnv } from "./lib.mjs";

loadEnv();

const CLIENT_ID = process.env.DIGIKEY_CLIENT_ID || process.env.CLIENT_ID;
const CLIENT_SECRET = process.env.DIGIKEY_CLIENT_SECRET || process.env.CLIENT_SECRET;
const USE_SANDBOX = ["1", "true", "yes"].includes(
  (process.env.DIGIKEY_USE_SANDBOX || process.env.USE_SANDBOX || "true").toLowerCase()
);
const API_BASE = USE_SANDBOX ? "https://sandbox-api.digikey.com" : "https://api.digikey.com";
const TOKEN_URL = `${API_BASE}/v1/oauth2/token`;

let token = null;
let tokenExp = 0;

async function getToken() {
  if (!CLIENT_ID || !CLIENT_SECRET)
    throw new Error("DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET must be set");
  if (token && Date.now() < tokenExp) return token;
  const res = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "client_credentials",
      client_id: CLIENT_ID,
      client_secret: CLIENT_SECRET,
    }),
  });
  if (!res.ok) throw new Error(`Digi-Key OAuth error: ${res.status} - ${await res.text()}`);
  const p = await res.json();
  token = p.access_token;
  tokenExp = Date.now() + (Number(p.expires_in || 1800) - 60) * 1000;
  return token;
}

async function headers() {
  return {
    Authorization: `Bearer ${await getToken()}`,
    "X-DIGIKEY-Client-Id": CLIENT_ID || "",
    "Content-Type": "application/json",
    "X-DIGIKEY-Locale-Site": "US",
    "X-DIGIKEY-Locale-Language": "en",
    "X-DIGIKEY-Locale-Currency": "USD",
    "X-DIGIKEY-Locale-ShipToCountry": "US",
    "X-DIGIKEY-Customer-Id": "0",
  };
}

async function request(method, url, body) {
  let h = await headers();
  let res = await fetch(url, {
    method,
    headers: h,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    token = null;
    tokenExp = 0;
    h = await headers();
    res = await fetch(url, { method, headers: h, body: body ? JSON.stringify(body) : undefined });
  }
  if (!res.ok) throw new Error(`Digi-Key API error: ${res.status} - ${await res.text()}`);
  return res.json();
}

function trimProduct(p) {
  const variations = (p.ProductVariations || []).map((v) => ({
    digikey_part_number: String(v.DigiKeyProductNumber || ""),
    package_type: String(v.PackageType?.Name || ""),
    unit_price: (v.StandardPricing || [])[0]?.UnitPrice ?? null,
    quantity_available: Number(v.QuantityAvailableforPackageType || 0),
  }));
  return {
    manufacturer_part_number: String(p.ManufacturerProductNumber || ""),
    manufacturer: String(p.Manufacturer?.Name || ""),
    description: String(p.Description?.ProductDescription || ""),
    datasheet_url: String(p.DatasheetUrl || ""),
    unit_price: p.UnitPrice ?? null,
    quantity_available: Number(p.QuantityAvailable || 0),
    variations,
  };
}

async function keywordSearch(keywords, limit = 5) {
  const data = await request("POST", `${API_BASE}/products/v4/search/keyword`, {
    Keywords: keywords,
    Limit: limit,
  });
  return (data.Products || []).map(trimProduct);
}

async function datasheetFor(productNumber) {
  try {
    const d = await request(
      "GET",
      `${API_BASE}/products/v4/search/${encodeURIComponent(productNumber)}/productdetails`
    );
    const url = String(d.Product?.DatasheetUrl || d.DatasheetUrl || "");
    if (url) return url;
  } catch {}
  try {
    const m = await request(
      "GET",
      `${API_BASE}/products/v4/search/${encodeURIComponent(productNumber)}/media`
    );
    for (const link of m.MediaLinks || []) {
      if (String(link.MediaType || "").toLowerCase() === "datasheets") return String(link.Url || "");
    }
  } catch {}
  return "";
}

const searchTool = tool({
  name: "keyword_search",
  description: "Search Digi-Key products by keyword. Returns products with variations (DigiKey part numbers), stock, pricing, datasheet.",
  inputSchema: z.object({ keywords: z.string() }),
  execute: async ({ keywords }) => ({ products: await keywordSearch(keywords) }),
});

const interpret = `Interpret the input as an electronics engineer would:
  - Bare resistor values ("100k", "4.7k", "10R", "1M") → search e.g. "100k ohm resistor 0805" (use given package).
  - Bare capacitor values ("100n", "10u", "100pF") → search e.g. "100nF capacitor 0805 X5R".
  - Bare inductor values ("10uH") → search similarly.
  - Expand abbreviations: k→kohm, u/µ→uF or uH (context), n→nF, p→pF, M→Mohm, R→ohm.
- Include the footprint/package in the query to disambiguate variants.
- If the input is an MPN, prefer exact matches.
- Prefer active, in-stock, RoHS-compliant catalog products.`;

async function sourceMode([searchTerms, footprint = ""]) {
  await keywordSearch(searchTerms, 1); // fail fast on bad credentials
  const { client, model } = clientAndModel();
  const out = await runAgent(client, {
    model,
    instructions: `Use keyword_search to find an in-stock Digi-Key supplier part, then call submit exactly once.

Input:
- Search description or MPN: ${searchTerms}
- Footprint/package: ${footprint}

Rules:
- ${interpret}

Output (submit):
- part_number must be a Digi-Key supplier part number — a digikey_part_number from a product variation (prefer Cut Tape / lowest reasonable).
- qty is that variation's quantity_available (or product quantity_available); price is best unit_price or null.
- datasheet_url is the product datasheet_url, or "".
- If no credible match, submit part_number="", qty=0, price=null, datasheet_url="".`,
    input: `Search: ${searchTerms}, package: ${footprint}`,
    tools: [searchTool, submitTool({
      part_number: z.string(),
      qty: z.number().int().min(0),
      price: z.number().min(0).nullable(),
      datasheet_url: z.string(),
    })],
  });
  if (out.result && out.result.part_number && !out.result.datasheet_url) {
    out.result.datasheet_url = await datasheetFor(out.result.part_number);
  }
  return out;
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
    instructions: `Use keyword_search to find in-stock Digi-Key parts, then call submit exactly once.

User request: ${description}

Constraints:
${constraints}

Rules:
- ${interpret}
- Return up to ${n} distinct candidate(s), best-to-worst.
- Each candidate: part_number is a Digi-Key supplier part number (a variation's digikey_part_number), qty is stock, price is best unit price or null, datasheet_url is the datasheet or "", justification is a concise one-line reason.
- Also fill: name (short human-readable part name), manufacturer, footprint (package), and specs (array of key electrical parameters as short strings, e.g. ["Vds: 30V", "Id: 5.7A"]). Prioritize any parameter the user explicitly asked for.
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

async function main() {
  const [mode, ...rest] = process.argv.slice(2);
  const fn = { source: sourceMode, search: searchMode }[mode];
  if (!fn) {
    console.error("usage: node digikey_agent.mjs <source|search> ...");
    process.exit(2);
  }
  emit(await fn(rest));
}

main().catch((e) => {
  console.error("AGENT_ERROR:", e?.message || e);
  process.exit(1);
});
