// The broker's HTTP surface, split from the process entrypoint so it can be
// started against an arbitrary config/workspace/port in tests. Serves a stateless
// Streamable-HTTP MCP endpoint at /mcp (a fresh server per request, as the SDK's
// stateless example does) plus a /healthz liveness endpoint.

import express, { type Express, type Request, type Response } from "express";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { BrokerConfig } from "./config.js";
import { buildServer } from "./server.js";
import type { Spawn } from "./commands.js";

function methodNotAllowed(_req: Request, res: Response): void {
  res.status(405).json({
    jsonrpc: "2.0",
    error: { code: -32000, message: "Method not allowed." },
    id: null,
  });
}

export function createApp(
  config: BrokerConfig,
  workspaceHost: string | undefined,
  doSpawn?: Spawn,
): Express {
  const app = express();
  app.use(express.json());

  app.post("/mcp", async (req: Request, res: Response) => {
    const server = buildServer(config, workspaceHost, doSpawn);
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    res.on("close", () => {
      transport.close();
      server.close();
    });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    } catch (err) {
      console.error("mcp POST failed", err);
      if (!res.headersSent) {
        res.status(500).json({
          jsonrpc: "2.0",
          error: { code: -32603, message: "Internal server error" },
          id: null,
        });
      }
    }
  });

  app.get("/mcp", methodNotAllowed);
  app.delete("/mcp", methodNotAllowed);

  app.get("/healthz", (_req, res) => {
    res.json({ ok: true });
  });

  return app;
}
