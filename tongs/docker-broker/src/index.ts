// HTTP entrypoint for the broker. Loads the baked command config and serves the
// MCP app (see app.ts). The config path, port, and workspace host path all come
// from the environment so the same image serves any baked or mounted config.

import { loadConfig } from "./config.js";
import { createApp } from "./app.js";

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

const httpServer = createApp(config, workspaceHost).listen(port, () => {
  console.log(`${config.name} broker listening on :${port} (${config.commands.length} verb(s))`);
});

function shutdown(signal: string): void {
  console.log(`received ${signal}, shutting down`);
  httpServer.close(() => process.exit(0));
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
