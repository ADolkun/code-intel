"use strict";
// `facts` mode: per-file parse with no Program. Each file is read, parsed and
// handed to the JSX and declaration extractors; a failure in one file becomes
// that file's status and never stops the batch.

const { ProtocolError, lineColumn, readSourceText, scriptKindFor, wellFormed } = require("./common.cjs");
const { parseStandalone } = require("./tsapi.cjs");
const { extractJsx } = require("./jsx_facts.cjs");
const { extractDecls } = require("./decl_facts.cjs");

const FACTS_SCHEMA = 1;
const DEFAULT_SNIPPET_MAX = 500;
const DEFAULT_SIGNATURE_MAX = 200;

function positiveInt(request, key, fallback) {
  if (!(key in request)) return fallback;
  const value = request[key];
  if (!Number.isInteger(value) || value < 1) {
    throw new ProtocolError(`${key} must be a positive integer, got ${JSON.stringify(value)}`);
  }
  return value;
}

// Tags whose elements carry their string attributes and nearest same-tag ancestor.
function attributeTags(request) {
  const tags = "attribute_tags" in request ? request.attribute_tags : [];
  if (!Array.isArray(tags) || !tags.every((t) => typeof t === "string" && t !== "")) {
    throw new ProtocolError("attribute_tags must be an array of non-empty tag names");
  }
  return new Set(tags);
}

// One line: the compiler process reports a failure with its Go stack.
function errorText(err) {
  const text = err && typeof err.message === "string" ? `${err.name || "Error"}: ${err.message}` : String(err);
  return text.split("\n", 1)[0];
}

// `session.api` is replaced when a request fails: a parser crash may take the
// compiler process down, and the next file must not inherit that.
function factsForFile(ts, session, filePath, ctx) {
  const out = {
    path: filePath,
    status: "ok",
    error: null,
    syntax_errors: 0,
    first_syntax_error: null,
    elements: [],
    decls: [],
  };
  const { text, readError, decodeError } = readSourceText(filePath);
  if (text === undefined) {
    out.status = "read_failed";
    out.error = wellFormed(readError);
    return out;
  }
  let parsed;
  try {
    // UTF-16 text can hold a lone surrogate, which the compiler's JSON requests
    // reject; replacing it keeps every offset.
    parsed = parseStandalone(ts, session.api, filePath, wellFormed(text), scriptKindFor(ts, filePath));
    const diags = parsed.syntaxDiagnostics;
    out.syntax_errors = diags.length;
    if (diags.length > 0) {
      const [line, column] = lineColumn(parsed.sf, diags[0].pos || 0);
      out.first_syntax_error = { line, column, message: wellFormed(diags[0].text) };
    }
  } catch (err) {
    // The parser itself failed, so there is no tree to extract from; that is
    // not an extractor failure.
    out.status = "parse_failed";
    out.error = wellFormed(errorText(err));
    out.syntax_errors = 0;
    out.first_syntax_error = null;
    session.restart();
    return out;
  }
  try {
    const fileCtx = { ...ctx, path: filePath };
    out.elements = extractJsx(ts, parsed.sf, fileCtx);
    out.decls = extractDecls(ts, parsed.sf, fileCtx);
    // The facts still come from the text with U+FFFD in place of the bad bytes.
    if (decodeError !== null) {
      out.status = "decode_failed";
      out.error = decodeError;
    }
  } catch (err) {
    out.status = "extract_failed";
    out.error = wellFormed(errorText(err));
    out.elements = [];
    out.decls = [];
  } finally {
    parsed.dispose();
  }
  return out;
}

class Session {
  constructor(ts) {
    this.ts = ts;
    this.api = new ts.API({ cwd: process.cwd() });
  }

  restart() {
    this.close();
    this.api = new this.ts.API({ cwd: process.cwd() });
  }

  close() {
    try {
      this.api.close();
    } catch {
      // the process may already be gone
    }
  }
}

function run(ts, request) {
  const files = request.files;
  if (!Array.isArray(files) || !files.every((f) => typeof f === "string" && f !== "")) {
    throw new ProtocolError("files must be an array of non-empty path strings");
  }
  const ctx = {
    snippetMax: positiveInt(request, "snippet_max", DEFAULT_SNIPPET_MAX),
    signatureMax: positiveInt(request, "signature_max", DEFAULT_SIGNATURE_MAX),
    attributeTags: attributeTags(request),
  };
  const session = new Session(ts);
  try {
    return {
      protocol: 1,
      mode: "facts",
      typescript_version: ts.version,
      facts_schema: FACTS_SCHEMA,
      files: files.map((filePath) => factsForFile(ts, session, filePath, ctx)),
    };
  } finally {
    session.close();
  }
}

module.exports = { run, FACTS_SCHEMA };
