#!/bin/bash
# Claude Code statusLine script
# Shows: model, dir, % of context window used, aggregate session tokens/cost
# (summed from the transcript), and current-turn context usage.

node -e '
const fs = require("fs");

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", d => raw += d);

// Never let a malformed payload or a missing "end" event hang or crash the
// status line — always print something and exit.
const fallback = setTimeout(() => {
  try { process.stdout.write("Claude\n"); } catch (e) {}
  process.exit(0);
}, 1000);

process.stdin.on("end", () => {
  clearTimeout(fallback);
  try {
    render(raw);
  } catch (e) {
    try { process.stdout.write("Claude\n"); } catch (e2) {}
  }
});

function render(raw) {
  let d = {};
  try { d = JSON.parse(raw); } catch (e) {}

  const model = (d.model && d.model.display_name) || "Claude";
  const dirPath = (d.workspace && d.workspace.current_dir) || d.cwd || "";
  const dirName = dirPath ? (dirPath.split("/").filter(Boolean).pop() || "/") : "~";

  const cw = d.context_window || {};
  const curIn = cw.total_input_tokens;
  const curOut = cw.total_output_tokens;
  const usedPct = cw.used_percentage;
  const cost = d.cost && d.cost.total_cost_usd;

  // Aggregate tokens actually processed across the whole session: sum every
  // unique assistant message usage from the transcript. Streamed messages
  // repeat the same message.id with identical usage, so dedupe by id.
  // cache_read/creation is included because each API call is billed for it,
  // same as the cost.total_cost_usd figure — this is total tokens processed,
  // not unique content tokens (which would be far smaller, since context is
  // re-sent and re-read from cache every turn). Subagent/sidechain turns
  // share the same transcript file and are intentionally included here,
  // matching what cost.total_cost_usd bills.
  // Turn numbers, two flavors:
  // - allTurns: main-thread API calls (unique assistant messages). Every
  //   tool_result sent back to the API triggers another full call that
  //   reprocesses the whole growing conversation, so each assistant message
  //   is its own turn, not just each human-authored prompt.
  // - userTurns: human-authored prompts only (a "user" entry whose content
  //   is not purely tool_result blocks — tool_result replies are also sent
  //   as user-role messages but are not something a person typed).
  // Sidechain (subagent) calls reprocess their own separate, smaller context
  // rather than the main conversation, so both counts exclude them, even
  // though their tokens still count toward the session aggregate below.
  let aggIn = null, aggOut = null, allTurns = null, userTurns = null;
  if (d.transcript_path) {
    try {
      const lines = fs.readFileSync(d.transcript_path, "utf8").split("\n");
      const byId = new Map();
      const mainTurnIds = new Set();
      userTurns = 0;
      for (const line of lines) {
        if (!line) continue;
        let entry;
        try { entry = JSON.parse(line); } catch (e) { continue; }

        if (entry.type === "user" && !entry.isSidechain) {
          const content = entry.message && entry.message.content;
          const blocks = Array.isArray(content) ? content : null;
          const isToolResultOnly = blocks && blocks.length > 0 &&
            blocks.every(b => b && b.type === "tool_result");
          if (!isToolResultOnly) userTurns++;
        }

        const msg = entry.message;
        if (!msg || !msg.usage || !msg.id) continue;
        byId.set(msg.id, msg.usage);
        if (entry.type === "assistant" && !entry.isSidechain) mainTurnIds.add(msg.id);
      }
      allTurns = mainTurnIds.size;
      aggIn = 0; aggOut = 0;
      for (const u of byId.values()) {
        aggIn += (u.input_tokens || 0) + (u.cache_creation_input_tokens || 0) + (u.cache_read_input_tokens || 0);
        aggOut += (u.output_tokens || 0);
      }
    } catch (e) { /* transcript unreadable — leave agg/turn counts as null */ }
  }

  const fmtTok = n => {
    if (typeof n !== "number" || !isFinite(n)) return "n/a";
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  };
  const fmtCost = c => (typeof c === "number" && isFinite(c)) ? "$" + c.toFixed(2) : "n/a";
  const fmtPct = p => (typeof p === "number" && isFinite(p)) ? Math.round(p) + "%" : "n/a";
  const fmtTurn = n => (typeof n === "number" && isFinite(n)) ? String(n) : "n/a";
  const fmtTurnRatio = (user, all) => {
    if (user === null && all === null) return "n/a";
    return fmtTurn(user) + "/" + fmtTurn(all);
  };

  const DIM = "\x1b[2m", RESET = "\x1b[0m";
  const seg = (label, value) => DIM + label + RESET + " " + value;
  const sep = " " + DIM + "|" + RESET + " ";

  process.stdout.write(
    DIM + model + RESET + sep +
    DIM + dirName + RESET + sep +
    seg("turn:", fmtTurnRatio(userTurns, allTurns)) + sep +
    seg("ctx:", fmtPct(usedPct)) + sep +
    seg("sess in:", fmtTok(aggIn)) + " " + seg("out:", fmtTok(aggOut)) + " " + seg("$:", fmtCost(cost)) + sep +
    seg("now in:", fmtTok(curIn)) + " " + seg("out:", fmtTok(curOut)) +
    "\n"
  );
}
'
