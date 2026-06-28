// HTTP entrypoint for the broker. Loads the baked command config, then serves a
// stateless Streamable-HTTP MCP endpoint at /mcp (a fresh server per request, as
// the SDK's stateless example does) plus a /healthz liveness endpoint (the
// example tong gates readiness with a TCP probe; /healthz is available for a
// `healthcheck`-mode readiness if one is configured). The config path, port, and
// workspace host path all come from the environment so the same image serves any
// baked or mounted configuration.

import express, { type Request, type Response } from "express";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { loadConfig } from "./config.js";
import { buildServer } from "./server.js";

const port = Number(process.env.PORT ?? 3000);
const configPath = process.env.BROKER_CONFIG ?? "/etc/swarmforge/broker.config.yaml";
const workspaceHost = process.env.SWARMFORGE_WORKSPACE_HOST_PATH;

const config = (() => {
  try {
    return loadConfig(configPath);
  } catch (err) {
    console.error(`broker config error: ${(err as Error).message}`);
    process.exit(1);
  }
})();

const app = express();
app.use(express.json());

app.post("/mcp", async (req: Request, res: Response) => {
  const server = buildServer(config, workspaceHost);
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

function methodNotAllowed(_req: Request, res: Response): void {
  res.status(405).json({
    jsonrpc: "2.0",
    error: { code: -32000, message: "Method not allowed." },
    id: null,
  });
}

app.get("/mcp", methodNotAllowed);
app.delete("/mcp", methodNotAllowed);

app.get("/healthz", (_req, res) => {
  res.json({ ok: true });
});

const httpServer = app.listen(port, () => {
  console.log(`${config.name} broker listening on :${port} (${config.commands.length} verb(s))`);
});

function shutdown(signal: string): void {
  console.log(`received ${signal}, shutting down`);
  httpServer.close(() => process.exit(0));
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
