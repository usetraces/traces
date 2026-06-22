// Shared helpers for the sourcing agents.
//
// Provider-agnostic: talks the OpenAI chat-completions shape via the `openai`
// client, so the same code runs against OpenRouter (hosted) or a local Ollama
// instance — whatever LLM_BASE_URL / LLM_API_KEY / LLM_MODEL point at. The
// agent loop is a manual tool loop: we expose the supplier tools plus a
// `submit` sink, and stop as soon as the model calls `submit`.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import OpenAI from "openai";
import { z } from "zod";

export { z };

const __dirname = dirname(fileURLToPath(import.meta.url));

export function loadEnv() {
  // Fallback for running an agent directly: pull keys from the repo-root .env.
  for (const rel of ["../../../../.env", "../../.env"]) {
    try {
      for (const line of readFileSync(resolve(__dirname, rel), "utf8").split("\n")) {
        const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
        if (m && process.env[m[1]] === undefined) {
          process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
        }
      }
      break;
    } catch {}
  }
}

export function clientAndModel() {
  loadEnv();
  // Set by the Python launcher (node_agent.py); fall back to OpenRouter env.
  const baseURL = process.env.LLM_BASE_URL
    || (process.env.OPENROUTER_API_KEY ? "https://openrouter.ai/api/v1" : "http://localhost:11434/v1");
  const apiKey = process.env.LLM_API_KEY || process.env.OPENROUTER_API_KEY || "ollama";
  const model = process.env.LLM_MODEL || process.env.DEFAULT_MODEL || "gemma3";
  return { client: new OpenAI({ baseURL, apiKey }), model };
}

// Define a callable tool. `execute` is async ({...args}) => result; omit it for
// the terminal `submit` sink.
export function tool({ name, description, inputSchema, execute }) {
  return { name, description, inputSchema, execute };
}

// The structured-output sink. `shape` is a Zod object shape.
export function submitTool(shape) {
  return tool({
    name: "submit",
    description: "Submit the final answer. Call exactly once when done.",
    inputSchema: z.object(shape),
  });
}

function toFunctionDef(t) {
  return {
    type: "function",
    function: {
      name: t.name,
      description: t.description,
      parameters: z.toJSONSchema(t.inputSchema),
    },
  };
}

// Runs the tool loop and returns { result, usage, elapsed_ms, model }.
// result is the args object passed to `submit`, or null if never called.
export async function runAgent(client, { model, instructions, input, tools, maxSteps = 6 }) {
  const t0 = Date.now();
  const byName = Object.fromEntries(tools.map((t) => [t.name, t]));
  const functionDefs = tools.map(toFunctionDef);
  const messages = [
    { role: "system", content: instructions },
    { role: "user", content: input },
  ];

  let result = null;
  let usage = null;

  for (let step = 0; step < maxSteps; step++) {
    const resp = await client.chat.completions.create({
      model,
      messages,
      tools: functionDefs,
      tool_choice: "auto",
      temperature: 0,
    });
    usage = resp.usage ?? usage;
    const msg = resp.choices?.[0]?.message;
    if (!msg) break;
    messages.push(msg);

    const calls = msg.tool_calls || [];
    if (calls.length === 0) break;

    let submitted = false;
    for (const call of calls) {
      const t = byName[call.function?.name];
      let args = {};
      try {
        args = JSON.parse(call.function?.arguments || "{}");
      } catch {}
      if (call.function?.name === "submit") {
        result = args;
        submitted = true;
        continue;
      }
      let toolResult;
      try {
        toolResult = t ? await t.execute(args) : { error: `unknown tool ${call.function?.name}` };
      } catch (e) {
        toolResult = { error: String(e?.message || e) };
      }
      messages.push({
        role: "tool",
        tool_call_id: call.id,
        content: JSON.stringify(toolResult),
      });
    }
    if (submitted) break;
  }

  return { result, usage, elapsed_ms: Date.now() - t0, model };
}

export function emit(out) {
  process.stdout.write(JSON.stringify(out) + "\n");
}
