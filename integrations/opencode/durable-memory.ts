import { type Plugin, tool } from "@opencode-ai/plugin"
import { execFile } from "node:child_process"
import { promisify } from "node:util"

const execFileAsync = promisify(execFile)

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
        const argv = ["durable-memory", input.action]
        const options = input.options_json ? JSON.parse(input.options_json) : {}
        for (const [name, value] of Object.entries(options)) {
          if (!/^[a-z][a-z-]*$/.test(name) || typeof value !== "string") {
            throw new Error("Options must have safe names and string values")
          }
          argv.push(`--${name}`, value)
        }
        const result = await execFileAsync("hermes", argv, { windowsHide: true })
        return result.stdout.trim() || result.stderr.trim()
      },
    }),
  },
})
