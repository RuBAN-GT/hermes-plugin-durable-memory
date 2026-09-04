import { type Plugin, tool } from "@opencode-ai/plugin"
import { execFile } from "node:child_process"
import { promisify } from "node:util"

const execFileAsync = promisify(execFile)

type Option = string | number | boolean | Record<string, unknown>

const actions: Record<string, Record<string, "string" | "number" | "boolean" | "object">> = {
  doctor: {},
  namespaces: {},
  "list-inventories": { namespace: "string" },
  search: {
    query: "string", namespace: "string", type: "string", filters: "object",
    limit: "number", cursor: "string", sort: "string", descending: "boolean",
  },
  propose: {
    operation: "string", namespace: "string", type: "string", identity: "string",
    text: "string", "record-id": "string", "expected-revision": "number",
    replace: "boolean", payload: "object",
  },
  "create-inventory": { type: "string", namespace: "string", fields: "object" },
}

function safeOptions(action: string, value: string | undefined): string[] {
  const specification = actions[action]
  if (!specification) throw new Error("Unsupported memory action")
  if (!value) return []
  let options: unknown
  try {
    options = JSON.parse(value)
  } catch {
    throw new Error("Options must be valid JSON")
  }
  if (!options || Array.isArray(options) || typeof options !== "object") {
    throw new Error("Options must be a JSON object")
  }
  const argv: string[] = []
  for (const [name, option] of Object.entries(options as Record<string, Option>)) {
    const expected = specification[name]
    if (!expected || typeof option !== expected || (expected === "object" && Array.isArray(option))) {
      throw new Error("Unsupported memory option")
    }
    argv.push(`--${name}`, typeof option === "object" ? JSON.stringify(option) : String(option))
  }
  return argv
}

// Copy this file to .opencode/plugins/durable-memory.ts in a consuming project.
export const DurableMemoryPlugin: Plugin = async () => ({
  tool: {
    durable_memory: tool({
      description:
        "Use Hermes durable-memory for namespaced memory, inventory, search, and approvals. Mutations may return a pending request.",
      args: {
        action: tool.schema.string().describe("Hermes durable-memory action"),
        options_json: tool.schema
          .string()
          .optional()
          .describe("JSON object of CLI option names without -- and string values"),
      },
      async execute(input) {
        const argv = ["durable-memory", input.action, ...safeOptions(input.action, input.options_json)]
        try {
          const result = await execFileAsync("hermes", argv, { windowsHide: true })
          return result.stdout.trim()
        } catch {
          throw new Error("Durable Memory command failed")
        }
      },
    }),
  },
})
