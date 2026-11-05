"use strict";
// The TypeScript 7 API, behind the small TypeScript-6-shaped surface the
// extractors use: `ts.isX` guards, `ts.forEachChild`, `ts.SyntaxKind`, and for
// `routes` mode a program, a checker and module resolution.
//
// TypeScript 7 is a native compiler; its JavaScript API is a client that talks
// to it over a pipe, and only the 7.1 line exports it at stable paths
// (`typescript/sync`, `typescript/ast`, ...). The packages are ES modules, so
// they are loaded with import() and everything after loading is synchronous.

const fs = require("fs");
const path = require("path");
const { createRequire } = require("module");
const { pathToFileURL } = require("url");

class LoadError extends Error {}

// A function-like node, as TypeScript 6's `ts.isFunctionLike` defined it:
// declarations with or without a body, signatures and function types.
const FUNCTION_LIKE_KINDS = [
  "FunctionDeclaration",
  "MethodDeclaration",
  "Constructor",
  "GetAccessor",
  "SetAccessor",
  "FunctionExpression",
  "ArrowFunction",
  "MethodSignature",
  "CallSignature",
  "ConstructSignature",
  "IndexSignature",
  "FunctionType",
  "ConstructorType",
  "JSDocSignature",
];

function readPackage(dir) {
  const pkgPath = path.join(dir, "package.json");
  let pkg;
  try {
    pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"));
  } catch (err) {
    throw new LoadError(`${pkgPath}: could not read package.json (${err && err.message})`);
  }
  if (!pkg || pkg.name !== "typescript") {
    throw new LoadError(`${pkgPath}: package name is ${JSON.stringify(pkg && pkg.name)}, expected "typescript"`);
  }
  return { pkgPath, version: String(pkg.version || "") };
}

// Loads the API from the typescript package in `dir` and returns the `ts`
// facade. Rejects a package that has no stable API with a message naming the fix.
async function loadTypeScript(dir) {
  const resolved = path.resolve(dir);
  const { pkgPath, version } = readPackage(resolved);
  const major = Number.parseInt(version, 10);
  if (!(major >= 7)) {
    throw new LoadError(
      `${pkgPath}: typescript ${version || "(no version)"} has no compiler API the helper can use; ` +
        "it needs typescript 7.1 or newer",
    );
  }
  const requireFrom = createRequire(pkgPath);
  const specifiers = ["typescript/sync", "typescript/ast", "typescript/ast/is"];
  const modules = [];
  for (const specifier of specifiers) {
    let file;
    try {
      file = requireFrom.resolve(specifier);
    } catch (err) {
      const reason = String((err && err.message) || err).split("\n")[0];
      throw new LoadError(
        `${resolved}: typescript ${version} does not export ${specifier} (${reason}); it needs typescript 7.1 or newer`,
      );
    }
    try {
      modules.push(await import(pathToFileURL(file).href));
    } catch (err) {
      const reason = String((err && err.message) || err).split("\n")[0];
      throw new LoadError(`${resolved}: could not load ${specifier} (${reason})`);
    }
  }
  const [sync, ast, is] = modules;
  if (typeof sync.API !== "function") throw new LoadError(`${resolved}: typescript/sync does not export API`);
  return makeFacade(version, sync, ast, is);
}

// The body of a function, method or namespace, or undefined when it has none.
//
// The 7.1 beta's `body` accessor is wrong on a node that has a reparsed JSDoc
// type child (a JavaScript function under `@type {T}`): it answers that type
// node, or the `=>` token of an arrow, because the child mask the accessor
// indexes by leaves the type out. A body is the last child and ends where its
// owner ends, so a body that does not is looked up among the children instead.
function bodyOf(ts, node) {
  const body = node.body;
  if (!body) return undefined;
  if (body.end === node.end && (body.flags & ts.NodeFlags.Reparsed) === 0) return body;
  let found;
  node.forEachChild((child) => {
    if ((child.flags & ts.NodeFlags.Reparsed) === 0) found = child;
  });
  return found;
}

function makeFacade(version, sync, ast, is) {
  const { SyntaxKind } = ast;
  const functionLike = new Set(FUNCTION_LIKE_KINDS.map((name) => {
    if (SyntaxKind[name] === undefined) throw new LoadError(`typescript ${version}: SyntaxKind.${name} is missing`);
    return SyntaxKind[name];
  }));
  const facade = {
    version,
    API: sync.API,
    SyntaxKind,
    NodeFlags: ast.NodeFlags,
    ScriptKind: ast.ScriptKind,
    SymbolFlags: sync.SymbolFlags,
    TypeFlags: sync.TypeFlags,
    ...is,
    // TypeScript 6 names for guards TypeScript 7 spells differently.
    isParameter: is.isParameterDeclaration,
    isGetAccessor: is.isGetAccessorDeclaration,
    isSetAccessor: is.isSetAccessorDeclaration,
    isTypeAssertionExpression: is.isTypeAssertion,
    isFunctionLike: (node) => functionLike.has(node.kind),
    isClassLike: (node) => node.kind === SyntaxKind.ClassDeclaration || node.kind === SyntaxKind.ClassExpression,
    isStringLiteralLike: (node) =>
      node.kind === SyntaxKind.StringLiteral || node.kind === SyntaxKind.NoSubstitutionTemplateLiteral,
    forEachChild: (node, visit) => node.forEachChild(visit),
  };
  facade.bodyOf = (node) => bodyOf(facade, node);
  return facade;
}

// ── Compiler (routes mode) ───────────────────────────────────────────────

const NO_INPUTS_FOUND = 18003; // include globs matched nothing
const EMPTY_FILES_LIST = 18002; // the `files: []` this module writes into the config

function hasNodeModulesSegment(fileName) {
  return fileName.split(/[\\/]/).includes("node_modules");
}

// A program over `files` in which an import resolves only to a file under
// `frontendRoot` (outside node_modules), through TypeScript's own resolution
// and the config's paths. Nothing else resolves, so results never depend on
// what is installed.
//
// The module resolver is a static table, not a callback: a callback that asks
// TypeScript to resolve (a nested API request) hangs the 7.1 beta
// intermittently. So each file's imports are resolved here, before the program
// that uses them is built, and files the table adds are scanned in turn until
// no new file appears.
class Compiler {
  constructor(ts, options, frontendRoot, files) {
    this.ts = ts;
    this.api = new ts.API({ cwd: frontendRoot });
    this.options = options;
    this.files = files;
    this.canonical = (f) => this.api.getCanonicalFileName(path.resolve(f));
    try {
      // Unrestricted: resolves what the program leaves unresolved. Creating it is
      // also the first request, which the file-name methods need to have been made.
      this.resolver = this.api.createModuleResolver(options);
      this.rootPrefix = this.canonical(frontendRoot) + path.sep;
    } catch (err) {
      // An open API keeps the helper process alive until the bridge times out.
      this.close();
      throw err;
    }
    this.program = null;
    this.project = null;
    this.checker = null;
  }

  isInternal(fileName) {
    return this.canonical(fileName).startsWith(this.rootPrefix) && !hasNodeModulesSegment(fileName);
  }

  // Builds the program, and returns it with its checker adapter.
  build() {
    const entries = new Map();
    const scanned = new Set();
    let program = this.createProgram(entries);
    for (;;) {
      let grew = false;
      for (const name of program.getSourceFileNames()) {
        const key = this.canonical(name);
        if (scanned.has(key)) continue;
        scanned.add(key);
        const sf = program.getSourceFile(name);
        if (sf && this.addEntries(sf, entries)) grew = true;
      }
      if (!grew) break;
      program.dispose();
      program = this.createProgram(entries);
    }
    this.program = program;
    this.project = program.getProject();
    this.checker = new CheckerView(this.ts, this.project.checker);
    return this;
  }

  createProgram(entries) {
    const moduleResolver = this.api.createModuleResolver(this.options, {
      moduleResolutions: { fallback: "unresolved", entries: [...entries.values()] },
    });
    return this.api.createProgram(
      this.files.map((f) => f.path),
      this.options,
      { moduleResolver },
    );
  }

  // Adds the resolutions of `sf`'s imports; true when one is new.
  addEntries(sf, entries) {
    const dir = path.dirname(sf.fileName);
    let added = false;
    for (const literal of [...sf.imports, ...sf.moduleAugmentations]) {
      const name = literal.text;
      const key = `${dir}\0${name}`;
      if (entries.has(key)) continue;
      const resolved = this.resolver.resolveModuleName(name, dir).resolvedModule;
      if (!resolved || !this.isInternal(resolved.resolvedFileName)) continue;
      entries.set(key, { moduleName: name, containingDirectory: dir, result: { resolvedFileName: resolved.resolvedFileName } });
      added = true;
    }
    return added;
  }

  getSourceFile(fileName) {
    return this.program.getSourceFile(fileName);
  }

  getSyntacticDiagnostics(sf) {
    return this.program.getSyntacticDiagnostics(sf.fileName);
  }

  // The file `specifier` resolves to from `containingFile`, for a specifier the
  // checker did not resolve (`require()` in a .ts file). Null when none.
  resolveFileName(specifier, containingFile) {
    const resolved = this.resolver.resolveModuleName(specifier, path.dirname(containingFile)).resolvedModule;
    return resolved ? resolved.resolvedFileName : null;
  }

  close() {
    try {
      this.api.close();
    } catch {
      // the server may already be gone
    }
  }
}

// Compiler options for the hermetic program: the config's own, minus what
// would reach outside the request's files. Config problems are added to
// `failures`.
function programOptions(ts, api, tsconfig, failures) {
  let options = {};
  if (tsconfig) {
    const read = api.readConfigFile(tsconfig);
    if (read.error) {
      failures.push({ file: tsconfig, step: "read tsconfig", message: `${tsconfig}: ${read.error.text}` });
    } else {
      // The request carries the file list, so the config's own globs are not
      // expanded: that would only cost a second directory walk.
      const config = read.config && typeof read.config === "object" ? read.config : {};
      const parsed = api.parseJsonConfigFileContent({ ...config, include: [], files: [] }, { configFileName: tsconfig });
      for (const err of parsed.errors) {
        if (err.code === NO_INPUTS_FOUND || err.code === EMPTY_FILES_LIST) continue;
        failures.push({ file: tsconfig, step: "read tsconfig", message: `${tsconfig}: ${err.text}` });
      }
      options = parsed.options;
    }
  }
  return {
    ...options,
    noLib: true,
    lib: undefined,
    types: [],
    typeRoots: [],
    noEmit: true,
    skipLibCheck: true,
    allowJs: true,
    checkJs: false,
    incremental: false,
    composite: false,
    tsBuildInfoFile: undefined,
    declaration: false,
    plugins: undefined,
  };
}

function openCompiler(ts, req, failures) {
  // The config is read with a throwaway API: the options it yields go into the
  // one the compiler owns.
  const reader = new ts.API({ cwd: req.frontendRoot });
  let options;
  try {
    options = programOptions(ts, reader, req.tsconfig, failures);
  } finally {
    reader.close();
  }
  const compiler = new Compiler(ts, options, req.frontendRoot, req.files);
  try {
    return compiler.build();
  } catch (err) {
    compiler.close();
    throw err;
  }
}

// ── Checker ──────────────────────────────────────────────────────────────

// A symbol as the extractors read it: `name`, `flags` and `declarations` as
// nodes (the API hands out handles that are resolved on first use).
class SymbolView {
  constructor(symbol) {
    this.symbol = symbol;
    this.name = symbol.name;
    this.flags = symbol.flags;
    this.resolved = null;
  }

  get declarations() {
    if (this.resolved === null) this.resolved = this.symbol.declarations.map((handle) => handle.resolve());
    return this.resolved;
  }
}

class CheckerView {
  constructor(ts, checker) {
    this.ts = ts;
    this.checker = checker;
    this.views = new WeakMap();
  }

  view(symbol) {
    if (!symbol) return undefined;
    let view = this.views.get(symbol);
    if (view === undefined) {
      view = new SymbolView(symbol);
      this.views.set(symbol, view);
    }
    return view;
  }

  getSymbolAtLocation(node) {
    return this.view(this.checker.getSymbolAtLocation(node));
  }

  getShorthandAssignmentValueSymbol(node) {
    return this.view(this.checker.getShorthandAssignmentValueSymbol(node));
  }

  // The target of an alias; the checker's unknown symbol has no declarations.
  getAliasedSymbol(view) {
    return this.view(this.checker.getAliasedSymbol(view.symbol));
  }

  getExportsOfModule(view) {
    return this.checker.getExportsOfModule(view.symbol).map((symbol) => this.view(symbol));
  }

  // `{ flags, getProperty(name) }` for the type at `node`.
  getTypeAtLocation(node) {
    const type = this.checker.getTypeAtLocation(node);
    return { flags: type.flags, getProperty: (name) => this.view(type.getProperty(name)) };
  }
}

// ── Standalone parsing (facts mode) ──────────────────────────────────────

// The transpiler cannot produce output for a declaration file (`a.d.ts`,
// `a.d.mts`, `a.d.json.ts`, even `a.d.d.ts`) and fails, but reports the same
// syntax errors under any other name, so one is given in place of the real one.
const DECLARATION_FILE_RE = /\.d(\.[^./\\]+)?\.([cm]?ts)$/i;

function transpileName(fileName) {
  const match = DECLARATION_FILE_RE.exec(fileName);
  return match ? `${path.dirname(fileName)}${path.sep}code-intel-transpile.${match[2]}` : fileName;
}

// "... can only be used in TypeScript files" (8000-8999), which a JavaScript
// file with type annotations draws. TypeScript 6 raised these in a program's
// syntactic diagnostics, not as parse errors, and facts mode counts parse errors.
const JS_GRAMMAR_CODES = (code) => code >= 8000 && code < 9000;

// Parses `text` as `fileName` with no program. `syntaxDiagnostics` are the
// parser's own errors; a standalone source file does not carry them, so they
// come from a transpile of the same text, which reports only syntax errors.
function parseStandalone(ts, api, fileName, text, scriptKind) {
  const retained = api.createSourceFile(fileName, text, { scriptKind });
  let diagnostics;
  try {
    const result = api.transpileModule(text, { fileName: transpileName(fileName), reportDiagnostics: true });
    diagnostics = (result.diagnostics || []).filter((d) => !JS_GRAMMAR_CODES(d.code));
  } catch (err) {
    retained.dispose();
    throw err;
  }
  return { sf: retained.sourceFile, syntaxDiagnostics: diagnostics, dispose: () => retained.dispose() };
}

module.exports = {
  LoadError,
  loadTypeScript,
  openCompiler,
  parseStandalone,
};
