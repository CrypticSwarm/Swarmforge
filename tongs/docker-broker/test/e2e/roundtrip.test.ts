// End-to-end: drive the real broker over HTTP MCP and let it spawn real worker
// containers, then assert the workers actually touched the mounted workspace.
//
// Unlike the unit tests (which inject a mock Spawn), this exercises the whole
// path -- MCP client -> HTTP app -> buildServer -> runWorker -> `docker run` -> a
// container writing into the bind-mounted workspace -> the file on disk. It runs
// the harness directly on the host (not docker-in-docker) so the workspace host
// path is just a local temp dir the daemon can bind-mount without translation.
//
// Skipped when docker is unavailable so `npm run test:e2e` degrades gracefully
// off CI. The worker runs as root, so the files it leaves are root-owned but
// world-readable -- fine to stat and read back here.

import { after, before, describe, test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { parseConfig } from "../../src/config.js";
import { createApp } from "../../src/app.js";

const WORKER_IMAGE = "alpine:3.20";

function dockerUnavailableReason(): string | false {
  try {
    execFileSync("docker", ["version"], { stdio: "ignore" });
    return false;
  } catch {
    return "docker is not available";
  }
}

describe("broker end-to-end round trip", { skip: dockerUnavailableReason() }, () => {
  let workspace: string;
  let httpServer: Server;
  let client: Client;

  before(async () => {
    workspace = mkdtempSync(join(tmpdir(), "broker-e2e-"));
    const config = parseConfig({
      name: "e2e-broker",
      description: "End-to-end fixture broker.",
      allowed_images: [WORKER_IMAGE],
      commands: [
        {
          name: "write_a",
          description: "Write endpoint A's marker file into the workspace.",
          image: WORKER_IMAGE,
          mounts: ["workspace:/work"],
          workdir: "/work",
          command: ["sh", "-c", "echo A > /work/a.txt"],
        },
        {
          name: "write_b",
          description: "Write endpoint B's marker file into the workspace.",
          image: WORKER_IMAGE,
          mounts: ["workspace:/work"],
          workdir: "/work",
          command: ["sh", "-c", "echo B > /work/b.txt"],
        },
      ],
    });

    httpServer = await new Promise<Server>((resolve) => {
      const s = createApp(config, workspace).listen(0, () => resolve(s));
    });
    const { port } = httpServer.address() as AddressInfo;

    client = new Client({ name: "e2e-client", version: "0.0.0" });
    await client.connect(new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`)));
  });

  after(async () => {
    await client?.close();
    httpServer?.close();
    if (workspace) rmSync(workspace, { recursive: true, force: true });
  });

  // Each endpoint: call the verb over MCP, confirm the broker reports success,
  // and confirm the worker container actually wrote its file into the workspace.
  for (const { verb, file, marker } of [
    { verb: "write_a", file: "a.txt", marker: "A" },
    { verb: "write_b", file: "b.txt", marker: "B" },
  ]) {
    test(`${verb} runs a worker that writes ${file}`, { timeout: 120_000 }, async () => {
      const result = (await client.callTool({ name: verb, arguments: {} })) as {
        isError?: boolean;
        content: Array<{ type: string; text: string }>;
      };

      assert.ok(!result.isError, `expected ${verb} to succeed, got: ${result.content?.[0]?.text}`);
      assert.match(result.content[0].text, /success/);
      assert.equal(readFileSync(join(workspace, file), "utf8").trim(), marker);
    });
  }
});
