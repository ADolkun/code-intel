"use strict";
// TypeScript helper entry point (protocol 1), on the TypeScript 7 API.
//
//   node helper.cjs < request.json
//
// Reads one JSON request from stdin and writes one JSON document to stdout.
// A rejected request exits 2 with a one-line {"protocol_error": "..."}; per-file
// failures are reported in the response and never change the exit code.

const fs = require("fs");
const path = require("path");
const { ProtocolError, loadTypeScript } = require("./common.cjs");

const PROTOCOL = 1;
const MODES = { facts: "facts.cjs", routes: "routes.cjs" };

function protocolError(message) {
  process.stdout.write(`${JSON.stringify({ protocol_error: message })}\n`);
  process.exitCode = 2;
}

function loadMode(mode) {
  const file = path.join(__dirname, MODES[mode]);
  if (!fs.existsSync(file)) {
    throw new ProtocolError(`mode ${JSON.stringify(mode)} is not available: ${file} not found`);
  }
  return require(file);
}

async function handle(raw) {
  let request;
  try {
    request = JSON.parse(raw);
  } catch (err) {
    throw new ProtocolError(`request is not valid JSON (${err.message})`);
  }
  if (request === null || typeof request !== "object" || Array.isArray(request)) {
    throw new ProtocolError("request must be a JSON object");
  }
  if (request.protocol !== PROTOCOL) {
    throw new ProtocolError(`unsupported protocol ${JSON.stringify(request.protocol)}, expected ${PROTOCOL}`);
  }
  if (!Object.prototype.hasOwnProperty.call(MODES, request.mode)) {
    throw new ProtocolError(`unknown mode ${JSON.stringify(request.mode)}, expected "facts" or "routes"`);
  }
  const ts = await loadTypeScript(request.typescript_dir);
  return loadMode(request.mode).run(ts, request);
}

function main() {
  const chunks = [];
  process.stdin.on("data", (chunk) => chunks.push(chunk));
  process.stdin.on("error", (err) => {
    process.stderr.write(`helper: could not read request from stdin: ${err.message}\n`);
    process.exitCode = 1;
  });
  process.stdin.on("end", async () => {
    let response;
    try {
      response = await handle(Buffer.concat(chunks).toString("utf8"));
    } catch (err) {
      if (err instanceof ProtocolError) {
        protocolError(err.message);
        return;
      }
      process.stderr.write(`helper: request failed: ${(err && err.stack) || err}\n`);
      process.exitCode = 1;
      return;
    }
    // exitCode rather than process.exit(): stdout to a pipe may still be flushing.
    process.stdout.write(JSON.stringify(response));
  });
}

main();
