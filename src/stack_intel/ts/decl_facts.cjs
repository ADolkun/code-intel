"use strict";
// Declaration facts for one parsed file (protocol 1, `decls`).
//
// Names come from each declaration node's own name, never from source text, so
// keywords in comments, strings, JSX text or type syntax declare nothing.
// Classification follows the declaration's own body (components) and position
// (scope); see README "Symbols" for the indexed forms and the documented gaps.

const {
  containsJsxOutsideNestedFunctions,
  isComponentName,
  lineColumn,
  lineIndex,
  lineStarts,
  truncate,
  unwrap,
  wellFormed,
} = require("./common.cjs");

// Call initializers that make a component-named const a component.
const COMPONENT_WRAPPERS = new Set(["memo", "forwardRef", "lazy", "dynamic"]);
const WHITESPACE_RE = /\s+/g;
const SURROGATE_RE = /[\uD800-\uDFFF]/;
const FIRST_LINE_RE = /\r\n|\r|\n/;
// Whitespace and comments between the last decorator and the declaration proper.
const COMMENT_RE = /\/\*[\s\S]*?\*\/|\/\/[^\n\r\u2028\u2029]*/g;
const LEADING_TRIVIA_RE = /^(?:\s|\/\/[^\n\r\u2028\u2029]*|\/\*[\s\S]*?\*\/)*/;

function isFunctionValue(ts, node) {
  return !!node && (ts.isArrowFunction(node) || ts.isFunctionExpression(node));
}

function children(ts, node) {
  const kids = [];
  ts.forEachChild(node, (child) => {
    kids.push(child);
  });
  return kids;
}

function functionKind(ts, name, fn) {
  return isComponentName(name) && containsJsxOutsideNestedFunctions(ts, ts.bodyOf(fn)) ? "component" : "function";
}

function calleeName(ts, call) {
  const callee = unwrap(ts, call.expression);
  if (ts.isIdentifier(callee)) return callee.text;
  if (ts.isPropertyAccessExpression(callee)) return callee.name.text;
  return "";
}

// Text of a member or property name, or null for computed names.
function memberName(ts, name) {
  if (!name) return null;
  if (ts.isIdentifier(name) || ts.isPrivateIdentifier(name) || ts.isStringLiteral(name)) return name.text;
  return null;
}

// The modifiers a declaration is written with. TypeScript 7 gives the inner
// names of a dotted namespace (`export namespace A.B`) the outer one's
// modifiers; TypeScript 6 gave them none, and `export` applies to `A` only.
// It also reparses JSDoc in JavaScript files into nodes flagged Reparsed
// (`@private` becomes a modifier, `@typedef` a type alias), which sit inside a
// comment and declare nothing the source wrote.
function modifiersOf(ts, node) {
  if (ts.isModuleDeclaration(node) && node.parent && ts.isModuleDeclaration(node.parent)) return undefined;
  return node.modifiers && node.modifiers.filter((m) => !isReparsed(ts, m));
}

function isReparsed(ts, node) {
  return (node.flags & ts.NodeFlags.Reparsed) !== 0;
}

function hasModifier(ts, node, kind) {
  const mods = modifiersOf(ts, node);
  return !!mods && mods.some((m) => m.kind === kind);
}

function isAmbient(ts, node) {
  return (node.flags & ts.NodeFlags.Ambient) !== 0;
}

// The qualified name of a namespace: `namespace A.B {}` and
// `namespace A { namespace B {} }` are both `A.B`.
function namespacePath(ts, decl) {
  const parts = [];
  let d = decl;
  while (d && ts.isModuleDeclaration(d)) {
    parts.unshift(d.name.text);
    let up = d.parent;
    if (up && ts.isModuleBlock(up)) up = up.parent;
    d = up;
  }
  return parts.join(".");
}

// [scope, container] of a statement-level declaration from its position. The
// nearest boundary wins: a function body, block or expression context makes
// it local; a namespace body makes it a namespace member.
function positionScope(ts, node) {
  for (let p = node.parent; p; p = p.parent) {
    if (ts.isSourceFile(p)) return ["module", null];
    if (ts.isModuleBlock(p)) return ["namespace", namespacePath(ts, p.parent)];
    if (ts.isModuleDeclaration(p)) return ["namespace", namespacePath(ts, p)];
    if (
      ts.isBlock(p) ||
      ts.isCaseBlock(p) ||
      ts.isClassStaticBlockDeclaration(p) ||
      ts.isFunctionLike(p) ||
      ts.isClassLike(p) ||
      ts.isObjectLiteralExpression(p)
    ) {
      return ["local", null];
    }
  }
  return ["module", null];
}

// The variable or property an object literal initializes, through transparent wrappers.
function objectContainer(ts, obj) {
  let c = obj;
  let p = obj.parent;
  while (
    p &&
    (ts.isParenthesizedExpression(p) ||
      ts.isAsExpression(p) ||
      ts.isSatisfiesExpression(p) ||
      ts.isNonNullExpression(p) ||
      ts.isTypeAssertionExpression(p))
  ) {
    c = p;
    p = p.parent;
  }
  if (!p) return null;
  if (ts.isVariableDeclaration(p) && p.initializer === c && ts.isIdentifier(p.name)) return p.name.text;
  if (ts.isPropertyAssignment(p) && p.initializer === c) return memberName(ts, p.name);
  return null;
}

function classContainer(ts, cls) {
  return cls.name ? cls.name.text : null;
}

// Names listed in a local `export { a, b as c }` (no module specifier), or
// exported whole by `export default a;` or `export = a;`.
function localExportNames(ts, sf) {
  const names = new Set();
  for (const stmt of sf.statements) {
    if (ts.isExportAssignment(stmt)) {
      const value = unwrap(ts, stmt.expression);
      if (ts.isIdentifier(value)) names.add(value.text);
      continue;
    }
    if (
      ts.isExportDeclaration(stmt) &&
      !stmt.moduleSpecifier &&
      stmt.exportClause &&
      ts.isNamedExports(stmt.exportClause)
    ) {
      for (const el of stmt.exportClause.elements) names.add((el.propertyName || el.name).text);
    }
  }
  return names;
}

function declarationKeyword(ts, list) {
  const flags = list.flags;
  if ((flags & ts.NodeFlags.AwaitUsing) === ts.NodeFlags.AwaitUsing) return "await using";
  if (flags & ts.NodeFlags.Using) return "using";
  if (flags & ts.NodeFlags.Const) return "const";
  if (flags & ts.NodeFlags.Let) return "let";
  return "var";
}

function bindingNames(ts, name, acc) {
  if (ts.isIdentifier(name)) {
    acc.push(name);
    return acc;
  }
  // A hole in an array pattern (`[, a]`) is an OmittedExpression in TypeScript 6
  // and a binding element with no name in TypeScript 7.
  for (const el of name.elements) {
    if (!ts.isOmittedExpression(el) && el.name) bindingNames(ts, el.name, acc);
  }
  return acc;
}

function extractDecls(ts, sf, ctx) {
  const text = sf.text;
  const bodyOf = (node) => ts.bodyOf(node);
  const exportedNames = localExportNames(ts, sf);
  const out = [];

  const collapse = (s) => wellFormed(truncate(s.replace(WHITESPACE_RE, " ").trim(), ctx.signatureMax));
  const starts = lineStarts(sf);
  // Text from `pos` to the end of its line (and at most to `end`), so a huge
  // declaration costs one line, not its whole text.
  const lineFrom = (pos, end) => {
    const line = lineIndex(sf, pos);
    const next = line + 1 < starts.length ? starts[line + 1] : text.length;
    return text.slice(pos, Math.min(next, end)).split(FIRST_LINE_RE, 1)[0];
  };
  const endLine = (node) => lineIndex(sf, node.end) + 1;

  // Where the header starts: the first modifier that is not a decorator, else
  // the keyword after the last decorator.
  const headerStart = (node) => {
    const mods = modifiersOf(ts, node);
    if (!mods || mods.length === 0) return node.getStart(sf);
    for (const m of mods) {
      if (!ts.isDecorator(m)) return m.getStart(sf);
    }
    const after = mods[mods.length - 1].end;
    const trivia = LEADING_TRIVIA_RE.exec(text.slice(after, after + 4096));
    return after + (trivia ? trivia[0].length : 0);
  };
  const openBraceStart = (node) => {
    for (const child of node.getChildren(sf)) {
      if (child.kind === ts.SyntaxKind.OpenBraceToken) return child.getStart(sf);
    }
    return node.end;
  };
  const headerTo = (node, end) => collapse(text.slice(headerStart(node), end));
  const headerFirstLine = (node) => collapse(lineFrom(headerStart(node), node.end));
  const functionHeader = (node, fn) => headerTo(node, bodyOf(fn) ? bodyOf(fn).getStart(sf) : fn.end);

  // Lines without surrogates (nearly all) need no code-point count, so many
  // declarations on one long line cost O(line) once.
  const surrogateLines = new Map();
  const position = (pos) => {
    const line = lineIndex(sf, pos);
    let hasSurrogate = surrogateLines.get(line);
    if (hasSurrogate === undefined) {
      const next = line + 1 < starts.length ? starts[line + 1] : text.length;
      hasSurrogate = SURROGATE_RE.test(text.slice(starts[line], next));
      surrogateLines.set(line, hasSurrogate);
    }
    return hasSurrogate ? lineColumn(sf, pos) : [line + 1, pos - starts[line] + 1];
  };

  const emit = (nameNode, name, node, kind, scope, container, exported, signature) => {
    const [line, column] = position(nameNode.getStart(sf));
    out.push({
      name: wellFormed(name),
      kind,
      line,
      end_line: endLine(node),
      column,
      scope,
      container: container === null ? null : wellFormed(container),
      exported,
      signature,
    });
  };
  const isExported = (stmt, name, scope) =>
    hasModifier(ts, stmt, ts.SyntaxKind.ExportKeyword) || (scope === "module" && exportedNames.has(name));

  const statementDecl = (node, kind, signature) => {
    const [scope, container] = positionScope(ts, node);
    const name = node.name.text;
    emit(node.name, name, node, kind, scope, container, isExported(node, name, scope), signature);
  };

  const variableDecl = (node) => {
    const list = node.parent;
    const stmt = list.parent;
    const keyword = declarationKeyword(ts, list);
    const base = keyword === "const" ? "const" : "variable";
    const [scope, container] = positionScope(ts, list);
    const prefix = ts.isVariableStatement(stmt) ? text.slice(headerStart(stmt), list.getStart(sf)) : "";
    const signature = collapse(`${prefix}${keyword} ${lineFrom(node.getStart(sf), node.end)}`);
    const exportedStmt = ts.isVariableStatement(stmt) && hasModifier(ts, stmt, ts.SyntaxKind.ExportKeyword);
    if (ts.isIdentifier(node.name)) {
      const name = node.name.text;
      const kind = variableKind(ts, name, node.initializer, base);
      const exported = exportedStmt || (scope === "module" && exportedNames.has(name));
      emit(node.name, name, node, kind, scope, container, exported, signature);
      return;
    }
    for (const id of bindingNames(ts, node.name, [])) {
      const exported = exportedStmt || (scope === "module" && exportedNames.has(id.text));
      emit(id, id.text, node, base, scope, container, exported, signature);
    }
  };

  const classMember = (node, cls, fn) => {
    const name = memberName(ts, node.name);
    if (name !== null) {
      emit(node.name, name, node, "method", "class", classContainer(ts, cls), false, functionHeader(node, fn));
    }
  };

  // `declare global` is a module declaration named `global` with a `module`
  // keyword and no flag of its own, so it is told from `declare module global`
  // and `namespace global` by there being no keyword between the modifiers and the name.
  const isGlobalAugmentation = (node) => {
    if (!ts.isIdentifier(node.name) || node.name.text !== "global") return false;
    const mods = modifiersOf(ts, node);
    const from = mods && mods.length > 0 ? mods[mods.length - 1].end : node.getStart(sf);
    return text.slice(from, node.name.getStart(sf)).replace(COMMENT_RE, "").trim() === "";
  };

  const namespaceDecl = (node) => {
    // `declare global` augments the global scope and `declare module "x"`
    // names a module, so neither defines a namespace.
    if (ts.isIdentifier(node.name) && !isGlobalAugmentation(node)) {
      statementDecl(node, "namespace", headerTo(node, bodyOf(node) ? bodyOf(node).getStart(sf) : node.end));
    }
  };

  const methodOrAccessor = (node) => {
    const owner = node.parent;
    if (ts.isClassLike(owner)) {
      const bodiless = !bodyOf(node) && !isAmbient(ts, node) && !hasModifier(ts, node, ts.SyntaxKind.AbstractKeyword);
      if (!bodiless) classMember(node, owner, node);
      return;
    }
    if (!ts.isObjectLiteralExpression(owner)) return;
    const name = memberName(ts, node.name);
    if (name !== null && bodyOf(node)) {
      const kind = ts.isMethodDeclaration(node) ? functionKind(ts, name, node) : "function";
      emit(node.name, name, node, kind, "object", objectContainer(ts, owner), false, functionHeader(node, node));
    }
  };

  const objectProperty = (node) => {
    const name = ts.isIdentifier(node.name) || ts.isStringLiteral(node.name) ? node.name.text : null;
    const init = unwrap(ts, node.initializer);
    if (name === null || !isFunctionValue(ts, init)) return;
    const kind = functionKind(ts, name, init);
    emit(node.name, name, node, kind, "object", objectContainer(ts, node.parent), false, functionHeader(node, init));
  };

  // Declarations that are statements (or named class expressions); false for any other node.
  const visitStatement = (node) => {
    if (ts.isFunctionDeclaration(node)) {
      // Overload signatures are not definitions; ambient declarations are.
      if (node.name && (bodyOf(node) || isAmbient(ts, node))) {
        statementDecl(node, functionKind(ts, node.name.text, node), functionHeader(node, node));
      }
    } else if (ts.isVariableDeclaration(node)) {
      if (!ts.isCatchClause(node.parent)) variableDecl(node);
    } else if (ts.isClassDeclaration(node)) {
      if (node.name) statementDecl(node, "class", headerTo(node, openBraceStart(node)));
    } else if (ts.isClassExpression(node)) {
      // A class expression's own name is bound only inside its body, so it is
      // local and never exported wherever the expression sits.
      if (node.name) emit(node.name, node.name.text, node, "class", "local", null, false, headerTo(node, openBraceStart(node)));
    } else {
      return false;
    }
    return true;
  };

  const visit = (node) => {
    if (isReparsed(ts, node)) return;
    if (visitStatement(node)) return;
    if (ts.isInterfaceDeclaration(node)) {
      statementDecl(node, "interface", headerFirstLine(node));
    } else if (ts.isTypeAliasDeclaration(node)) {
      statementDecl(node, "type_alias", headerFirstLine(node));
    } else if (ts.isEnumDeclaration(node)) {
      statementDecl(node, "enum", headerTo(node, openBraceStart(node)));
    } else if (ts.isModuleDeclaration(node)) {
      namespaceDecl(node);
    } else if (ts.isMethodDeclaration(node) || ts.isGetAccessor(node) || ts.isSetAccessor(node)) {
      methodOrAccessor(node);
    } else if (ts.isPropertyDeclaration(node) && ts.isClassLike(node.parent)) {
      const init = node.initializer ? unwrap(ts, node.initializer) : undefined;
      if (isFunctionValue(ts, init)) classMember(node, node.parent, init);
    } else if (ts.isPropertyAssignment(node) && ts.isObjectLiteralExpression(node.parent)) {
      objectProperty(node);
    }
  };
  // Iterative pre-order walk, so nesting the parser accepts never overflows the stack here.
  const stack = [sf];
  while (stack.length > 0) {
    const node = stack.pop();
    visit(node);
    const kids = children(ts, node);
    for (let i = kids.length - 1; i >= 0; i -= 1) stack.push(kids[i]);
  }
  return out;
}

// A variable's kind from its initializer: a function value's kind, a component
// for `const Name = memo(...)` and other wrappers, else `base`.
function variableKind(ts, name, initializer, base) {
  const init = initializer ? unwrap(ts, initializer) : undefined;
  if (isFunctionValue(ts, init)) return functionKind(ts, name, init);
  if (init && ts.isCallExpression(init) && isComponentName(name) && COMPONENT_WRAPPERS.has(calleeName(ts, init))) {
    return "component";
  }
  return base;
}

module.exports = { extractDecls };
