"use strict";
// Helpers shared by the TypeScript helper modes. CommonJS because the helper
// lives outside the frontend, whose package.json may set "type": "module".

const fs = require("fs");
const { LoadError, loadTypeScript: loadApi } = require("./tsapi.cjs");

// A request the helper refuses: helper.cjs exits 2 with {"protocol_error"}.
class ProtocolError extends Error {}

// Loads the TypeScript API from the package in `dir` (see tsapi.cjs). Checks
// package.json first so a CODE_INTEL_TYPESCRIPT_DIR pointing at the wrong package
// fails with a named error. This guards against mistakes, not against a hostile
// directory: the package's own code runs.
async function loadTypeScript(dir) {
  if (typeof dir !== "string" || dir === "") {
    throw new ProtocolError("typescript_dir must be a non-empty string");
  }
  try {
    return await loadApi(dir);
  } catch (err) {
    if (err instanceof LoadError) throw new ProtocolError(err.message);
    throw err;
  }
}

// Wrappers that do not change an expression's value for analysis purposes.
function isTransparent(ts, node) {
  return (
    !!node &&
    (ts.isParenthesizedExpression(node) ||
      ts.isAsExpression(node) ||
      ts.isSatisfiesExpression(node) ||
      ts.isNonNullExpression(node) ||
      ts.isTypeAssertionExpression(node))
  );
}

function unwrap(ts, expr) {
  let e = expr;
  while (isTransparent(ts, e)) e = e.expression;
  return e;
}

const SURROGATE_RE = /[\uD800-\uDFFF]/;
const LF = 10;
const CR = 13;

// Line starts that break only at \r\n, \r and \n. TypeScript also breaks at
// U+2028 and U+2029, which editors, rg and Python's line counting do not, so
// one such character in a string would shift every later line number.
const lineStartCache = new WeakMap();

function lineStarts(sf) {
  let starts = lineStartCache.get(sf);
  if (starts === undefined) {
    starts = [0];
    const text = sf.text;
    for (let i = 0; i < text.length; i += 1) {
      const c = text.charCodeAt(i);
      if (c === LF) starts.push(i + 1);
      else if (c === CR) {
        if (text.charCodeAt(i + 1) === LF) i += 1;
        starts.push(i + 1);
      }
    }
    lineStartCache.set(sf, starts);
  }
  return starts;
}

// 0-based index of the line holding `pos`.
function lineIndex(sf, pos) {
  const starts = lineStarts(sf);
  let lo = 0;
  let hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (starts[mid] <= pos) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

// 1-based [line, column]; the column counts code points so it agrees with
// Python string indexing on the same line.
function lineColumn(sf, pos) {
  const index = lineIndex(sf, pos);
  const slice = sf.text.slice(lineStarts(sf)[index], pos);
  const width = SURROGATE_RE.test(slice) ? Array.from(slice).length : slice.length;
  return [index + 1, width + 1];
}

const LINE_END_RE = /(?:\r\n|\r|\n)$/;

// Text of 1-based `line` without its terminator; "" when out of range.
function lineText(sf, line) {
  const starts = lineStarts(sf);
  if (!Number.isInteger(line) || line < 1 || line > starts.length) return "";
  const end = line < starts.length ? starts[line] : sf.text.length;
  return sf.text.slice(starts[line - 1], end).replace(LINE_END_RE, "");
}

function scriptKindFor(ts, filePath) {
  const lower = filePath.toLowerCase();
  if (lower.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (lower.endsWith(".ts") || lower.endsWith(".mts") || lower.endsWith(".cts")) return ts.ScriptKind.TS;
  if (lower.endsWith(".jsx")) return ts.ScriptKind.JSX;
  if (lower.endsWith(".js") || lower.endsWith(".mjs") || lower.endsWith(".cjs")) return ts.ScriptKind.JS;
  throw new Error(`${filePath}: unsupported file extension`);
}

const LONE_SURROGATE_RE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;

// Lone surrogates would reach Python as unencodable str values.
function wellFormed(s) {
  if (typeof s !== "string") return s;
  if (typeof s.toWellFormed === "function") return s.toWellFormed();
  return s.replace(LONE_SURROGATE_RE, "\uFFFD");
}

// The README parent rule: a function declaration or expression with a name,
// or an arrow/function expression that initializes a variable directly or as
// a direct call argument (through transparent wrappers).
function readmeFunctionName(ts, fn) {
  if ((ts.isFunctionDeclaration(fn) || ts.isFunctionExpression(fn)) && fn.name) return fn.name.text;
  if (ts.isArrowFunction(fn) || ts.isFunctionExpression(fn)) {
    let c = fn;
    let p = fn.parent;
    for (;;) {
      if (isTransparent(ts, p)) {
        c = p;
        p = p.parent;
        continue;
      }
      if (p && ts.isCallExpression(p) && p.arguments.includes(c)) {
        c = p;
        p = p.parent;
        continue;
      }
      break;
    }
    if (p && ts.isVariableDeclaration(p) && ts.isIdentifier(p.name) && p.initializer === c) return p.name.text;
  }
  return undefined;
}

// The README rule, then method/accessor name, property-assignment name,
// class-field name, assignment target or "default" for `export default`.
function callerName(ts, fn) {
  const readme = readmeFunctionName(ts, fn);
  if (readme !== undefined) return readme;
  if ((ts.isMethodDeclaration(fn) || ts.isGetAccessor(fn) || ts.isSetAccessor(fn)) && fn.name) {
    return fn.name.getText();
  }
  if (ts.isArrowFunction(fn) || ts.isFunctionExpression(fn)) return assignedName(ts, fn);
  return undefined;
}

// The name a function expression is stored under, seen through wrappers and
// the calls it is an argument of: a property, a class field, an assignment
// target, or "default" for `export default`.
function assignedName(ts, fn) {
  let c = fn;
  let p = fn.parent;
  while (isTransparent(ts, p) || (p && ts.isCallExpression(p) && p.arguments.includes(c))) {
    c = p;
    p = p.parent;
  }
  if (!p) return undefined;
  if ((ts.isPropertyAssignment(p) || ts.isPropertyDeclaration(p)) && p.initializer === c) return p.name.getText();
  if (ts.isBinaryExpression(p) && p.operatorToken.kind === ts.SyntaxKind.EqualsToken && p.right === c) {
    return p.left.getText();
  }
  if (ts.isExportAssignment(p)) return "default";
  return undefined;
}

function isComponentName(name) {
  return typeof name === "string" && /^[A-Z]/.test(name) && /[a-z]/.test(name);
}

// JSX directly in a function's `body`, not inside a nested function-like. A
// body that is itself a function (a curried arrow) belongs to that nested
// function, so it never counts. Iterative: deep JSX trees must not overflow
// the stack.
function containsJsxOutsideNestedFunctions(ts, body) {
  if (!body || ts.isFunctionLike(body)) return false;
  const stack = [body];
  while (stack.length > 0) {
    const node = stack.pop();
    if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node) || ts.isJsxFragment(node)) return true;
    if (node !== body && ts.isFunctionLike(node)) continue;
    ts.forEachChild(node, (child) => {
      stack.push(child);
    });
  }
  return false;
}

// Pre-order walk with an explicit stack, children in source order: the parser
// accepts nesting deeper than a recursive walk could follow on Node's default
// stack. `visit` returns false to skip a node's children.
function walkNodes(ts, root, visit) {
  const stack = [root];
  while (stack.length > 0) {
    const node = stack.pop();
    if (visit(node) === false) continue;
    const kids = [];
    ts.forEachChild(node, (child) => {
      kids.push(child);
    });
    for (let i = kids.length - 1; i >= 0; i -= 1) stack.push(kids[i]);
  }
}

// For a UTF-8 lead byte: [continuation bytes, smallest code point it may
// encode]; null for a byte that cannot start a sequence.
function utf8Lead(b) {
  if (b >= 0xc2 && b <= 0xdf) return [1, 0x80];
  if (b >= 0xe0 && b <= 0xef) return [2, 0x800];
  if (b >= 0xf0 && b <= 0xf4) return [3, 0x10000];
  return null;
}

// Length of the valid multi-byte sequence at `i`, or 0 when it is invalid.
function utf8SequenceLength(bytes, i) {
  const lead = utf8Lead(bytes[i]);
  if (lead === null) return 0;
  const [need, min] = lead;
  let cp = bytes[i] & (0x3f >> need);
  for (let k = 1; k <= need; k += 1) {
    const c = bytes[i + k];
    if (c === undefined || (c & 0xc0) !== 0x80) return 0;
    cp = (cp << 6) | (c & 0x3f);
  }
  if (cp < min || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) return 0;
  return need + 1;
}

// Byte offset of the first invalid UTF-8 sequence, or -1.
function invalidUtf8Offset(bytes) {
  let i = 0;
  while (i < bytes.length) {
    const length = bytes[i] < 0x80 ? 1 : utf8SequenceLength(bytes, i);
    if (length === 0) return i;
    i += length;
  }
  return -1;
}

function hasUtf16Bom(bytes) {
  return bytes.length >= 2 && ((bytes[0] === 0xfe && bytes[1] === 0xff) || (bytes[0] === 0xff && bytes[1] === 0xfe));
}

const UTF8 = new TextDecoder("utf-8");

// UTF-16 text with a byte order mark, without the mark.
function decodeUtf16(bytes) {
  const little = bytes[0] === 0xff;
  // An odd trailing byte is dropped, as TypeScript's own reader did.
  const body = Buffer.from(bytes.subarray(2, bytes.length - ((bytes.length - 2) % 2)));
  if (!little) body.swap16();
  return body.toString("utf16le");
}

// The file's text as TypeScript reads it (UTF-8 with or without a BOM, UTF-16
// with a BOM), plus `decodeError` when bytes are not valid UTF-8: the decoder
// replaces them with U+FFFD, which would otherwise pass silently.
// `readError` is set (and `text` undefined) when the file cannot be read.
function readSourceText(filePath) {
  let bytes;
  try {
    bytes = fs.readFileSync(filePath);
  } catch (err) {
    return { text: undefined, readError: (err && err.message) || String(err), decodeError: null };
  }
  if (hasUtf16Bom(bytes)) return { text: decodeUtf16(bytes), readError: null, decodeError: null };
  const bad = invalidUtf8Offset(bytes);
  const decodeError = bad === -1 ? null : `byte ${bad} is not valid UTF-8; undecodable bytes were read as U+FFFD`;
  return { text: UTF8.decode(bytes), readError: null, decodeError };
}

// Only the validity check of readSourceText, for files TypeScript reads itself.
function utf8DecodeError(filePath) {
  let bytes;
  try {
    bytes = fs.readFileSync(filePath);
  } catch {
    return null; // the read failure is reported where TypeScript reads the file
  }
  if (hasUtf16Bom(bytes)) return null;
  const bad = invalidUtf8Offset(bytes);
  return bad === -1 ? null : `byte ${bad} is not valid UTF-8; undecodable bytes were read as U+FFFD`;
}

// At most `n` code points, cut without an ellipsis.
function truncate(s, n) {
  if (s.length <= n) return s;
  const points = Array.from(s);
  return points.length <= n ? s : points.slice(0, n).join("");
}

module.exports = {
  ProtocolError,
  loadTypeScript,
  isTransparent,
  unwrap,
  lineStarts,
  lineIndex,
  lineColumn,
  lineText,
  walkNodes,
  readSourceText,
  utf8DecodeError,
  scriptKindFor,
  wellFormed,
  readmeFunctionName,
  callerName,
  isComponentName,
  containsJsxOutsideNestedFunctions,
  truncate,
};
