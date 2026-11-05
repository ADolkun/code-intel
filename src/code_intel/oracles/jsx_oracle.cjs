"use strict";
// JSX ground truth straight from the TypeScript AST, independent of ts/*.cjs.
//
//   node jsx_oracle.cjs <typescript_dir> <repo_root> <frontend_root> <scan_root> <out.json> [file-list.json]
//
// Emits every JsxOpeningElement and JsxSelfClosingElement under scan_root as
// {file, name, line, col, kind, parent, is_test}. `line` breaks only at \r\n, \r
// and \n (not U+2028/U+2029, which editors and rg do not count); `col` counts
// code points. `parent` applies the README rule by walking up from each element,
// a formulation of its own: the nearest enclosing function that holds the
// element in its parameters or body and has a name. Every TS/JS suffix is
// parsed, so a `.ts` file holding an element would show up here even though the
// tool skips `.ts`. compare_facts.py diffs this against `code-intel dump jsx --all --json`.
// `frontend_root` is relative to repo_root or absolute.

const fs = require("fs");
const path = require("path");

const [tsDir, repoRoot, frontendArg, scanRoot, outPath, listPath] = process.argv.slice(2);
if (!tsDir || !repoRoot || !frontendArg || !scanRoot || !outPath) {
  process.stderr.write(
    "usage: node jsx_oracle.cjs <typescript_dir> <repo_root> <frontend_root> <scan_root> <out.json> [file-list.json]\n",
  );
  process.exit(2);
}

// The TypeScript 7 API, as far as this oracle needs it, loaded from the package
// in `dir`. Written here, not shared with ts/tsapi.cjs, so the two cannot
// agree by sharing a mistake.
async function loadTypeScript(dir) {
  const { createRequire } = require("module");
  const { pathToFileURL } = require("url");
  const requireFrom = createRequire(path.join(path.resolve(dir), "package.json"));
  const load = (specifier) => import(pathToFileURL(requireFrom.resolve(specifier)).href);
  const [{ API }, ast] = await Promise.all([load("typescript/sync"), load("typescript/ast")]);
  const K = ast.SyntaxKind;
  const functionLike = new Set(
    [
      "FunctionDeclaration", "MethodDeclaration", "Constructor", "GetAccessor", "SetAccessor", "FunctionExpression",
      "ArrowFunction", "MethodSignature", "CallSignature", "ConstructSignature", "IndexSignature", "FunctionType",
      "ConstructorType",
    ].map((name) => K[name]),
  );
  const api = new API({ cwd: process.cwd() });
  return {
    version: requireFrom("typescript/package.json").version,
    SyntaxKind: K,
    ScriptKind: ast.ScriptKind,
    isFunctionLike: (n) => functionLike.has(n.kind),
    isJsxOpeningElement: (n) => n.kind === K.JsxOpeningElement,
    isJsxSelfClosingElement: (n) => n.kind === K.JsxSelfClosingElement,
    forEachChild: (n, visit) => n.forEachChild(visit),
    // The 7.1 beta's `body` accessor answers the wrong child on a node with a reparsed JSDoc type; the body is the
    // last child that is not reparsed.
    bodyOf: (n) => {
      if (!n.body) return undefined;
      let last;
      n.forEachChild((c) => {
        if ((c.flags & ast.NodeFlags.Reparsed) === 0) last = c;
      });
      return last;
    },
    // Text as the compiler reads a file: UTF-16 after a byte order mark (an odd
    // trailing byte dropped), otherwise UTF-8 without its mark.
    readFile: (f) => {
      const bytes = fs.readFileSync(f);
      const big = bytes[0] === 0xfe && bytes[1] === 0xff;
      if (!big && !(bytes[0] === 0xff && bytes[1] === 0xfe)) return bytes.toString("utf8").replace(/^\uFEFF/, "");
      const body = Buffer.from(bytes.subarray(2, bytes.length - ((bytes.length - 2) % 2)));
      return (big ? body.swap16() : body).toString("utf16le");
    },
    // [source file, number of syntax errors]
    parse: (file, text, scriptKind) => {
      const sf = api.createSourceFile(file, text, { scriptKind }).sourceFile;
      // The transpiler fails on a declaration file but reports the same syntax errors under another name.
      const decl = /\.d(\.[^./\\]+)?\.([cm]?ts)$/i.exec(file);
      const asScript = decl ? `${path.dirname(file)}${path.sep}oracle-transpile.${decl[2]}` : file;
      const diagnostics = api.transpileModule(text, { fileName: asScript, reportDiagnostics: true }).diagnostics || [];
      return [sf, diagnostics.length];
    },
    close: () => api.close(),
  };
}

async function main() {
  const ts = await loadTypeScript(tsDir);
  const SKIP = new Set([
    "node_modules",
    "__pycache__",
    ".next",
    "dist",
    ".git",
    ".venv",
    "venv",
  ]);
  // Generated output, skipped only as direct children of the frontend root.
  const GENERATED = new Set(["coverage", "out", "build", "playwright-report", "test-results", "storybook-static"]);
  const EXT = [".tsx", ".jsx", ".ts", ".js", ".mjs", ".cjs", ".mts", ".cts"];
  const TEST_DIRS = new Set(["__tests__", "__mocks__", "test", "tests", "e2e"]);

  function realOrNull(p) {
    try {
      return fs.realpathSync(p);
    } catch {
      return null;
    }
  }
  const frontendRoot = realOrNull(path.resolve(repoRoot, frontendArg));

  function walk(dir, acc) {
    const atFrontendRoot = fs.realpathSync(dir) === frontendRoot;
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) {
        const generated = atFrontendRoot && GENERATED.has(e.name);
        if (!SKIP.has(e.name) && !e.name.startsWith(".") && !generated) walk(p, acc);
      } else if (e.isFile() && EXT.some((x) => e.name.endsWith(x))) {
        acc.push(p);
      }
    }
    return acc;
  }

  function isTest(rel) {
    const parts = rel.split("/");
    if (parts.slice(0, -1).some((p) => TEST_DIRS.has(p))) return true;
    return /\.(test|spec)\.[cm]?[jt]sx?$/.test(parts[parts.length - 1]);
  }

  const kindOf = (f) =>
    f.endsWith(".tsx")
      ? ts.ScriptKind.TSX
      : f.endsWith(".ts") || f.endsWith(".mts") || f.endsWith(".cts")
        ? ts.ScriptKind.TS
        : f.endsWith(".jsx")
          ? ts.ScriptKind.JSX
          : ts.ScriptKind.JS;
  // Expression wrappers that leave the value unchanged.
  const WRAPPER_KINDS = new Set([
    ts.SyntaxKind.ParenthesizedExpression,
    ts.SyntaxKind.AsExpression,
    ts.SyntaxKind.SatisfiesExpression,
    ts.SyntaxKind.NonNullExpression,
    ts.SyntaxKind.TypeAssertionExpression,
  ]);

  // The name the README gives a function node, or null. A function declaration
  // or function expression with its own name is named by it. An arrow or
  // anonymous function expression is named by the variable it initializes, seen
  // through value wrappers and through any calls it is an argument of
  // (`const X = memo(forwardRef((p, r) => ...))`, `const X = withA(B)(() => ...)`).
  function readmeParentName(fn) {
    const k = fn.kind;
    const named = k === ts.SyntaxKind.FunctionDeclaration || k === ts.SyntaxKind.FunctionExpression;
    if (named && fn.name) return fn.name.text;
    if (k !== ts.SyntaxKind.ArrowFunction && k !== ts.SyntaxKind.FunctionExpression) return null;
    return initializedVariableName(fn);
  }

  // The variable a value initializes, through wrappers and call arguments; null when none.
  function initializedVariableName(fn) {
    let value = fn;
    for (;;) {
      const up = value.parent;
      if (!up) return null;
      if (WRAPPER_KINDS.has(up.kind)) {
        value = up;
      } else if (up.kind === ts.SyntaxKind.CallExpression && up.expression !== value) {
        value = up; // an argument (the callee is not an argument)
      } else if (up.kind === ts.SyntaxKind.VariableDeclaration) {
        return up.initializer === value && up.name.kind === ts.SyntaxKind.Identifier ? up.name.text : null;
      } else {
        return null;
      }
    }
  }

  // Whether `child`, a direct child of `fn`, is one of its parameters or its body.
  function inParametersOrBody(fn, child) {
    return child === ts.bodyOf(fn) || (fn.parameters && fn.parameters.includes(child));
  }

  function parentOf(element) {
    let child = element;
    for (let up = element.parent; up; child = up, up = up.parent) {
      if (!ts.isFunctionLike(up) || !inParametersOrBody(up, child)) continue;
      const name = readmeParentName(up);
      if (name !== null) return name;
    }
    return null;
  }

  // Offsets where each line starts, breaking only at \r\n, \r and \n.
  function lineOffsets(text) {
    const offsets = [0];
    const re = /\r\n|\r|\n/g;
    for (let m = re.exec(text); m; m = re.exec(text)) offsets.push(m.index + m[0].length);
    return offsets;
  }

  // 1-based [line, column in code points] of `pos`.
  function position(text, offsets, pos) {
    let line = 0;
    while (line + 1 < offsets.length && offsets[line + 1] <= pos) line += 1;
    return [line + 1, Array.from(text.slice(offsets[line], pos)).length + 1];
  }

  const files = listPath ? JSON.parse(fs.readFileSync(listPath, "utf8")) : walk(scanRoot, []).sort();
  const out = [];
  const rels = [];
  for (const abs of files) {
    const src = ts.readFile(abs);
    const rel = path.relative(repoRoot, abs).split(path.sep).join("/");
    rels.push(rel);
    const [sf] = ts.parse(abs, src, kindOf(abs));
    const test = isTest(rel);
    const offsets = lineOffsets(src);
    // Iterative, so nesting the parser accepts never overflows the oracle.
    const stack = [sf];
    while (stack.length) {
      const n = stack.pop();
      if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) {
        const [line, col] = position(src, offsets, n.getStart(sf));
        out.push({
          file: rel,
          name: n.tagName.getText(sf).replace(/\s+/g, ""),
          line,
          col,
          kind: ts.isJsxSelfClosingElement(n) ? "self_close" : "open",
          parent: parentOf(n),
          is_test: test,
        });
      }
      const kids = [];
      ts.forEachChild(n, (c) => {
        kids.push(c);
      });
      for (let i = kids.length - 1; i >= 0; i -= 1) stack.push(kids[i]);
    }
  }
  fs.writeFileSync(outPath, JSON.stringify({ typescript_version: ts.version, files: rels, elements: out }));
  process.stdout.write(`jsx_oracle: ${files.length} files, ${out.length} elements\n`);
  ts.close();
}

main().catch((err) => {
  process.stderr.write(`jsx_oracle: ${(err && err.stack) || err}\n`);
  process.exit(1);
});
