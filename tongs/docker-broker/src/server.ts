// Maps a validated broker config onto an MCP server: one tool per command, whose
// input schema is derived from the command's declared parameters. A boolean param
// becomes an optional boolean; an enum param becomes a (required or optional)
// string constrained to its allowed values, so the harness can only ever send a
// value the config already sanctioned.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { BrokerConfig, CommandDef } from "./config.js";
import { formatResult, runWorker, type Spawn } from "./commands.js";

function buildInputSchema(command: CommandDef): Record<string, z.ZodTypeAny> {
  const shape: Record<string, z.ZodTypeAny> = {};
  for (const param of command.params) {
    if (param.type === "boolean") {
      shape[param.name] = z
        .boolean()
        .optional()
        .describe(param.description || `When true, apply the '${param.name}' option.`);
    } else {
      const base = z.enum(param.values as [string, ...string[]]);
      const described = (param.required ? base : base.optional()).describe(
        param.description || `One of: ${param.values.join(", ")}.`,
      );
      shape[param.name] = described;
    }
  }
  return shape;
}

function serverInstructions(config: BrokerConfig): string {
  const header =
    config.description ||
    "A Swarmforge broker: spawns a fixed set of narrow worker containers on demand.";
  const verbs = config.commands.map((c) => `  - ${c.name} -- ${c.description}`).join("\n");
  return `${header}\n\nExposed verbs:\n${verbs}\n\nEach verb runs a pre-defined container to completion and returns its combined output; no arbitrary images or mounts can be requested.`;
}

export function buildServer(
  config: BrokerConfig,
  workspaceHost: string | undefined,
  doSpawn?: Spawn,
): McpServer {
  const server = new McpServer(
    { name: config.name, version: "0.1.0" },
    { instructions: serverInstructions(config) },
  );
  for (const command of config.commands) {
    server.registerTool(
      command.name,
      {
        title: command.name,
        description: command.description,
        inputSchema: buildInputSchema(command),
      },
      async (args: Record<string, unknown>) => {
        try {
          const result = await runWorker(command, args ?? {}, workspaceHost, doSpawn);
          return { content: [{ type: "text" as const, text: formatResult(command.name, result) }] };
        } catch (err) {
          return {
            content: [{ type: "text" as const, text: `${command.name}: error: ${(err as Error).message}` }],
            isError: true,
          };
        }
      },
    );
  }
  return server;
}
