"use strict";
// TypeScript declaration ground truth, written independently of ts/decl_facts.cjs.
//
//   node decl_oracle.cjs <typescript_dir> <frontend_root> <out.json> [repo_root]
//
// Applies the declaration rules of the symbol index (README "Symbols") with its
// own tree walk, so compare_facts.py can diff the helper's `decls` against it.
// Rows: {file, name, kind, line, end_line, column, scope, container, exported,
// is_test, form}. Lines break only at \r\n, \r and \n, as editors and rg count
// them. `form` names the syntactic source (function_decl, var,
// destructure, object_property, class_method, ...) for count breakdowns and is
// not compared. `repo_root` defaults to the parent of frontend_root.

const fs = require("fs");
const path = require("path");

const [tsDir, root, outPath, repoArg] = process.argv.slice(2);
if (!tsDir || !root || !outPath) {
  process.stderr.write("usage: node decl_oracle.cjs <typescript_dir> <frontend_root> <out.json> [repo_root]\n");
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
    NodeFlags: ast.NodeFlags,
    isFunctionLike: (n) => functionLike.has(n.kind),
    isJsxOpeningElement: (n) => n.kind === K.JsxOpeningElement,
    isJsxSelfClosingElement: (n) => n.kind === K.JsxSelfClosingElement,
    forEachChild: (n, visit) => n.forEachChild(visit),
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
  const repoRoot = repoArg ? path.resolve(repoArg) : path.dirname(path.resolve(root));

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
  const EXT = /\.(ts|tsx|js|jsx|mjs|cjs|mts|cts)$/;
  const TEST_DIRS = ["__tests__", "__mocks__", "test", "tests", "e2e"];
  const WRAPPERS = ["memo", "forwardRef", "lazy", "dynamic"];
  const K = ts.SyntaxKind;

  // `top` is the frontend root itself, where generated output lives.
  function walk(d, acc, top = true) {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      if (e.isDirectory()) {
        if (!SKIP.has(e.name) && e.name[0] !== "." && !(top && GENERATED.has(e.name))) walk(path.join(d, e.name), acc, false);
      } else if (e.isFile() && EXT.test(e.name)) {
        acc.push(path.join(d, e.name));
      }
    }
    return acc;
  }

  function scriptKind(f) {
    if (/\.tsx$/.test(f)) return ts.ScriptKind.TSX;
    if (/\.(ts|mts|cts)$/.test(f)) return ts.ScriptKind.TS;
    if (/\.jsx$/.test(f)) return ts.ScriptKind.JSX;
    return ts.ScriptKind.JS;
  }

  function testPath(rel) {
    const p = rel.split("/");
    return p.slice(0, -1).some((x) => TEST_DIRS.includes(x)) || /\.(test|spec)\.[cm]?[jt]sx?$/.test(p[p.length - 1]);
  }

  function strip(e) {
    while (
      e &&
      (e.kind === K.ParenthesizedExpression ||
        e.kind === K.AsExpression ||
        e.kind === K.SatisfiesExpression ||
        e.kind === K.NonNullExpression ||
        e.kind === K.TypeAssertionExpression)
    ) {
      e = e.expression;
    }
    return e;
  }

  const pascal = (n) => /^[A-Z]/.test(n) && /[a-z]/.test(n);
  const isFnValue = (e) => !!e && (e.kind === K.ArrowFunction || e.kind === K.FunctionExpression);

  // JSX reachable from the body without entering another function-like node.
  function bodyHasJsx(fn) {
    const body = bodyOf(fn);
    if (!body || ts.isFunctionLike(body)) return false;
    const stack = [body];
    while (stack.length) {
      const n = stack.pop();
      if (n.kind === K.JsxElement || n.kind === K.JsxSelfClosingElement || n.kind === K.JsxFragment) return true;
      if (n !== body && ts.isFunctionLike(n)) continue;
      ts.forEachChild(n, (c) => {
        stack.push(c);
      });
    }
    return false;
  }

  const fnKind = (name, fn) => (pascal(name) && bodyHasJsx(fn) ? "component" : "function");
  // The inner names of a dotted namespace carry the outer one's modifiers in TypeScript 7; `export` applies to the
  // outermost only. TypeScript 7 also turns JSDoc tags in JavaScript files into nodes flagged Reparsed (`@private`
  // a modifier, `@typedef` a type alias); they are comments, not declarations.
  const NODE_REPARSED = ts.NodeFlags.Reparsed;
  const reparsed = (n) => (n.flags & NODE_REPARSED) !== 0;
  // The 7.1 beta's `body` accessor answers the wrong child on a node with a reparsed JSDoc type; the body is the
  // last child that is not reparsed.
  const bodyOf = (n) => {
    if (!n.body) return undefined;
    let last;
    n.forEachChild((c) => {
      if ((c.flags & NODE_REPARSED) === 0) last = c;
    });
    return last;
  };
  const mods = (n) =>
    n.modifiers && !(n.kind === K.ModuleDeclaration && n.parent.kind === K.ModuleDeclaration)
      ? Array.from(n.modifiers).filter((m) => !reparsed(m))
      : [];
  const hasMod = (n, kind) => mods(n).some((m) => m.kind === kind);

  // Ambient: a .d.ts file, or `declare` on the node or on an enclosing statement.
  function ambient(sf, n) {
    if (sf.isDeclarationFile) return true;
    for (let p = n; p; p = p.parent) if (hasMod(p, K.DeclareKeyword)) return true;
    return false;
  }

  // Every enclosing namespace from the outside in, dotted: `namespace A.B {}`
  // and `namespace A { namespace B {} }` both qualify B as `A.B`.
  function nsName(m) {
    const names = [];
    for (let n = m; n && (n.kind === K.ModuleDeclaration || n.kind === K.ModuleBlock); n = n.parent) {
      if (n.kind === K.ModuleDeclaration) names.push(n.name.text);
    }
    return names.reverse().join(".");
  }

  const LOCAL_BOUNDARY = (p) =>
    p.kind === K.Block ||
    p.kind === K.CaseBlock ||
    p.kind === K.ClassStaticBlockDeclaration ||
    p.kind === K.ObjectLiteralExpression ||
    p.kind === K.ClassDeclaration ||
    p.kind === K.ClassExpression ||
    ts.isFunctionLike(p);

  function scopeFrom(start) {
    for (let p = start; p; p = p.parent) {
      if (p.kind === K.SourceFile) return ["module", null];
      if (p.kind === K.ModuleBlock) return ["namespace", nsName(p.parent)];
      if (p.kind === K.ModuleDeclaration) return ["namespace", nsName(p)];
      if (LOCAL_BOUNDARY(p)) return ["local", null];
    }
    return ["module", null];
  }

  function propName(n) {
    if (!n) return null;
    if (n.kind === K.Identifier || n.kind === K.PrivateIdentifier || n.kind === K.StringLiteral) return n.text;
    return null;
  }

  function objectOwner(obj) {
    let child = obj;
    let up = obj.parent;
    while (
      up &&
      [K.ParenthesizedExpression, K.AsExpression, K.SatisfiesExpression, K.NonNullExpression, K.TypeAssertionExpression].includes(
        up.kind,
      )
    ) {
      child = up;
      up = up.parent;
    }
    if (up && up.kind === K.VariableDeclaration && up.initializer === child && up.name.kind === K.Identifier) {
      return up.name.text;
    }
    if (up && up.kind === K.PropertyAssignment && up.initializer === child) return propName(up.name);
    return null;
  }

  // One handler per declaration kind; each adds the rows its node declares.
  function variableRows(n, { sf, add, exportList }) {
    if (n.parent.kind === K.CatchClause) return;
    const list = n.parent;
    const keyword = list.getFirstToken(sf).getText(sf);
    const base = keyword === "const" ? "const" : "variable";
    const holder = list.parent;
    const inFor = holder.kind === K.ForStatement || holder.kind === K.ForOfStatement || holder.kind === K.ForInStatement;
    const [scope, container] = scopeFrom(holder);
    const stmtExport = holder.kind === K.VariableStatement && hasMod(holder, K.ExportKeyword);
    const exported = (name) => stmtExport || (scope === "module" && exportList.has(name));
    if (n.name.kind === K.Identifier) {
      const name = n.name.text;
      const form = inFor ? "for_header" : n.exclamationToken ? "var_definite" : "var";
      add(form, n.name, name, variableKindOf(name, strip(n.initializer), base), n, scope, container, exported(name));
      return;
    }
    for (const id of bindingIds(n.name, [])) {
      add(inFor ? "for_header_destructure" : "destructure", id, id.text, base, n, scope, container, exported(id.text));
    }
  }

  function variableKindOf(name, init, base) {
    if (isFnValue(init)) return fnKind(name, init);
    if (!init || init.kind !== K.CallExpression || !pascal(name)) return base;
    const callee = strip(init.expression);
    const cn = callee.kind === K.Identifier ? callee.text : callee.kind === K.PropertyAccessExpression ? callee.name.text : "";
    return WRAPPERS.includes(cn) ? "component" : base;
  }

  function bindingIds(b, ids) {
    if (b.kind === K.Identifier) ids.push(b);
    else for (const el of b.elements) if (el.kind !== K.OmittedExpression && el.name) bindingIds(el.name, ids);
    return ids;
  }

  function namespaceRows(n, { sf, declared }) {
    if (n.name.kind !== K.Identifier) return;
    const dottedChild = n.parent.kind === K.ModuleDeclaration;
    const keywordBefore = /\b(namespace|module)\s*$/.test(sf.text.slice(n.getStart(sf), n.name.getStart(sf)));
    if (dottedChild || keywordBefore) declared("namespace", n, "namespace");
  }

  const isClassNode = (n) => n.kind === K.ClassDeclaration || n.kind === K.ClassExpression;

  function methodRows(n, { sf, add }) {
    const owner = n.parent;
    const name = propName(n.name);
    if (name === null) return;
    if (isClassNode(owner)) {
      if (bodyOf(n) || ambient(sf, n) || hasMod(n, K.AbstractKeyword)) {
        add("class_method", n.name, name, "method", n, "class", owner.name ? owner.name.text : null, false);
      }
    } else if (owner.kind === K.ObjectLiteralExpression && bodyOf(n)) {
      const kind = n.kind === K.MethodDeclaration ? fnKind(name, n) : "function";
      add("object_method", n.name, name, kind, n, "object", objectOwner(owner), false);
    }
  }

  function classFieldRows(n, { add }) {
    const owner = n.parent;
    const name = propName(n.name);
    if (name !== null && isClassNode(owner) && isFnValue(strip(n.initializer))) {
      add("class_arrow_field", n.name, name, "method", n, "class", owner.name ? owner.name.text : null, false);
    }
  }

  function objectPropertyRows(n, { add }) {
    const name = n.name.kind === K.Identifier || n.name.kind === K.StringLiteral ? n.name.text : null;
    const value = strip(n.initializer);
    if (name !== null && isFnValue(value)) {
      add("object_property", n.name, name, fnKind(name, value), n, "object", objectOwner(n.parent), false);
    }
  }

  const VISITORS = new Map([
    [K.FunctionDeclaration, (n, { sf, declared }) => {
      if (n.name && (bodyOf(n) || ambient(sf, n))) declared("function_decl", n, fnKind(n.name.text, n));
    }],
    [K.VariableDeclaration, variableRows],
    [K.ClassDeclaration, (n, { declared }) => n.name && declared("class", n, "class")],
    // The name of `class X {}` used as a value binds only inside its own body.
    [K.ClassExpression, (n, { add }) => n.name && add("class_expression", n.name, n.name.text, "class", n, "local", null, false)],
    [K.InterfaceDeclaration, (n, { declared }) => declared("interface", n, "interface")],
    [K.TypeAliasDeclaration, (n, { declared }) => declared("type_alias", n, "type_alias")],
    [K.EnumDeclaration, (n, { declared }) => declared("enum", n, "enum")],
    [K.ModuleDeclaration, namespaceRows],
    [K.MethodDeclaration, methodRows],
    [K.GetAccessor, methodRows],
    [K.SetAccessor, methodRows],
    [K.PropertyDeclaration, classFieldRows],
    [K.PropertyAssignment, objectPropertyRows],
  ]);

  const files = walk(path.resolve(root), []).sort();
  const rows = [];
  let filesWithSyntaxErrors = 0;
  for (const f of files) {
    const [sf, syntaxErrors] = ts.parse(f, ts.readFile(f), scriptKind(f));
    if (syntaxErrors > 0) filesWithSyntaxErrors += 1;
    const rel = path.relative(repoRoot, f).split(path.sep).join("/");
    const isTest = testPath(rel);
    const exportList = new Set();
    for (const st of sf.statements) {
      // `export default name;` and `export = name;` export that declaration.
      if (st.kind === K.ExportAssignment) {
        const value = strip(st.expression);
        if (value.kind === K.Identifier) exportList.add(value.text);
      }
      if (st.kind === K.ExportDeclaration && !st.moduleSpecifier && st.exportClause && st.exportClause.kind === K.NamedExports) {
        for (const sp of st.exportClause.elements) exportList.add(sp.propertyName ? sp.propertyName.text : sp.name.text);
      }
    }
    const breaks = [0];
    for (const m of sf.text.matchAll(/\r\n|\r|\n/g)) breaks.push(m.index + m[0].length);
    const lineAt = (offset) => {
      let lo = 0;
      let hi = breaks.length - 1;
      while (lo < hi) {
        const mid = Math.ceil((lo + hi) / 2);
        if (breaks[mid] > offset) hi = mid - 1;
        else lo = mid;
      }
      return lo;
    };
    const pos = (node) => {
      const p = node.getStart(sf);
      const line = lineAt(p);
      return [line + 1, Array.from(sf.text.slice(breaks[line], p)).length + 1];
    };
    const add = (form, nameNode, name, kind, owner, scope, container, exported) => {
      const [line, column] = pos(nameNode);
      rows.push({
        file: rel,
        name,
        kind,
        line,
        end_line: lineAt(owner.end) + 1,
        column,
        scope,
        container,
        exported,
        is_test: isTest,
        form,
      });
    };
    const exportedAt = (stmt, name, scope) => hasMod(stmt, K.ExportKeyword) || (scope === "module" && exportList.has(name));
    const declared = (form, n, kind) => {
      const [scope, container] = scopeFrom(n.parent);
      add(form, n.name, n.name.text, kind, n, scope, container, exportedAt(n, n.name.text, scope));
    };

    const ctx = { sf, add, declared, exportList };
    const visit = (n) => {
      const handler = reparsed(n) ? undefined : VISITORS.get(n.kind);
      if (handler) handler(n, ctx);
      ts.forEachChild(n, visit);
    };
    visit(sf);
  }
  fs.writeFileSync(
    outPath,
    JSON.stringify({ typescript_version: ts.version, files: files.length, files_with_syntax_errors: filesWithSyntaxErrors, decls: rows }),
  );
  process.stdout.write(`decl_oracle: ${files.length} files, ${rows.length} declarations\n`);
  ts.close();
}

main().catch((err) => {
  process.stderr.write(`decl_oracle: ${(err && err.stack) || err}\n`);
  process.exit(1);
});
