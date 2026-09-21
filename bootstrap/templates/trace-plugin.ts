import { appendFileSync } from "node:fs"

export default async () => {
  const path = process.env.PRO_TRACE_PATH || "/swe/output/events.jsonl"
  const log = (kind: string, data: unknown) => appendFileSync(path, JSON.stringify({ kind, at: Date.now() / 1000, monotonic_ns: process.hrtime.bigint().toString(), data }) + "\n")
  log("plugin.loaded", { pid: process.pid })
  return {
    event: async ({ event }: { event: unknown }) => { log("event", event) },
    "chat.headers": async (input: { sessionID: string; agent: string; message: { id: string } }, output: { headers: Record<string, string> }) => {
      output.headers["x-pro-session"] = input.sessionID
      output.headers["x-pro-agent"] = input.agent
      output.headers["x-pro-message"] = input.message.id
      log("model.prepare", { sessionID: input.sessionID, agent: input.agent, messageID: input.message.id })
    },
    "tool.execute.before": async (input: { tool: string }, output: { args: Record<string, unknown> }) => {
      if (input.tool === "bash") {
        const requested = output.args.timeout
        output.args.timeout = 450000
        log("budget.tool", { tool: input.tool, requested_timeout_ms: requested, effective_timeout_ms: 450000 })
      }
      log("tool.before", { input, output })
    },
    "tool.execute.after": async (input: unknown, output: unknown) => { log("tool.after", { input, output }) },
  }
}
