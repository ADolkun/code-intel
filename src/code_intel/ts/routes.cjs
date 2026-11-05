"use strict";
// `routes` mode: one hermetic Program and checker over the frontend. Finds every
// call to a configured API client, every call to a URL builder outside a client
// URL, and every string expression naming a WebSocket path, and evaluates each
// URL to path variants. Matching against backend routes happens in Python.

const path = require("path");
const { openCompiler } = require("./tsapi.cjs");
const {
  ProtocolError,
  unwrap,
  lineColumn,
  callerName,
  isComponentName,
  containsJsxOutsideNestedFunctions,
  utf8DecodeError,
  readSourceText,
  walkNodes,
  wellFormed,
  truncate,
} = require("./common.cjs");

const DEFAULT_LIMITS = { max_url_variants: 16, max_eval_depth: 32 };
const RAW_MAX = 300;
const HOOK_NAME_RE = /^use[A-Z0-9]/;
const HTTP_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"]);
const CLIENT_KINDS = ["call", "options", "instance", "global"];
// The members of an `instance` client that make a request: the method is the
// member name, except `request`, which reads it from its config object.
const INSTANCE_VERBS = new Set(["get", "post", "put", "patch", "delete", "head", "options"]);
const INSTANCE_REQUEST = "request";
// A URL another service serves: it has a scheme, or starts with `//`.
const ABSOLUTE_URL_RE = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i;
// Highest first: a variant's origin is the strongest source that shaped it.
const ORIGIN_RANK = ["prop_flow", "builder", "conditional", "const", "literal"];
// Wrappers whose first argument is the component itself.
const COMPONENT_WRAPPERS = new Set(["memo", "forwardRef"]);
// A callee whose name may denote a client but whose declaration the checker
// cannot find: reported, never silently skipped.
const UNRESOLVED = Symbol("unresolved client");
// A computed property key the evaluator cannot read: it may be any name.
const UNKNOWN_KEY = Symbol("unknown key");
const WHITESPACE_RE = /\s/;

// ── Request validation ───────────────────────────────────────────────────

function requireString(request, key, { nullable = false } = {}) {
  const value = request[key];
  if (nullable && (value === null || value === undefined)) return null;
  if (typeof value !== "string" || value === "") {
    throw new ProtocolError(`${key} must be a non-empty string, got ${JSON.stringify(value)}`);
  }
  return value;
}

function optionalIndex(spec, key, where) {
  if (!(key in spec) || spec[key] === null) return null;
  if (!Number.isInteger(spec[key]) || spec[key] < 0) {
    throw new ProtocolError(`${where}.${key} must be a non-negative integer, got ${JSON.stringify(spec[key])}`);
  }
  return spec[key];
}

function validateClients(raw) {
  if (!Array.isArray(raw)) throw new ProtocolError("clients must be an array");
  return raw.map((spec, i) => {
    const where = `clients[${i}]`;
    if (!spec || typeof spec !== "object") throw new ProtocolError(`${where} must be an object`);
    if (!CLIENT_KINDS.includes(spec.kind)) {
      throw new ProtocolError(`${where}.kind must be one of ${CLIENT_KINDS.join(", ")}, got ${JSON.stringify(spec.kind)}`);
    }
    const named = spec.kind === "global" ? ["name"] : ["module", "export"];
    for (const key of [...named, "default_method"]) {
      if (typeof spec[key] !== "string" || spec[key] === "") {
        throw new ProtocolError(`${where}.${key} must be a non-empty string`);
      }
    }
    const basePath = spec.base_path === undefined ? "" : spec.base_path;
    if (typeof basePath !== "string" || (basePath !== "" && !basePath.startsWith("/"))) {
      throw new ProtocolError(`${where}.base_path must be a string starting with "/", got ${JSON.stringify(spec.base_path)}`);
    }
    if (spec.same_origin_only !== undefined && typeof spec.same_origin_only !== "boolean") {
      throw new ProtocolError(`${where}.same_origin_only must be a boolean, got ${JSON.stringify(spec.same_origin_only)}`);
    }
    const out = {
      kind: spec.kind,
      module: spec.kind === "global" ? null : spec.module,
      export: spec.kind === "global" ? null : spec.export,
      label: spec.kind === "global" ? spec.name : spec.export,
      defaultMethod: spec.default_method.toUpperCase(),
      urlArg: optionalIndex(spec, "url_arg", where),
      initArg: optionalIndex(spec, "init_arg", where),
      optionsArg: optionalIndex(spec, "options_arg", where),
      urlProperty: typeof spec.url_property === "string" ? spec.url_property : null,
      basePath: basePath.replace(/\/+$/, ""),
      sameOriginOnly: spec.same_origin_only === true,
    };
    if ((out.kind === "call" || out.kind === "global") && out.urlArg === null) {
      throw new ProtocolError(`${where}: a ${out.kind} client needs url_arg`);
    }
    if (out.kind === "options" && (out.optionsArg === null || out.urlProperty === null)) {
      throw new ProtocolError(`${where}: an options client needs options_arg and url_property`);
    }
    return out;
  });
}

function validateLimits(raw) {
  const limits = { ...DEFAULT_LIMITS };
  if (raw === undefined || raw === null) return limits;
  if (typeof raw !== "object") throw new ProtocolError("limits must be an object");
  for (const key of Object.keys(DEFAULT_LIMITS)) {
    if (key in raw) {
      if (!Number.isInteger(raw[key]) || raw[key] < 1) {
        throw new ProtocolError(`limits.${key} must be a positive integer, got ${JSON.stringify(raw[key])}`);
      }
      limits[key] = raw[key];
    }
  }
  return limits;
}

function validate(request) {
  const files = request.files;
  if (
    !Array.isArray(files) ||
    !files.every((f) => f && typeof f.path === "string" && f.path !== "" && typeof f.is_test === "boolean")
  ) {
    throw new ProtocolError('files must be an array of {"path": non-empty string, "is_test": boolean}');
  }
  const builderModules = request.builder_modules === undefined ? [] : request.builder_modules;
  if (!Array.isArray(builderModules) || !builderModules.every((m) => typeof m === "string" && m !== "")) {
    throw new ProtocolError("builder_modules must be an array of non-empty strings");
  }
  const marker = request.websocket_marker;
  if (marker !== null && marker !== undefined && (typeof marker !== "string" || marker === "")) {
    throw new ProtocolError("websocket_marker must be a non-empty string or null");
  }
  return {
    repoRoot: path.resolve(requireString(request, "repo_root")),
    frontendRoot: path.resolve(requireString(request, "frontend_root")),
    tsconfig: requireString(request, "tsconfig", { nullable: true }),
    files,
    clients: validateClients(request.clients),
    builderModules,
    marker: marker || null,
    limits: validateLimits(request.limits),
  };
}

// ── Program ──────────────────────────────────────────────────────────────

// One line: the compiler process reports a failure with its Go stack.
function errorText(err) {
  const text = err && typeof err.message === "string" ? `${err.name || "Error"}: ${err.message}` : String(err);
  return text.split("\n", 1)[0];
}

// ── Analysis ─────────────────────────────────────────────────────────────

const isWellFormed = (s) => s === wellFormed(s);

function run(ts, request) {
  const req = validate(request);
  const t0 = Date.now();
  const failures = [];
  // The compiler takes requests as JSON, which cannot carry a lone surrogate,
  // so such a path stays out of the program (collectSources reports it).
  const readable = req.files.filter((f) => isWellFormed(f.path));
  const compiler = openCompiler(ts, { ...req, files: readable }, failures);
  try {
    const tProgram = Date.now();
    const analyzer = new RoutesAnalyzer(ts, compiler, req, failures);
    analyzer.analyze();
    const tExtract = Date.now();
    return {
      protocol: 1,
      mode: "routes",
      typescript_version: ts.version,
      calls: analyzer.calls,
      unresolved: analyzer.unresolved,
      limitations: analyzer.limitations,
      missing_client_modules: analyzer.missingClientModules,
      files_failed: failures,
      timings_ms: { program: tProgram - t0, extract: tExtract - tProgram },
    };
  } finally {
    compiler.close();
  }
}

class RoutesAnalyzer {
  constructor(ts, compiler, req, failures) {
    this.ts = ts;
    this.compiler = compiler;
    this.program = compiler;
    this.checker = compiler.checker;
    this.req = req;
    this.canonical = compiler.canonical;
    this.failures = failures;
    this.calls = [];
    this.unresolved = [];
    this.limitations = [];
    this.missingClientModules = [];
    this.clientByDecl = new Map(); // declaration node -> client spec
    this.builderByDecl = new Map(); // declaration node -> exported builder name
    this.clientNames = new Set();
    this.globalClients = new Map(); // global function name -> client spec
    this.builderNames = new Set();
    this.consumed = new Set(); // expression nodes read while evaluating a client or builder URL
    this.handledRefs = new Set(); // client identifiers that are the callee of a read call
    this.callSites = new Map(); // function node -> call and JSX sites, built on demand
    this.sources = []; // [{sf, path, isTest}]
    this.limitationKeys = new Set();
    this.renameEdges = null; // [{from, to, wrapper}], built on first use
    this.failedFiles = new Set(); // files already reported by perFile
  }

  // ── setup ──

  analyze() {
    this.collectSources();
    this.resolveClients();
    this.resolveBuilders();
    this.clientNames = this.aliasNames(this.clientNames);
    this.builderNames = this.aliasNames(this.builderNames);
    this.perFile((src) => this.walkClientCalls(src));
    this.perFile((src) => this.walkClientReferences(src));
    this.perFile((src) => this.walkBuilderCalls(src));
    if (this.req.marker) this.walkWebSockets();
  }

  collectSources() {
    for (const file of this.req.files) {
      const sf = isWellFormed(file.path) ? this.program.getSourceFile(file.path) : undefined;
      if (!sf) {
        const reason = readSourceText(file.path).readError || "file not found";
        this.failures.push({ file: file.path, step: "read file", message: wellFormed(`${file.path}: could not read file (${reason})`) });
        continue;
      }
      if (sf.isDeclarationFile) continue;
      const decodeError = utf8DecodeError(file.path);
      if (decodeError !== null) {
        this.failures.push({ file: file.path, step: "decode file", message: wellFormed(`${file.path}: ${decodeError}`) });
      }
      const syntax = this.program.getSyntacticDiagnostics(sf);
      if (syntax.length > 0) {
        const [line, column] = lineColumn(sf, syntax[0].pos || 0);
        this.failures.push({
          file: file.path,
          step: "parse file",
          message: wellFormed(
            `TypeScript reported ${syntax.length} syntax error${syntax.length === 1 ? "" : "s"} in ${file.path}; ` +
              `first at ${line}:${column}: ${wellFormed(syntax[0].text)}`,
          ),
        });
      }
      this.sources.push({ sf, path: file.path, isTest: file.is_test });
    }
  }

  perFile(fn) {
    for (const src of this.sources) this.guarded(src, () => fn(src));
  }

  // Runs `fn` for one file; an exception is that file's analysis_failed, once per file.
  guarded(src, fn) {
    try {
      return fn();
    } catch (err) {
      if (!this.failedFiles.has(src.path)) {
        this.failedFiles.add(src.path);
        this.failures.push({
          file: src.path,
          step: "extract calls",
          message: wellFormed(`${src.path}: could not extract calls (${errorText(err)})`),
        });
      }
      return undefined;
    }
  }

  moduleSymbol(relModule) {
    const sf = this.program.getSourceFile(path.join(this.req.repoRoot, relModule));
    if (!sf) return { sf: null, symbol: null };
    return { sf, symbol: this.checker.getSymbolAtLocation(sf) || null };
  }

  exportedSymbols(moduleSym) {
    return moduleSym ? this.checker.getExportsOfModule(moduleSym) : [];
  }

  resolveAlias(sym) {
    if (sym && sym.flags & this.ts.SymbolFlags.Alias) {
      try {
        const target = this.checker.getAliasedSymbol(sym);
        if (target && target.declarations && target.declarations.length) return target;
      } catch {
        return null;
      }
      return null;
    }
    return sym;
  }

  declsOf(node) {
    const parent = node.parent;
    // A shorthand `{ url }` names a property; its value is the variable `url`.
    const raw =
      parent && this.ts.isShorthandPropertyAssignment(parent) && parent.name === node
        ? this.checker.getShorthandAssignmentValueSymbol(parent)
        : this.checker.getSymbolAtLocation(node);
    const sym = this.resolveAlias(raw);
    return (sym && sym.declarations) || [];
  }

  resolveClients() {
    for (const spec of this.req.clients) {
      if (spec.kind === "global") this.globalClients.set(spec.label, spec);
      else this.resolveClient(spec);
    }
  }

  // One configured client: its declarations and names, or the missing module
  // (`frontend/src/lib/api.ts`) or export (`frontend/src/lib/api.ts#apiJson`).
  resolveClient(spec) {
    const { sf, symbol } = this.moduleSymbol(spec.module);
    if (!sf) {
      if (!this.missingClientModules.includes(spec.module)) this.missingClientModules.push(spec.module);
      return;
    }
    const target = this.clientSymbol(sf, symbol, spec.export);
    if (!target || !target.declarations) {
      this.missingClientModules.push(`${spec.module}#${spec.export}`);
      return;
    }
    this.clientNames.add(spec.export);
    for (const decl of target.declarations) {
      this.clientByDecl.set(decl, spec);
      if (decl.name && this.ts.isIdentifier(decl.name)) this.clientNames.add(decl.name.text);
    }
  }

  // The module's export of that name, else its top-level declaration of that
  // name: a client may be module-private and still be called inside the module.
  clientSymbol(sf, moduleSym, name) {
    const exp = this.exportedSymbols(moduleSym).find((s) => s.name === name);
    if (exp) return this.resolveAlias(exp);
    const ts = this.ts;
    for (const stmt of sf.statements) {
      let nameNode = null;
      if ((ts.isFunctionDeclaration(stmt) || ts.isClassDeclaration(stmt)) && stmt.name && stmt.name.text === name) nameNode = stmt.name;
      if (ts.isVariableStatement(stmt)) {
        const decl = stmt.declarationList.declarations.find((d) => ts.isIdentifier(d.name) && d.name.text === name);
        if (decl) nameNode = decl.name;
      }
      if (nameNode) return this.checker.getSymbolAtLocation(nameNode) || null;
    }
    return null;
  }

  resolveBuilders() {
    for (const relModule of this.req.builderModules) {
      const { symbol } = this.moduleSymbol(relModule);
      for (const exp of this.exportedSymbols(symbol)) this.resolveBuilder(exp);
    }
  }

  // An export is a URL builder when it is a function with a body (not a
  // client or a builder already seen) whose every return is a path.
  resolveBuilder(exp) {
    const target = this.resolveAlias(exp);
    const decls = (target && target.declarations) || [];
    if (decls.some((d) => this.clientByDecl.has(d) || this.builderByDecl.has(d))) return;
    const decl = decls.find((d) => {
      const fn = this.functionOfDecl(d);
      return fn && this.ts.bodyOf(fn);
    });
    if (!decl || !this.returnsOnlyPaths(this.functionOfDecl(decl))) return;
    for (const d of decls) this.builderByDecl.set(d, exp.name);
    this.builderNames.add(exp.name);
    if (decl.name && this.ts.isIdentifier(decl.name)) this.builderNames.add(decl.name.text);
  }

  // Every return must be a path: a function that sometimes returns a base URL
  // or "" (apiBaseUrl) is not a route builder.
  returnsOnlyPaths(fn) {
    const ctx = this.newContext({ propFlow: false, record: false });
    const variants = this.evalReturns(fn, new Map(), ctx, 0);
    return Boolean(variants) && variants.length > 0 && variants.every((v) => startsWithPath(compress(v.parts)));
  }

  // Names that may refer to a tracked symbol: the originals plus every import,
  // export, destructuring and `const x = y` rename of them. A default export is
  // tracked per module (`default:<file>`), so a default import adds its local
  // name only when that module's default is tracked. `wrapperCalls` also follows
  // `const X = hoc(Y)`. A text prefilter only; the checker decides. Without it
  // every identifier in the program would be resolved.
  aliasNames(base, { wrapperCalls = false } = {}) {
    if (this.renameEdges === null) this.renameEdges = this.collectRenameEdges();
    const names = new Set(base);
    let grew = true;
    while (grew) {
      grew = false;
      for (const edge of this.renameEdges) {
        if (edge.wrapper && !wrapperCalls) continue;
        if (names.has(edge.from) && !names.has(edge.to)) {
          names.add(edge.to);
          grew = true;
        }
      }
    }
    return names;
  }

  collectRenameEdges() {
    const edges = [];
    for (const src of this.sources) {
      this.guarded(src, () =>
        walkNodes(this.ts, src.sf, (node) => {
          this.renameEdgesOf(node, src.sf, edges);
        }),
      );
    }
    return edges;
  }

  renameEdgesOf(node, sf, edges) {
    const ts = this.ts;
    const add = (from, to, wrapper = false) => {
      if (from !== null && to !== null) edges.push({ from, to, wrapper });
    };
    if (ts.isImportSpecifier(node) || ts.isExportSpecifier(node)) {
      this.specifierEdge(node, sf, add);
    } else if (ts.isImportClause(node) && node.name) {
      const module = this.importedModuleFile(node.parent);
      if (module) add(this.defaultKey(module), node.name.text);
    } else if (ts.isExportAssignment(node)) {
      // `export default X`, `export default memo(X)`: the default is a rename of X.
      this.variableEdges(unwrap(ts, node.expression), this.defaultKey(sf.fileName), add);
    } else {
      this.declarationEdges(node, sf, add);
    }
  }

  // Renames made by a declaration: `export default function X`, a
  // destructured `{ a: b }`, or `const x = <expression naming y>`.
  declarationEdges(node, sf, add) {
    const ts = this.ts;
    if ((ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node)) && node.name && this.isDefaultExport(node)) {
      add(node.name.text, this.defaultKey(sf.fileName));
    } else if (ts.isBindingElement(node) && ts.isObjectBindingPattern(node.parent) && ts.isIdentifier(node.name)) {
      add(nameText(ts, node.propertyName || node.name), node.name.text);
    } else if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      this.variableEdges(unwrap(ts, node.initializer), node.name.text, add);
    }
  }

  // The rename-edge key of a module's default export.
  defaultKey(fileName) {
    return `default:${this.canonical(fileName)}`;
  }

  // `export { x as default }` names this module's default; `{ default as x }`
  // reads the default of the module named in the declaration.
  specifierEdge(node, sf, add) {
    const ts = this.ts;
    const to = node.name.text === "default" && ts.isExportSpecifier(node) ? this.defaultKey(sf.fileName) : node.name.text;
    let from = nameText(ts, node.propertyName || node.name);
    if (from === "default") {
      // ImportSpecifier -> NamedImports -> ImportClause -> ImportDeclaration;
      // ExportSpecifier -> NamedExports -> ExportDeclaration.
      const owner = ts.isImportSpecifier(node) ? node.parent.parent.parent : node.parent.parent;
      const module = this.importedModuleFile(owner);
      from = module ? this.defaultKey(module) : null;
    }
    add(from, to);
  }

  variableEdges(init, to, add) {
    const ts = this.ts;
    if (ts.isIdentifier(init)) add(init.text, to);
    else if (ts.isPropertyAccessExpression(init)) add(init.name.text, to);
    else if (ts.isElementAccessExpression(init)) add(nameText(ts, unwrap(ts, init.argumentExpression)), to);
    else if (ts.isCallExpression(init) && init.arguments.length > 0) this.wrapperEdges(init, to, add);
  }

  // `const X = hoc(Y)`, through nested wrappers (`memo(forwardRef(Y))`) and
  // to a named inline function (`forwardRef(function Y(...) {...})`), and
  // `dynamic(() => import("m"))` or `lazy(...)` naming the export they load.
  wrapperEdges(call, to, add) {
    const ts = this.ts;
    const first = unwrap(ts, call.arguments[0]);
    if (ts.isCallExpression(first) && first.arguments.length > 0) this.wrapperEdges(first, to, add);
    else add(inlineName(ts, first) || nameText(ts, first), to, true);
    for (const arg of call.arguments) {
      const loaded = this.lazyLoadTarget(arg);
      if (!loaded || !loaded.moduleFile) continue;
      // An unreadable `.then` pick may load any export of the module.
      const members = loaded.member === null ? loaded.exportNames : [loaded.member];
      for (const member of members) add(member === "default" ? this.defaultKey(loaded.moduleFile) : member, to, true);
    }
  }

  isDefaultExport(node) {
    const mods = node.modifiers || [];
    return (
      mods.some((m) => m.kind === this.ts.SyntaxKind.ExportKeyword) &&
      mods.some((m) => m.kind === this.ts.SyntaxKind.DefaultKeyword)
    );
  }

  // For a loader `() => import("m")` or `() => import("m").then((mod) => mod.X)`:
  // { member: "default" | "X" | null, moduleFile, decls, exportNames }.
  // `member` is null when the `.then` callback picks the export in a way the
  // analysis cannot read; `moduleFile` and `decls` are null and [] when m is
  // outside the program. Null when `arg` is not such a loader.
  lazyLoadTarget(arg) {
    const ts = this.ts;
    const fn = unwrap(ts, arg);
    if (!(ts.isArrowFunction(fn) || ts.isFunctionExpression(fn)) || !ts.bodyOf(fn)) return null;
    const returns = this.returnsOf(fn);
    if (returns.length !== 1) return null;
    let load = unwrap(ts, returns[0]);
    let member = "default";
    const isImport = (e) => ts.isCallExpression(e) && e.expression.kind === ts.SyntaxKind.ImportKeyword;
    if (ts.isCallExpression(load) && ts.isPropertyAccessExpression(load.expression) && load.expression.name.text === "then") {
      const inner = unwrap(ts, load.expression.expression);
      if (!isImport(inner)) return null;
      member = this.thenPickedExport(load.arguments[0]);
      load = inner;
    }
    if (!isImport(load)) return null;
    const moduleSym = this.moduleOf(load, 0);
    const file = moduleSym && moduleSym.declarations[0];
    if (!file) return { member, moduleFile: null, decls: [], exportNames: [] };
    const exported = this.exportedSymbols(moduleSym);
    let decls = [];
    if (member !== null) {
      const exp = exported.find((s) => s.name === member);
      const target = exp && this.resolveAlias(exp);
      decls = (target && target.declarations) || [];
    }
    return { member, moduleFile: file.fileName, decls, exportNames: exported.map((s) => s.name) };
  }

  // `(mod) => mod.X` picks export X; anything else is null.
  thenPickedExport(callback) {
    const ts = this.ts;
    const cb = callback && unwrap(ts, callback);
    if (!cb || !(ts.isArrowFunction(cb) || ts.isFunctionExpression(cb)) || cb.parameters.length !== 1) return null;
    const param = cb.parameters[0].name;
    const returns = ts.bodyOf(cb) ? this.returnsOf(cb) : [];
    if (!ts.isIdentifier(param) || returns.length !== 1) return null;
    const picked = unwrap(ts, returns[0]);
    if (ts.isPropertyAccessExpression(picked) && ts.isIdentifier(picked.expression) && picked.expression.text === param.text) {
      return picked.name.text;
    }
    return null;
  }

  // True when `node`, or the object it is a member of, is imported from a
  // module outside the program (a package): it cannot be a frontend client or
  // component, whose modules are all in the program.
  importsFromOutside(node) {
    const ts = this.ts;
    const base = this.accessRoot(node);
    if (!ts.isIdentifier(base)) return false;
    const sym = this.checker.getSymbolAtLocation(base);
    const decl = sym && sym.flags & ts.SymbolFlags.Alias && sym.declarations && sym.declarations[0];
    let owner = decl || null;
    while (owner && !ts.isImportDeclaration(owner) && !ts.isImportEqualsDeclaration(owner)) owner = owner.parent;
    if (!owner || ts.isImportEqualsDeclaration(owner)) return false;
    return this.importedModuleFile(owner) === null;
  }

  // The object at the root of the member chain `node` is part of: `api` for the
  // `f` of `api.x.f` or `api["f"]`.
  accessRoot(node) {
    const ts = this.ts;
    const isAccess = (n) => n && (ts.isPropertyAccessExpression(n) || ts.isElementAccessExpression(n));
    let base = node;
    for (;;) {
      const parent = base.parent;
      if (isAccess(parent) && parent.expression !== base) base = unwrap(ts, parent.expression);
      else if (isAccess(base)) base = unwrap(ts, base.expression);
      else return base;
    }
  }


  // The file an import or export declaration's module specifier resolves to, or null.
  importedModuleFile(decl) {
    const ts = this.ts;
    if (!decl || !(ts.isImportDeclaration(decl) || ts.isExportDeclaration(decl)) || !decl.moduleSpecifier) return null;
    const sym = this.checker.getSymbolAtLocation(decl.moduleSpecifier);
    const target = sym && sym.declarations && sym.declarations[0];
    return target && ts.isSourceFile(target) ? target.fileName : null;
  }

  // ── shared node helpers ──

  isConstDeclaration(decl) {
    return (
      this.ts.isVariableDeclaration(decl) &&
      this.ts.isVariableDeclarationList(decl.parent) &&
      (decl.parent.flags & this.ts.NodeFlags.Const) !== 0
    );
  }

  functionOfDecl(decl) {
    const ts = this.ts;
    if (!decl) return null;
    if (ts.isFunctionDeclaration(decl) || ts.isMethodDeclaration(decl)) return decl;
    if (ts.isFunctionExpression(decl) || ts.isArrowFunction(decl)) return decl;
    if ((ts.isVariableDeclaration(decl) && this.isConstDeclaration(decl)) || ts.isPropertyAssignment(decl)) {
      const init = decl.initializer && unwrap(ts, decl.initializer);
      if (init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init))) return init;
    }
    return null;
  }

  // The node naming what a callee denotes: `f`, the `f` of `a.f`, or the
  // string literal of `a["f"]`.
  calleeName(callee) {
    const ts = this.ts;
    const e = unwrap(ts, callee);
    if (ts.isIdentifier(e)) return e;
    if (ts.isPropertyAccessExpression(e)) return e.name;
    if (ts.isElementAccessExpression(e)) {
      const arg = unwrap(ts, e.argumentExpression);
      if (ts.isStringLiteral(arg) || ts.isNoSubstitutionTemplateLiteral(arg)) return arg;
    }
    return null;
  }

  // The configured client a callee expression denotes, through imports,
  // namespaces (static, `await import()` or `require()`), destructuring and
  // `const f = client` aliases. UNRESOLVED when the name may denote a client
  // but no declaration can be found; null when it denotes something else.
  clientOf(expr, depth = 0) {
    const ts = this.ts;
    const nameNode = this.calleeName(expr);
    if (!nameNode || depth > 8 || !this.clientNames.has(nameNode.text)) return null;
    const decls = this.declsOf(nameNode);
    const direct = this.clientOfDecls(decls);
    if (direct) return direct;
    const e = unwrap(ts, expr);
    if (ts.isPropertyAccessExpression(e) || ts.isElementAccessExpression(e)) {
      const moduleSym = this.moduleOf(e.expression, depth + 1);
      if (moduleSym) return this.clientExport(moduleSym, nameNode.text);
    }
    const d = decls[0];
    if (!d) return this.importsFromOutside(nameNode) ? null : UNRESOLVED;
    if (ts.isBindingElement(d)) return this.clientOfBinding(d, depth + 1);
    if (this.isConstDeclaration(d) && d.initializer && ts.isIdentifier(d.name)) {
      return this.clientOf(d.initializer, depth + 1);
    }
    return null;
  }

  // The module symbol an expression evaluates to: a namespace import, an
  // `import()` or `require()` of a module in the program (awaited or not), or a
  // const holding one. The checker cannot type `await import()` without lib
  // files, so the call is resolved here.
  moduleOf(expr, depth) {
    const ts = this.ts;
    if (depth > 8) return null;
    let e = unwrap(ts, expr);
    while (ts.isAwaitExpression(e)) e = unwrap(ts, e.expression);
    if (ts.isCallExpression(e)) return this.moduleOfLoad(e);
    return ts.isIdentifier(e) ? this.moduleOfName(e, depth) : null;
  }

  // `import("m")` or `require("m")`, for a module in the program.
  moduleOfLoad(call) {
    const ts = this.ts;
    const callee = call.expression;
    const loads = callee.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(callee) && callee.text === "require");
    const arg = loads && call.arguments.length > 0 ? unwrap(ts, call.arguments[0]) : null;
    if (!arg || !ts.isStringLiteralLike(arg)) return null;
    return this.asModule(this.checker.getSymbolAtLocation(arg)) || this.moduleNamed(arg.text, call.getSourceFile());
  }

  // A namespace import, or a const or `.then` parameter holding a module.
  moduleOfName(name, depth) {
    const ts = this.ts;
    const sym = this.resolveAlias(this.checker.getSymbolAtLocation(name));
    const module = this.asModule(sym);
    if (module) return module;
    const d = sym && sym.declarations && sym.declarations[0];
    if (!d) return null;
    if (this.isConstDeclaration(d) && d.initializer && ts.isIdentifier(d.name)) return this.moduleOf(d.initializer, depth + 1);
    return ts.isParameter(d) ? this.moduleOfThenParameter(d, depth + 1) : null;
  }

  // The module a `.then` callback receives as its first parameter, for
  // `import("m").then((mod) => ...)`; null for any other parameter.
  moduleOfThenParameter(param, depth) {
    const ts = this.ts;
    const fn = param.parent;
    if (!fn || fn.parameters[0] !== param || !(ts.isArrowFunction(fn) || ts.isFunctionExpression(fn))) return null;
    const c = parentThroughTransparent(ts, fn);
    const call = c && c.parent;
    if (!call || !ts.isCallExpression(call) || call.arguments[0] !== c.node) return null;
    const callee = unwrap(ts, call.expression);
    if (!ts.isPropertyAccessExpression(callee) || callee.name.text !== "then") return null;
    return this.moduleOf(callee.expression, depth);
  }

  // A module specifier the checker did not resolve (`require()` in a .ts file
  // is not an import to TypeScript), resolved with the program's own options.
  // Only files in the program count, as for every other import.
  moduleNamed(specifier, sf) {
    const resolved = this.compiler.resolveFileName(specifier, sf.fileName);
    const file = resolved && this.program.getSourceFile(resolved);
    return file && !file.isDeclarationFile ? this.asModule(this.checker.getSymbolAtLocation(file)) : null;
  }

  asModule(sym) {
    const d = sym && sym.declarations && sym.declarations[0];
    return d && this.ts.isSourceFile(d) ? sym : null;
  }

  clientExport(moduleSym, name) {
    return this.clientOfSymbol(this.exportedSymbols(moduleSym).find((s) => s.name === name));
  }

  // The client a symbol (through aliases) declares; null when none.
  clientOfSymbol(sym) {
    const target = sym && this.resolveAlias(sym);
    return this.clientOfDecls((target && target.declarations) || []);
  }

  clientOfDecls(decls) {
    for (const d of decls) {
      const spec = this.clientByDecl.get(d);
      if (spec) return spec;
    }
    return null;
  }

  // `const { apiJson } = api`, `const { apiJson: get } = await import(...)`
  // and `import(...).then(({ apiJson }) => ...)`.
  clientOfBinding(element, depth) {
    const ts = this.ts;
    const pattern = element.parent;
    const holder = pattern && pattern.parent;
    if (!ts.isObjectBindingPattern(pattern) || element.dotDotDotToken || !holder) return null;
    const key = element.propertyName || element.name;
    if (!(ts.isIdentifier(key) || ts.isStringLiteral(key))) return UNRESOLVED;
    if (ts.isParameter(holder)) {
      // A destructured parameter: the module of a `.then` callback, else a
      // value the analysis cannot see, which may be the client.
      const moduleSym = this.moduleOfThenParameter(holder, depth);
      return moduleSym ? this.clientExport(moduleSym, key.text) : UNRESOLVED;
    }
    if (!ts.isVariableDeclaration(holder) || !holder.initializer) return null;
    const moduleSym = this.moduleOf(holder.initializer, depth);
    return moduleSym ? this.clientExport(moduleSym, key.text) : this.clientOfProperty(holder.initializer, key.text);
  }

  // The client held in property `name` of a value, by its type. An unknown
  // type may hold the client.
  clientOfProperty(value, name) {
    const ts = this.ts;
    const type = this.checker.getTypeAtLocation(value);
    const prop = type && type.getProperty(name);
    if (prop) return this.clientOfSymbol(prop);
    return type && type.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown) ? UNRESOLVED : null;
  }

  builderOf(expr) {
    const nameNode = this.calleeName(expr);
    if (!nameNode || !this.builderNames.has(nameNode.text)) return null;
    for (const d of this.declsOf(nameNode)) {
      const name = this.builderByDecl.get(d);
      if (name !== undefined) return { name, decl: d };
    }
    return null;
  }

  insideDeclarations(node, declMap) {
    for (let p = node.parent; p; p = p.parent) if (declMap.has(p)) return true;
    return false;
  }

  enclosingCaller(node, sf) {
    for (let p = node.parent; p; p = p.parent) {
      if (this.ts.isFunctionLike(p) && this.ts.bodyOf(p)) {
        const name = callerName(this.ts, p);
        if (name !== undefined) return { name, defLine: lineColumn(sf, p.getStart(sf))[0] };
      }
    }
    return { name: null, defLine: null };
  }

  location(node, src) {
    const [line, column] = lineColumn(src.sf, node.getStart(src.sf));
    return { file: src.path, line, column };
  }

  // The node's own file: a URL value may live in another module than the call.
  rawText(node) {
    return wellFormed(truncate(node.getText(), RAW_MAX));
  }

  addLimitation(kind, src, node, message) {
    const { line } = this.location(node, src);
    const key = `${kind}\0${src.path}\0${line}\0${message}`;
    if (this.limitationKeys.has(key)) return;
    this.limitationKeys.add(key);
    this.limitations.push({ kind, file: src.path, line, message: wellFormed(message) });
  }

  // Test files are never matched, so their unreadable calls lose nothing.
  addUnresolved(src, node, client, reason) {
    if (src.isTest) return;
    const loc = this.location(node, src);
    this.unresolved.push({ ...loc, client, reason, text: this.rawText(node) });
  }

  // ── client calls ──

  walkClientCalls(src) {
    const ts = this.ts;
    walkNodes(ts, src.sf, (node) => {
      if (!ts.isCallExpression(node)) return;
      const global = this.globalClientOf(node.expression);
      if (global) {
        this.clientCall(node, global, src);
        return;
      }
      const member = this.instanceMemberOf(node.expression);
      const spec = member ? member.spec : this.clientOf(node.expression);
      if (!spec) return;
      const nameNode = member ? member.object : this.calleeName(node.expression);
      this.handledRefs.add(nameNode);
      if (this.insideDeclarations(node, this.clientByDecl)) return;
      if (spec === UNRESOLVED) this.addUnresolved(src, node, nameNode.text, "client_not_resolved");
      else if (spec.kind === "instance") this.instanceCall(node, spec, member ? member.name : INSTANCE_REQUEST, src);
      else this.clientCall(node, spec, src);
    });
  }

  // The configured global function a callee names (`fetch`), unless a local
  // binding or an import shadows it. The hermetic program has no lib files, so
  // a real global has no declaration at all.
  globalClientOf(callee) {
    const id = unwrap(this.ts, callee);
    if (!this.ts.isIdentifier(id)) return null;
    const spec = this.globalClients.get(id.text);
    if (!spec) return null;
    const sym = this.checker.getSymbolAtLocation(id);
    if (!sym) return spec;
    if (sym.flags & this.ts.SymbolFlags.Alias) return null;
    return (sym.declarations || []).every((d) => d.getSourceFile().isDeclarationFile) ? spec : null;
  }

  // For `api.get(...)`, `api["post"](...)`: the instance, the node naming it and
  // the member called. Null when the callee is not a request member of a
  // configured instance; the spec is UNRESOLVED when `api` may be one.
  instanceMemberOf(callee) {
    const ts = this.ts;
    const e = unwrap(ts, callee);
    if (!(ts.isPropertyAccessExpression(e) || ts.isElementAccessExpression(e))) return null;
    const member = this.calleeName(e);
    if (!member || !(INSTANCE_VERBS.has(member.text) || member.text === INSTANCE_REQUEST)) return null;
    const object = this.calleeName(e.expression);
    const spec = object && this.clientOf(e.expression);
    if (!spec || (spec !== UNRESOLVED && spec.kind !== "instance")) return null;
    return { spec, object, name: member.text };
  }

  // How a reference to an instance is used as the object of a member access:
  // "request" (`api.get`), "setup" (`api.interceptors`), or null when it is not
  // the object of one.
  instanceMemberUse(target) {
    const ts = this.ts;
    const c = parentThroughTransparent(ts, target);
    const access = c && c.parent;
    if (!access || !(ts.isPropertyAccessExpression(access) || ts.isElementAccessExpression(access)) || access.expression !== c.node) {
      return null;
    }
    const member = this.calleeName(access);
    return member && (INSTANCE_VERBS.has(member.text) || member.text === INSTANCE_REQUEST) ? "request" : "setup";
  }

  // `call` and `global` clients take the URL at `urlArg` and the method from
  // the init object at `initArg`; `options` clients take both from one object.
  clientCall(call, spec, src) {
    const ts = this.ts;
    const args = call.arguments;
    const needed = spec.kind === "options" ? spec.optionsArg : spec.urlArg;
    const spreadAt = args.findIndex((a) => ts.isSpreadElement(a));
    if (spreadAt !== -1 && spreadAt <= Math.max(needed, spec.initArg === null ? -1 : spec.initArg)) {
      this.addUnresolved(src, call, spec.label, "spread_arguments");
      return;
    }
    let urlExpr;
    let urlEnv = new Map();
    let method = { method: spec.defaultMethod, source: "default" };
    if (spec.kind !== "options") {
      urlExpr = args[spec.urlArg];
      if (!urlExpr) {
        this.addUnresolved(src, call, spec.label, "url_not_static");
        return;
      }
      if (spec.initArg !== null) method = this.methodOf(args[spec.initArg], spec, src, call);
    } else {
      const options = args[spec.optionsArg] && this.objectLiteralOf(args[spec.optionsArg], new Map(), 0);
      const prop = options && this.propertyValue(options.object, spec.urlProperty, options.env);
      if (!options || prop.value === null || prop.ambiguous) {
        this.addUnresolved(src, call, spec.label, "options_not_object");
        return;
      }
      urlExpr = prop.value;
      urlEnv = options.env;
    }
    this.emitClientCall(call, spec, urlExpr, urlEnv, method, src);
  }

  // A call on an `instance` client: `member` is a verb (its URL is the first
  // argument, its method the verb) or `request`, which reads `url` and `method`
  // from its config object. Calling the instance itself reads the same object.
  instanceCall(call, spec, member, src) {
    const ts = this.ts;
    const args = call.arguments;
    if (args.length > 0 && ts.isSpreadElement(args[0])) {
      this.addUnresolved(src, call, spec.label, "spread_arguments");
      return;
    }
    let urlExpr;
    let urlEnv = new Map();
    let method = { method: member.toUpperCase(), source: "member" };
    if (member === INSTANCE_REQUEST) {
      const options = args[0] && this.objectLiteralOf(args[0], new Map(), 0);
      const prop = options && this.propertyValue(options.object, "url", options.env);
      if (!options || prop.value === null || prop.ambiguous) {
        this.addUnresolved(src, call, spec.label, "options_not_object");
        return;
      }
      urlExpr = prop.value;
      urlEnv = options.env;
      method = this.methodOf(args[0], spec, src, call);
    } else {
      urlExpr = args[0];
      if (!urlExpr) {
        this.addUnresolved(src, call, spec.label, "url_not_static");
        return;
      }
    }
    this.emitClientCall(call, spec, urlExpr, urlEnv, method, src);
  }

  emitClientCall(call, spec, urlExpr, urlEnv, method, src) {
    const ctx = this.newContext({ propFlow: true, record: true, src });
    const variants = this.finishVariants(this.evalExpr(urlExpr, urlEnv, ctx, 0), ctx, src, call);
    this.emitCall(src, call, urlExpr, variants, {
      kind: spec.kind,
      client: spec.label,
      builder: null,
      method,
      unresolvedClient: spec.label,
      callee: this.calleeName(call.expression),
      basePath: spec.basePath,
      sameOriginOnly: spec.sameOriginOnly,
    });
  }

  emitCall(src, node, urlExpr, allVariants, info) {
    let variants = info.basePath ? allVariants.map((v) => withBasePath(v, info.basePath)) : allVariants;
    if (info.sameOriginOnly) variants = variants.filter((v) => !isAbsoluteUrl(v.parts));
    const paths = variants.filter((v) => startsWithPath(v.parts));
    // A variant that starts with a value the evaluator cannot read is not
    // static; one that starts with known text other than "/" is not a path.
    const reasons = new Set(
      variants
        .filter((v) => !startsWithPath(v.parts))
        .map((v) => (v.parts.length > 0 && v.parts[0].hole !== undefined ? "url_not_static" : "url_not_path")),
    );
    for (const reason of reasons) this.addUnresolved(src, node, info.unresolvedClient, reason);
    if (paths.length === 0) return;
    const caller = this.enclosingCaller(node, src.sf);
    const loc = this.location(node, src);
    const callee = info.callee || null;
    // A member call can put its callee on a later line than the call starts.
    const at = callee === null ? null : this.location(callee, src);
    this.calls.push({
      ...loc,
      callee: callee === null ? null : this.rawText(callee),
      callee_line: at === null ? null : at.line,
      callee_column: at === null ? null : at.column,
      is_test: src.isTest,
      kind: info.kind,
      client: info.client,
      builder: info.builder,
      function: caller.name,
      def_line: caller.defLine,
      method: info.method.method,
      method_source: info.method.source,
      urls: paths.map((v) => variantJson(v)),
      raw: this.rawText(urlExpr),
    });
  }

  methodOf(initExpr, spec, src, call) {
    const fallback = { method: spec.defaultMethod, source: "default" };
    if (!initExpr) return fallback;
    const bare = unwrap(this.ts, initExpr);
    if (this.ts.isIdentifier(bare) && bare.text === "undefined") return fallback;
    const unknown = (why) => {
      this.addLimitation(
        "unresolved_http_method",
        src,
        call,
        `${spec.label} call: HTTP method could not be evaluated (${why}); reported as UNKNOWN`,
      );
      return { method: "UNKNOWN", source: "unresolved" };
    };
    const found = this.objectLiteralOf(initExpr, new Map(), 0);
    if (!found) return unknown(`init argument \`${truncate(initExpr.getText(), 60)}\` is not an object literal`);
    const prop = this.propertyValue(found.object, "method", found.env);
    if (prop.ambiguous) return unknown(`a ${prop.cause} in the init object may set the method`);
    if (prop.value === null) return fallback;
    const ctx = this.newContext({ propFlow: false, record: false });
    const variants = this.evalExpr(prop.value, found.env, ctx, 0).map((v) => compress(v.parts));
    const texts = new Set();
    for (const parts of variants) {
      if (parts.length !== 1 || parts[0].text === undefined) return unknown(`method \`${truncate(prop.value.getText(), 60)}\` is not a string constant`);
      texts.add(parts[0].text.toUpperCase());
    }
    if (texts.size !== 1) return unknown(`method \`${truncate(prop.value.getText(), 60)}\` has several values`);
    const value = [...texts][0];
    if (!HTTP_METHODS.has(value)) return unknown(`method ${JSON.stringify(value)} is not an HTTP method`);
    return { method: value, source: "init" };
  }

  // An expression that evaluates to an object literal: the literal itself or a
  // const (or bound parameter) whose value is one.
  objectLiteralOf(expr, env, depth) {
    const ts = this.ts;
    const e = unwrap(ts, expr);
    if (depth > this.req.limits.max_eval_depth) return null;
    if (ts.isObjectLiteralExpression(e)) return { object: e, env };
    if (ts.isIdentifier(e)) {
      const decl = this.declsOf(e)[0];
      if (!decl) return null;
      if (ts.isParameter(decl) && env.has(decl)) {
        const bound = env.get(decl);
        return bound.expr ? this.objectLiteralOf(bound.expr, bound.env, depth + 1) : null;
      }
      if (this.isConstDeclaration(decl) && decl.initializer) return this.objectLiteralOf(decl.initializer, env, depth + 1);
    }
    return null;
  }

  // A property's name; a computed key is evaluated (`[K]` with a const K),
  // and UNKNOWN_KEY when it has no single string value.
  propertyName(prop, env = new Map()) {
    const ts = this.ts;
    const name = prop.name;
    if (!name) return null;
    if (ts.isIdentifier(name) || ts.isStringLiteral(name) || ts.isNoSubstitutionTemplateLiteral(name) || ts.isNumericLiteral(name)) {
      return name.text;
    }
    if (!ts.isComputedPropertyName(name)) return null;
    const ctx = this.newContext({ propFlow: false, record: false });
    const texts = new Set();
    for (const v of this.evalExpr(name.expression, env, ctx, 0)) {
      const parts = compress(v.parts);
      if (parts.length > 1 || (parts.length === 1 && parts[0].text === undefined)) return UNKNOWN_KEY;
      texts.add(parts.length ? parts[0].text : "");
    }
    return texts.size === 1 ? [...texts][0] : UNKNOWN_KEY;
  }

  // The last top-level property called `name` (`value`, null when absent),
  // and whether a later spread or unreadable computed key may set it too
  // (`ambiguous`, with `cause` naming which).
  propertyValue(object, name, env = new Map(), depth = 0) {
    const found = { value: null, ambiguous: false, cause: null };
    for (const prop of object.properties) this.foldProperty(found, prop, name, env, depth);
    return found;
  }

  // Updates `found` with one property, in source order: a later definition
  // of `name` replaces an earlier one, a spread that may define it or an
  // unreadable computed key makes it ambiguous.
  foldProperty(found, prop, name, env, depth) {
    const ts = this.ts;
    if (ts.isSpreadAssignment(prop)) {
      if (!found.ambiguous && this.spreadMayDefine(prop.expression, name, env, depth + 1)) {
        found.ambiguous = true;
        found.cause = "spread";
      }
      return;
    }
    const key = this.propertyName(prop, env);
    if (key === UNKNOWN_KEY) {
      found.ambiguous = true;
      found.cause = "computed key";
      return;
    }
    if (key !== name) return;
    if (ts.isPropertyAssignment(prop)) found.value = prop.initializer;
    else if (ts.isShorthandPropertyAssignment(prop)) found.value = prop.name;
    else found.value = null;
    found.ambiguous = found.value === null;
    found.cause = found.ambiguous ? "method or accessor" : null;
  }

  // False only when every object the spread can produce lacks `name`, as in
  // `...(reason == null ? {} : { body })`.
  spreadMayDefine(expr, name, env, depth) {
    const ts = this.ts;
    const e = unwrap(ts, expr);
    if (depth > this.req.limits.max_eval_depth) return true;
    if (ts.isConditionalExpression(e)) {
      return this.spreadMayDefine(e.whenTrue, name, env, depth + 1) || this.spreadMayDefine(e.whenFalse, name, env, depth + 1);
    }
    if (ts.isBinaryExpression(e)) {
      const op = e.operatorToken.kind;
      if (op === ts.SyntaxKind.AmpersandAmpersandToken) return this.spreadMayDefine(e.right, name, env, depth + 1);
      if (op === ts.SyntaxKind.QuestionQuestionToken || op === ts.SyntaxKind.BarBarToken) {
        return this.spreadMayDefine(e.left, name, env, depth + 1) || this.spreadMayDefine(e.right, name, env, depth + 1);
      }
    }
    const found = this.objectLiteralOf(e, env, depth + 1);
    if (!found) return true;
    const prop = this.propertyValue(found.object, name, found.env, depth + 1);
    return prop.value !== null || prop.ambiguous;
  }

  // ── uncalled client references ──

  walkClientReferences(src) {
    if (src.isTest) return;
    const ts = this.ts;
    walkNodes(ts, src.sf, (node) => {
      const name = this.referenceName(node);
      if (name === null || !this.clientNames.has(name.text) || this.handledRefs.has(name) || !this.isValueReference(node)) return;
      // A member reference is resolved through its object, like a callee.
      const target = node === name ? node : node.parent;
      const spec = this.clientOf(target);
      if (!spec || this.insideDeclarations(node, this.clientByDecl)) return;
      // `const f = api.get` names a request member, not the instance, so `f`
      // is not followed: the reference itself is what is reported.
      const use = spec !== UNRESOLVED && spec.kind === "instance" ? this.instanceMemberUse(target) : null;
      if (use === "setup" || (use === null && this.isAliasInitializer(node))) return;
      if (spec === UNRESOLVED) this.addUnresolved(src, target, name.text, "client_not_resolved");
      else this.addUnresolved(src, target, spec.label, "client_reference_not_called");
    });
  }

  // The name node when `node` is an identifier or the string key of `a["f"]`.
  referenceName(node) {
    const ts = this.ts;
    if (ts.isIdentifier(node)) return node;
    const parent = node.parent;
    if (ts.isStringLiteral(node) && parent && ts.isElementAccessExpression(parent) && parent.argumentExpression === node) {
      return node;
    }
    return null;
  }

  isValueReference(node) {
    const ts = this.ts;
    const parent = node.parent;
    if (!parent) return false;
    if (parent.name === node && !ts.isPropertyAccessExpression(parent) && !ts.isShorthandPropertyAssignment(parent)) return false;
    if (ts.isPropertyAssignment(parent) && parent.name === node) return false;
    // `{ apiJson: get }` in a destructuring names the property being read.
    if (ts.isBindingElement(parent) && parent.propertyName === node) return false;
    for (let p = parent; p; p = p.parent) {
      if (ts.isTypeNode(p) || ts.isImportDeclaration(p) || ts.isExportDeclaration(p) || ts.isImportEqualsDeclaration(p)) return false;
      if (ts.isStatement(p) || ts.isSourceFile(p)) break;
    }
    return true;
  }

  // `const f = apiJson` and `export default apiJson`: the alias's own
  // references are checked instead.
  isAliasInitializer(node) {
    const ts = this.ts;
    const parent = node.parent;
    const member = (ts.isPropertyAccessExpression(parent) && parent.name === node) || (ts.isElementAccessExpression(parent) && parent.argumentExpression === node);
    const target = member ? parent : node;
    const c = parentThroughTransparent(ts, target);
    if (!c) return false;
    if (ts.isExportAssignment(c.parent)) return true;
    return this.isConstDeclaration(c.parent) && c.parent.initializer === c.node;
  }

  // ── builder calls ──

  walkBuilderCalls(src) {
    if (this.builderByDecl.size === 0) return;
    const ts = this.ts;
    walkNodes(ts, src.sf, (node) => {
      if (!ts.isCallExpression(node) || this.consumed.has(node)) return;
      const builder = this.builderOf(node.expression);
      if (!builder || this.insideDeclarations(node, this.builderByDecl) || this.insideDeclarations(node, this.clientByDecl)) return;
      const ctx = this.newContext({ propFlow: true, record: true, src });
      const variants = this.finishVariants(this.evalExpr(node, new Map(), ctx, 0), ctx, src, node);
      // A builder only names a path; GET is the default, as for a client call without init.
      this.emitCall(src, node, node, variants, {
        kind: "builder",
        client: null,
        builder: builder.name,
        method: { method: "GET", source: "default" },
        unresolvedClient: builder.name,
        callee: this.calleeName(node.expression),
      });
    });
  }

  // ── WebSocket strings ──

  walkWebSockets() {
    const marker = this.req.marker;
    const markerConsts = this.markerConstNames(marker);
    const candidates = [];
    this.perFile((src) => {
      walkNodes(this.ts, src.sf, (node) => {
        if (!this.isStringLike(node)) return true;
        if (this.isOutermostString(node) && !this.consumed.has(node) && this.inValuePosition(node)) {
          const text = node.getText(src.sf);
          if (text.includes(marker) || this.mentionsAny(node, markerConsts)) {
            const ctx = this.newContext({ propFlow: false, record: false, src });
            const variants = this.finishVariants(this.evalExpr(node, new Map(), ctx, 0), ctx, src, node);
            // URLs hold no whitespace, so "could not reach /ws/x" is a message, not a URL.
            const cut = variants
              .filter((v) => !v.parts.some((p) => p.text !== undefined && WHITESPACE_RE.test(p.text)))
              .map((v) => cutAtMarker(v, marker))
              .filter((v) => v !== null);
            if (cut.length) candidates.push({ src, node, variants: cut, used: ctx.usedDecls });
          }
        }
        return false; // nested string parts belong to this expression
      });
    });
    const usedElsewhere = new Set();
    for (const c of candidates) for (const d of c.used) usedElsewhere.add(d);
    for (const c of candidates) {
      const holder = parentThroughTransparent(this.ts, c.node);
      if (holder && this.isConstDeclaration(holder.parent) && usedElsewhere.has(holder.parent)) continue;
      const caller = this.enclosingCaller(c.node, c.src.sf);
      this.calls.push({
        ...this.location(c.node, c.src),
        callee: null,
        callee_line: null,
        callee_column: null,
        is_test: c.src.isTest,
        kind: "websocket",
        client: null,
        builder: null,
        function: caller.name,
        def_line: caller.defLine,
        method: "WS",
        method_source: "default",
        urls: c.variants.map((v) => variantJson(v)),
        raw: this.rawText(c.node),
      });
    }
  }

  markerConstNames(marker) {
    const ts = this.ts;
    const names = new Set();
    for (const src of this.sources) {
      this.guarded(src, () =>
        walkNodes(ts, src.sf, (node) => {
          if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer && this.isConstDeclaration(node)) {
            if (node.initializer.getText(src.sf).includes(marker)) names.add(node.name.text);
          }
        }),
      );
    }
    return names;
  }

  mentionsAny(node, names) {
    if (names.size === 0) return false;
    let hit = false;
    walkNodes(this.ts, node, (n) => {
      if (hit) return false;
      if (this.ts.isIdentifier(n) && names.has(n.text)) hit = true;
      return !hit;
    });
    return hit;
  }

  isPlus(node) {
    return this.ts.isBinaryExpression(node) && node.operatorToken.kind === this.ts.SyntaxKind.PlusToken;
  }

  // A string literal or template, or a `+` chain with one among its operands.
  // Iterative: a chain of thousands of `+` must not overflow the stack.
  isStringLike(node) {
    const ts = this.ts;
    const stack = [node];
    while (stack.length > 0) {
      const n = stack.pop();
      if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n) || ts.isTemplateExpression(n)) return true;
      if (this.isPlus(n)) stack.push(unwrap(ts, n.right), unwrap(ts, n.left));
    }
    return false;
  }

  isOutermostString(node) {
    const ts = this.ts;
    let p = node.parent;
    while (p && (ts.isParenthesizedExpression(p) || ts.isAsExpression(p) || ts.isSatisfiesExpression(p) || ts.isNonNullExpression(p))) p = p.parent;
    if (!p) return true;
    if (ts.isTemplateSpan(p)) return false;
    return !(this.isPlus(p) && this.isStringLike(p));
  }

  inValuePosition(node) {
    const ts = this.ts;
    const p = node.parent;
    if (!p) return false;
    if (ts.isLiteralTypeNode(p) || ts.isImportDeclaration(p) || ts.isExportDeclaration(p) || ts.isExternalModuleReference(p)) return false;
    if (ts.isImportTypeNode(p) || ts.isModuleDeclaration(p)) return false;
    if (p.name === node) return false; // property, enum member or method names
    if (ts.isCallExpression(p) && (p.expression.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(p.expression) && p.expression.text === "require"))) return false;
    return true;
  }

  // ── evaluator ──

  newContext({ propFlow, record, src = null }) {
    return { propFlow, record, src, truncated: false, usedDecls: new Set(), stack: new Set(), nonLiteralSites: [] };
  }

  finishVariants(variants, ctx, src, node) {
    let out = variants;
    if (ctx.propFlow) out = this.applyPropFlows(out, ctx, src);
    if (out.length > this.req.limits.max_url_variants) {
      out = out.slice(0, this.req.limits.max_url_variants);
      ctx.truncated = true;
    }
    if (ctx.truncated) {
      this.addLimitation(
        "url_variants_truncated",
        src,
        node,
        `URL \`${truncate(node.getText(src.sf), 80)}\` has more than ${this.req.limits.max_url_variants} variants; only the first are kept`,
      );
    }
    return out.map((v) => ({ ...v, parts: classifyHoles(compress(v.parts)) }));
  }

  hole(expr, extra = {}) {
    return [makeVariant([{ hole: "param", expr: wellFormed(truncate(expr.getText(), 120)), ...extra }], "literal")];
  }

  product(lists, ctx) {
    const max = this.req.limits.max_url_variants;
    let acc = [makeVariant([], "literal")];
    for (const list of lists) {
      const next = [];
      for (const a of acc) {
        for (const b of list) {
          if (next.length >= max) {
            ctx.truncated = true;
            break;
          }
          next.push(joinVariants(a, b));
        }
      }
      acc = next;
    }
    return acc;
  }

  union(lists, ctx, origin) {
    const max = this.req.limits.max_url_variants;
    const out = [];
    for (const list of lists) {
      for (const v of list) {
        if (out.length >= max) {
          ctx.truncated = true;
          return out;
        }
        out.push(origin ? withOrigin(v, origin) : v);
      }
    }
    return out;
  }

  evalExpr(expr, env, ctx, depth) {
    const ts = this.ts;
    const e = unwrap(ts, expr);
    if (ctx.record) this.consumed.add(e);
    if (depth > this.req.limits.max_eval_depth) return this.hole(e);
    if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) return [makeVariant([{ text: e.text }], "literal")];
    if (ts.isNumericLiteral(e)) return [makeVariant([{ text: e.text }], "literal")];
    if (ts.isTemplateExpression(e)) return this.evalTemplate(e, env, ctx, depth);
    if (ts.isBinaryExpression(e)) return this.evalBinary(e, env, ctx, depth);
    if (ts.isConditionalExpression(e)) {
      return this.union([this.evalExpr(e.whenTrue, env, ctx, depth + 1), this.evalExpr(e.whenFalse, env, ctx, depth + 1)], ctx, "conditional");
    }
    if (ts.isIdentifier(e)) return this.evalIdentifier(e, env, ctx, depth);
    if (ts.isPropertyAccessExpression(e) || ts.isElementAccessExpression(e)) return this.evalMember(e, env, ctx, depth);
    if (ts.isCallExpression(e)) return this.evalCall(e, env, ctx, depth);
    return this.hole(e);
  }

  evalTemplate(e, env, ctx, depth) {
    const lists = [[makeVariant([{ text: e.head.text }], "literal")]];
    for (const span of e.templateSpans) {
      lists.push(this.evalExpr(span.expression, env, ctx, depth + 1));
      lists.push([makeVariant([{ text: span.literal.text }], "literal")]);
    }
    return this.product(lists, ctx);
  }

  // `a + b` concatenates; `a ?? b` and `a || b` give one variant per side.
  evalBinary(e, env, ctx, depth) {
    const ts = this.ts;
    const op = e.operatorToken.kind;
    if (op === ts.SyntaxKind.PlusToken) {
      return this.product([this.evalExpr(e.left, env, ctx, depth + 1), this.evalExpr(e.right, env, ctx, depth + 1)], ctx);
    }
    if (op === ts.SyntaxKind.QuestionQuestionToken || op === ts.SyntaxKind.BarBarToken) {
      return this.union([this.evalExpr(e.left, env, ctx, depth + 1), this.evalExpr(e.right, env, ctx, depth + 1)], ctx, "conditional");
    }
    return this.hole(e);
  }

  // Evaluates a declaration's value once per chain; a cycle becomes a hole.
  evalDeclValue(decl, valueExpr, env, ctx, depth, origin, holeNode) {
    if (ctx.stack.has(decl)) return this.hole(holeNode);
    ctx.stack.add(decl);
    ctx.usedDecls.add(decl);
    try {
      const out = this.evalExpr(valueExpr, env, ctx, depth + 1);
      return origin ? out.map((v) => withOrigin(v, origin)) : out;
    } finally {
      ctx.stack.delete(decl);
    }
  }

  evalIdentifier(id, env, ctx, depth) {
    const ts = this.ts;
    if (id.text === "undefined") return this.hole(id);
    const decl = this.declsOf(id)[0];
    if (!decl) return this.hole(id);
    if (ts.isParameter(decl)) return this.evalParam(decl, [], id, env, ctx, depth);
    if (ts.isBindingElement(decl)) return this.evalBindingElement(decl, id, env, ctx, depth);
    if (this.isConstDeclaration(decl) && decl.initializer && ts.isIdentifier(decl.name)) {
      return this.evalDeclValue(decl, decl.initializer, env, ctx, depth, "const", id);
    }
    if (ts.isEnumMember(decl) && decl.initializer) return this.evalDeclValue(decl, decl.initializer, new Map(), ctx, depth, "const", id);
    return this.hole(id);
  }

  // A parameter, optionally followed by property names (`p.a.b` or a
  // destructured `{ a: { b } }`). Bound parameters evaluate the argument;
  // unbound ones become holes that prop flow may fill later.
  evalParam(param, propPath, holeNode, env, ctx, depth) {
    if (env.has(param)) {
      const bound = env.get(param);
      if (!bound.expr) return this.hole(holeNode);
      if (propPath.length === 0) return this.evalExpr(bound.expr, bound.env, ctx, depth + 1);
      const value = this.navigate(bound.expr, bound.env, propPath, depth);
      return value ? this.evalExpr(value.expr, value.env, ctx, depth + 1) : this.hole(holeNode);
    }
    return this.hole(holeNode, { param, propPath });
  }

  navigate(expr, env, propPath, depth) {
    let cur = { expr, env };
    for (const name of propPath) {
      const found = this.objectLiteralOf(cur.expr, cur.env, depth + 1);
      const prop = found && this.propertyValue(found.object, name, found.env);
      if (!prop || prop.value === null || prop.ambiguous) return null;
      cur = { expr: prop.value, env: found.env };
    }
    return cur;
  }

  evalBindingElement(element, id, env, ctx, depth) {
    const ts = this.ts;
    const propPath = [];
    let node = element;
    while (ts.isBindingElement(node)) {
      if (!ts.isObjectBindingPattern(node.parent) || node.dotDotDotToken) return this.hole(id);
      const key = node.propertyName || node.name;
      if (!(ts.isIdentifier(key) || ts.isStringLiteral(key))) return this.hole(id);
      propPath.unshift(key.text);
      node = node.parent.parent;
    }
    if (ts.isParameter(node)) return this.evalParam(node, propPath, id, env, ctx, depth);
    if (this.isConstDeclaration(node) && node.initializer) {
      const value = this.navigate(node.initializer, env, propPath, depth);
      if (value) return this.evalDeclValue(element, value.expr, value.env, ctx, depth, "const", id);
    }
    return this.hole(id);
  }

  // A property of a const object, an enum member, an imported const through a
  // namespace, or a property of a parameter.
  evalMember(e, env, ctx, depth) {
    const nameNode = this.memberNameNode(e);
    if (!nameNode) return this.hole(e);
    const decl = this.declsOf(nameNode)[0];
    const fromDecl = decl ? this.evalMemberDecl(decl, e, env, ctx, depth) : null;
    return fromDecl || this.evalParamMember(e, env, ctx, depth);
  }

  // The name of `a.b`, or the literal key of `a["b"]`; null for a computed key.
  memberNameNode(e) {
    const ts = this.ts;
    if (ts.isPropertyAccessExpression(e)) return e.name;
    const arg = unwrap(ts, e.argumentExpression);
    return ts.isStringLiteral(arg) || ts.isNoSubstitutionTemplateLiteral(arg) ? arg : null;
  }

  // The value of a const object member, a const or an enum member; null for
  // any other declaration.
  evalMemberDecl(decl, e, env, ctx, depth) {
    const ts = this.ts;
    if (ts.isPropertyAssignment(decl) && this.isConstObjectMember(decl)) {
      return this.evalDeclValue(decl, decl.initializer, env, ctx, depth, "const", e);
    }
    if (ts.isShorthandPropertyAssignment(decl) && this.isConstObjectMember(decl)) {
      return this.evalDeclValue(decl, decl.name, env, ctx, depth, "const", e);
    }
    if (this.isConstDeclaration(decl) && decl.initializer) return this.evalDeclValue(decl, decl.initializer, env, ctx, depth, "const", e);
    if (ts.isEnumMember(decl) && decl.initializer) return this.evalDeclValue(decl, decl.initializer, new Map(), ctx, depth, "const", e);
    return null;
  }

  // `config.exportPath` where `config` is a parameter.
  evalParamMember(e, env, ctx, depth) {
    const ts = this.ts;
    const chain = [];
    let base = e;
    while (ts.isPropertyAccessExpression(base)) {
      chain.unshift(base.name.text);
      base = unwrap(ts, base.expression);
    }
    if (ts.isIdentifier(base)) {
      const baseDecl = this.declsOf(base)[0];
      if (baseDecl && ts.isParameter(baseDecl) && ts.isIdentifier(baseDecl.name)) return this.evalParam(baseDecl, chain, e, env, ctx, depth);
    }
    return this.hole(e);
  }

  isConstObjectMember(prop) {
    const ts = this.ts;
    let node = prop;
    for (;;) {
      let p = node.parent;
      if (!p || !ts.isObjectLiteralExpression(p)) return false;
      let c = p;
      p = p.parent;
      while (p && (ts.isAsExpression(p) || ts.isSatisfiesExpression(p) || ts.isParenthesizedExpression(p))) {
        c = p;
        p = p.parent;
      }
      if (!p) return false;
      if (this.isConstDeclaration(p) && p.initializer === c) return true;
      if (ts.isPropertyAssignment(p) && p.initializer === c) {
        node = p;
        continue;
      }
      return false;
    }
  }

  returnsOf(fn) {
    const ts = this.ts;
    const body = ts.bodyOf(fn);
    if (!ts.isBlock(body)) return [body];
    const out = [];
    walkNodes(ts, body, (n) => {
      if (n !== body && ts.isFunctionLike(n)) return false;
      if (ts.isReturnStatement(n) && n.expression) out.push(n.expression);
      return true;
    });
    return out;
  }

  evalReturns(fn, env, ctx, depth) {
    const returns = this.returnsOf(fn);
    if (returns.length === 0) return null;
    return this.union(returns.map((r) => this.evalExpr(r, env, ctx, depth + 1)), ctx, returns.length > 1 ? "conditional" : null);
  }

  // A call to a function declared in the program: arguments are bound to its
  // parameters and its return expressions are evaluated (builders).
  evalCall(call, env, ctx, depth) {
    const ts = this.ts;
    const nameNode = this.calleeName(call.expression);
    if (!nameNode) return this.hole(call);
    const decl = this.declsOf(nameNode).find((d) => {
      const f = this.functionOfDecl(d);
      return f && this.ts.bodyOf(f);
    });
    const fn = this.functionOfDecl(decl);
    if (!fn || !this.ts.bodyOf(fn) || fn.getSourceFile().isDeclarationFile || ctx.stack.has(fn)) return this.hole(call);
    const builderName = this.builderByDecl.get(decl);
    if (ctx.record && builderName !== undefined) this.consumed.add(call);
    const inner = new Map(env);
    let spread = false;
    fn.parameters.forEach((param, i) => {
      const arg = call.arguments[i];
      if (spread || param.dotDotDotToken || (arg && ts.isSpreadElement(arg))) {
        spread = true;
        inner.set(param, { expr: null, env });
      } else if (arg) inner.set(param, { expr: arg, env });
      else if (param.initializer) inner.set(param, { expr: param.initializer, env: inner });
      else inner.set(param, { expr: null, env });
    });
    ctx.stack.add(fn);
    let out;
    try {
      out = this.evalReturns(fn, inner, ctx, depth);
    } finally {
      ctx.stack.delete(fn);
    }
    if (!out) return this.hole(call);
    if (builderName !== undefined) out = out.map((v) => withVia(withOrigin(v, "builder"), builderName));
    return out;
  }

  // ── prop flow ──

  applyPropFlows(variants, ctx, src) {
    let out = variants;
    for (let guard = 0; guard < 8; guard++) {
      let changed = false;
      const next = [];
      for (const v of out) {
        const expanded = this.expandPropFlow(v, src);
        if (expanded === null) next.push(v);
        else {
          changed = true;
          next.push(...expanded);
        }
      }
      out = next;
      if (!changed) break;
    }
    return out;
  }

  // The variants for the first parameter hole not yet expanded: one per
  // literal passed for it, plus the hole itself when some site passes a value
  // the evaluator cannot read. Null when no hole is left to expand.
  expandPropFlow(v, src) {
    const idx = v.parts.findIndex((p) => p.hole !== undefined && p.param && !p.propFlowDone);
    if (idx === -1) return null;
    const part = v.parts[idx];
    const values = this.propValues(part.param, part.propPath, src);
    const flagged = replacePart(v, idx, { ...part, propFlowDone: true });
    if (values === null || (values.literals.length === 0 && !values.nonLiteral)) return [flagged];
    const out = values.literals.map((text) =>
      withConfidence(withOrigin(replacePart(v, idx, { text }), "prop_flow"), "possible"),
    );
    // Some site passes a value the evaluator cannot read: the hole stays, unproven.
    if (values.nonLiteral) out.push(withConfidence(withOrigin(flagged, "prop_flow"), "possible"));
    return out;
  }

  // Literal values passed for a component or hook parameter (or one of its
  // props) at every call and JSX site, with the parameter's default for sites
  // that omit it; null when the function is neither a component nor a hook.
  propValues(param, propPath, src) {
    const ts = this.ts;
    const fn = param.parent;
    if (!fn || !ts.isFunctionLike(fn)) return null;
    const target = this.propFlowTarget(fn);
    if (target === null) return null;
    const { name, key } = target;
    const index = fn.parameters.indexOf(param);
    if (index < 0) return null;
    const label = ts.isIdentifier(param.name) ? [param.name.text, ...propPath].join(".") : propPath.join(".");
    const acc = { literals: new Set(), nonLiteral: false };
    const unreadable = (where, node, why) => {
      acc.nonLiteral = true;
      this.addLimitation("unresolved_prop_value", where, node, `${name}: \`${label}\` feeds a URL but ${why}`);
    };
    let omitted = false;
    for (const site of this.sitesOf(fn, key)) {
      if (site.src.isTest && !src.isTest) continue;
      // The parameter's default applies at a site that omits it.
      if (this.collectSiteValue(site, index, propPath, name, acc, unreadable) === "omitted") omitted = true;
    }
    if (omitted) this.collectDefault(param, propPath, acc, unreadable);
    return { literals: [...acc.literals], nonLiteral: acc.nonLiteral };
  }

  // The function's display name and the name its sites are found by, or null
  // when it is neither a component nor a hook. An anonymous component
  // exported as the default (`export default memo(({ path }) => ...)`) is
  // found through the module's default key.
  propFlowTarget(fn) {
    const ts = this.ts;
    const name = callerName(ts, fn);
    if (isComponentName(name) || HOOK_NAME_RE.test(name || "")) return { name, key: name };
    if (name !== "default" || !containsJsxOutsideNestedFunctions(ts, ts.bodyOf(fn))) return null;
    return { name: "the default export", key: this.defaultKey(fn.getSourceFile().fileName) };
  }

  // Adds one site's literal values to `acc`; "omitted" when the site does not pass the value.
  collectSiteValue(site, index, propPath, name, acc, unreadable) {
    if (site.tie === "untied") {
      unreadable(site.src, site.node, `this site's \`${site.target}\` could not be traced to ${name}'s declaration`);
      return "read";
    }
    const value = this.siteValue(site, index, propPath);
    if (value === undefined) return "omitted";
    const texts = value === null ? null : this.literalTexts(value);
    if (texts === null) unreadable(site.src, site.node, "this site passes a value that is not a string literal");
    else for (const t of texts) acc.literals.add(t);
    return "read";
  }

  collectDefault(param, propPath, acc, unreadable) {
    const fallback = this.defaultValue(param, propPath);
    if (!fallback) return;
    const texts = this.literalTexts(fallback);
    if (texts !== null) {
      for (const t of texts) acc.literals.add(t);
      return;
    }
    const owner = this.sourceOf(fallback.expr);
    if (owner) unreadable(owner, fallback.expr, "its default is not a string literal");
    else acc.nonLiteral = true;
  }

  // The default for a parameter, or for the destructured prop at `propPath`
  // (`{ path = "/a" }`); null when there is none.
  defaultValue(param, propPath) {
    const ts = this.ts;
    if (propPath.length === 0) return param.initializer ? { expr: param.initializer, env: new Map() } : null;
    let pattern = param.name;
    for (let i = 0; i < propPath.length; i += 1) {
      if (!ts.isObjectBindingPattern(pattern)) return null;
      const element = pattern.elements.find((el) => {
        if (el.dotDotDotToken) return false;
        const key = el.propertyName || el.name;
        return (ts.isIdentifier(key) || ts.isStringLiteral(key)) && key.text === propPath[i];
      });
      if (!element) return null;
      if (i === propPath.length - 1) return element.initializer ? { expr: element.initializer, env: new Map() } : null;
      pattern = element.name;
    }
    return null;
  }

  sourceOf(node) {
    const sf = node.getSourceFile();
    return this.sources.find((s) => s.sf === sf) || null;
  }

  literalTexts(value) {
    const ctx = this.newContext({ propFlow: false, record: false });
    const variants = this.evalExpr(value.expr, value.env, ctx, 0);
    const texts = [];
    for (const v of variants) {
      const parts = compress(v.parts);
      if (parts.some((p) => p.hole !== undefined)) return null;
      texts.push(parts.length ? parts[0].text : "");
    }
    return texts;
  }

  // undefined: not passed; null: passed but unreadable; else {expr, env}.
  siteValue(site, index, propPath) {
    if (site.kind === "call") return this.callSiteValue(site.node.arguments, index, propPath);
    if (index !== 0 || propPath.length === 0) return null;
    const attrValue = this.jsxAttributeValue(site.node.attributes.properties, propPath[0]);
    if (!attrValue || propPath.length === 1) return attrValue;
    const found = this.objectLiteralOf(attrValue.expr, attrValue.env, 0);
    return found ? this.navigateSite(found, propPath.slice(1)) : null;
  }

  callSiteValue(args, index, propPath) {
    if (args.slice(0, index + 1).some((a) => this.ts.isSpreadElement(a))) return null;
    const arg = args[index];
    if (!arg) return undefined;
    if (propPath.length === 0) return { expr: arg, env: new Map() };
    const found = this.objectLiteralOf(arg, new Map(), 0);
    return found ? this.navigateSite(found, propPath) : null;
  }

  // The last value given for JSX attribute `name`: a spread after it may
  // override it, so it is then unreadable, as is a spread with no attribute.
  jsxAttributeValue(properties, name) {
    const ts = this.ts;
    let spread = false;
    let attrValue;
    for (const attr of properties) {
      if (ts.isJsxSpreadAttribute(attr)) {
        spread = true;
      } else if (attr.name.getText() === name) {
        spread = false;
        attrValue = this.jsxInitializerValue(attr.initializer);
      }
    }
    if (attrValue === undefined) return spread ? null : undefined;
    return attrValue;
  }

  jsxInitializerValue(init) {
    const ts = this.ts;
    if (init && ts.isStringLiteral(init)) return { expr: init, env: new Map() };
    if (init && ts.isJsxExpression(init) && init.expression) return { expr: init.expression, env: new Map() };
    return null;
  }

  navigateSite(found, propPath) {
    let cur = found;
    for (let i = 0; i < propPath.length; i++) {
      const prop = this.propertyValue(cur.object, propPath[i], cur.env);
      if (prop.ambiguous) return null;
      if (prop.value === null) return undefined;
      if (i === propPath.length - 1) return { expr: prop.value, env: cur.env };
      const next = this.objectLiteralOf(prop.value, cur.env, 0);
      if (!next) return null;
      cur = next;
    }
    return null;
  }

  // Call and JSX sites whose name may denote `fn`: each is tied to `fn`
  // through the checker (`tie: "fn"`), or could not be traced (`tie:
  // "untied"`, its value then unknown). Sites of other declarations are dropped.
  sitesOf(fn, name) {
    if (this.callSites.has(fn)) return this.callSites.get(fn);
    const ts = this.ts;
    const names = this.aliasNames(new Set([name]), { wrapperCalls: true });
    const sites = [];
    for (const src of this.sources) {
      this.guarded(src, () =>
        walkNodes(ts, src.sf, (node) => {
          let target = null;
          let kind = null;
          if (ts.isCallExpression(node)) {
            target = this.calleeName(node.expression);
            kind = "call";
          } else if (ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node)) {
            target = ts.isIdentifier(node.tagName) ? node.tagName : ts.isPropertyAccessExpression(node.tagName) ? node.tagName.name : null;
            kind = "jsx";
          }
          if (!target || !names.has(target.text)) return;
          const decls = this.declsOf(target);
          const tie = decls.length === 0 && this.importsFromOutside(target) ? "other" : this.tieDecls(decls, fn, 0);
          if (tie !== "other") sites.push({ kind, node, src, tie, target: target.text });
        }),
      );
    }
    this.callSites.set(fn, sites);
    return sites;
  }

  // "fn" when the declarations denote `fn` (directly, through `const X = Y`,
  // through memo/forwardRef, or loaded by dynamic/lazy), "other" when they
  // denote another declaration, "untied" when they may denote `fn` but the
  // chain cannot be followed.
  tieDecls(decls, fn, depth) {
    if (decls.length === 0 || depth > 8) return "untied";
    let result = "other";
    for (const d of decls) {
      const tie = d === fn || this.functionOfDecl(d) === fn ? "fn" : this.tieDecl(d, fn, depth);
      if (tie === "fn") return "fn";
      if (tie === "untied") result = "untied";
    }
    return result;
  }

  tieDecl(decl, fn, depth) {
    const ts = this.ts;
    if (ts.isParameter(decl) || ts.isBindingElement(decl)) return "untied"; // a component passed in as a value
    // A default import of `export default memo(X)` denotes the exported value.
    if (ts.isExportAssignment(decl)) return this.tieExpr(decl.expression, fn, depth + 1);
    if (!ts.isVariableDeclaration(decl)) return "other";
    if (!decl.initializer) return "untied"; // assigned later, to anything
    const tie = this.tieExpr(decl.initializer, fn, depth + 1);
    // A `let` that starts out as `fn` may be reassigned before the site runs.
    return tie === "fn" && !this.isConstDeclaration(decl) ? "untied" : tie;
  }

  // An initializer ties to `fn` only through a name, a member, or a call one
  // of whose arguments ties back (or loads `fn`); any other value
  // (`styled.button\`\``, an object, a literal) is some other component.
  tieExpr(expr, fn, depth) {
    const ts = this.ts;
    const e = unwrap(ts, expr);
    if (e === fn) return "fn";
    if (depth > 8) return "untied";
    if (ts.isIdentifier(e)) return this.tieDecls(this.declsOf(e), fn, depth + 1);
    if (ts.isPropertyAccessExpression(e)) return this.tieDecls(this.declsOf(e.name), fn, depth + 1);
    if (!ts.isCallExpression(e)) return "other";
    const callee = this.calleeName(e.expression);
    const passesProps = !!callee && COMPONENT_WRAPPERS.has(callee.text);
    for (const arg of e.arguments) {
      const tie = this.tieArgument(arg, fn, depth, passesProps);
      if (tie !== "other") return tie;
    }
    return "other";
  }

  tieArgument(arg, fn, depth, passesProps) {
    const loaded = this.lazyLoadTarget(arg);
    if (loaded) {
      // A loader renders the export it loads with the props it is given.
      if (loaded.member === null && loaded.moduleFile !== null) return "untied";
      return this.tieDecls(loaded.decls, fn, depth + 1) === "fn" ? "fn" : "other";
    }
    // Another wrapper may change the props on the way to `fn`.
    if (this.tieExpr(arg, fn, depth + 1) !== "fn") return "other";
    return passesProps ? "fn" : "untied";
  }
}

// The own name of a function or class expression; null for anything else.
function inlineName(ts, n) {
  return (ts.isFunctionExpression(n) || ts.isClassExpression(n)) && n.name ? n.name.text : null;
}

// An identifier's or string literal's text; null for anything else.
function nameText(ts, n) {
  return n && (ts.isIdentifier(n) || ts.isStringLiteral(n)) ? n.text : null;
}

// ── Variants ─────────────────────────────────────────────────────────────

function makeVariant(parts, origin) {
  return { parts, origins: new Set([origin]), via: [], confidence: "exact" };
}

function joinVariants(a, b) {
  return {
    parts: [...a.parts, ...b.parts],
    origins: new Set([...a.origins, ...b.origins]),
    via: [...a.via, ...b.via.filter((x) => !a.via.includes(x))],
    confidence: a.confidence === "possible" || b.confidence === "possible" ? "possible" : "exact",
  };
}

function withOrigin(v, origin) {
  return { ...v, origins: new Set([...v.origins, origin]) };
}

function withVia(v, name) {
  return v.via.includes(name) ? v : { ...v, via: [name, ...v.via] };
}

function withConfidence(v, confidence) {
  return { ...v, confidence };
}

function replacePart(v, index, part) {
  const parts = v.parts.slice();
  parts[index] = part;
  return { ...v, parts };
}

function compress(parts) {
  const out = [];
  for (const p of parts) {
    if (p.text !== undefined) {
      if (p.text === "") continue;
      const last = out[out.length - 1];
      if (last && last.text !== undefined) out[out.length - 1] = { text: last.text + p.text };
      else out.push({ text: p.text });
    } else out.push(p);
  }
  return out;
}

function isAbsoluteUrl(parts) {
  return parts.length > 0 && parts[0].text !== undefined && ABSOLUTE_URL_RE.test(parts[0].text);
}

// What an axios-style instance requests: `baseURL` joined to a relative URL
// with exactly one slash between. An absolute URL is left as it is.
function withBasePath(v, basePath) {
  const first = v.parts[0];
  if (first === undefined || first.text === undefined || isAbsoluteUrl(v.parts)) return v;
  const text = first.text.startsWith("/") ? first.text : `/${first.text}`;
  return { ...v, parts: [{ text: basePath + text }, ...v.parts.slice(1)] };
}

function startsWithPath(parts) {
  return parts.length > 0 && parts[0].text !== undefined && parts[0].text.startsWith("/");
}

// Hole kinds: after a literal `?` a hole is query; a last hole that fills
// a whole segment is a param, one glued to earlier text is unknown.
function classifyHoles(parts) {
  let inQuery = false;
  return parts.map((p, i) => {
    if (p.text !== undefined) {
      if (p.text.includes("?")) inQuery = true;
      return p;
    }
    let kind = "param";
    if (inQuery) kind = "query";
    else if (i === parts.length - 1) {
      const prev = parts[i - 1];
      kind = prev && prev.text !== undefined && prev.text.endsWith("/") ? "param" : "unknown";
    }
    return { ...p, hole: kind };
  });
}

// A WebSocket URL keeps the path from the marker on; a base URL before it is dropped.
function cutAtMarker(v, marker) {
  const idx = v.parts.findIndex((p) => p.text !== undefined && p.text.includes(marker));
  if (idx === -1) return null;
  const first = v.parts[idx].text;
  const parts = [{ text: first.slice(first.indexOf(marker)) }, ...v.parts.slice(idx + 1)];
  return { ...v, parts: classifyHoles(parts.map((p) => (p.text !== undefined ? p : { hole: "param", expr: p.expr }))) };
}

function variantJson(v) {
  const origin = ORIGIN_RANK.find((o) => v.origins.has(o)) || "literal";
  return {
    parts: v.parts.map((p) => (p.text !== undefined ? { text: wellFormed(p.text) } : { hole: p.hole, expr: p.expr })),
    confidence: v.confidence,
    origin,
    via: v.via,
  };
}

// The node and its nearest ancestor that is not a transparent wrapper.
function parentThroughTransparent(ts, node) {
  let c = node;
  let p = node.parent;
  while (p && (ts.isParenthesizedExpression(p) || ts.isAsExpression(p) || ts.isSatisfiesExpression(p) || ts.isNonNullExpression(p))) {
    c = p;
    p = p.parent;
  }
  return p ? { node: c, parent: p } : null;
}

module.exports = { run };
