"use strict";
// JSX element facts for one parsed file (protocol 1, `elements`).
//
// Every JsxOpeningElement and JsxSelfClosingElement is one element. Closing
// tags and fragments are not elements: a fragment has no tag to query.

const { lineColumn, lineIndex, lineStarts, lineText, readmeFunctionName, truncate, wellFormed } = require("./common.cjs");

const WHITESPACE_RE = /\s+/g;
const SURROGATE_RE = /[\uD800-\uDFFF]/;

// Per-line facts shared by every element on a line, so a minified line with
// thousands of elements costs O(line) once rather than once per element.
function lineCache(sf, snippetMax) {
  const starts = lineStarts(sf);
  const lines = new Map();
  return (pos) => {
    const index = lineIndex(sf, pos);
    let info = lines.get(index);
    if (info === undefined) {
      const text = lineText(sf, index + 1);
      info = {
        start: starts[index],
        ascii: !SURROGATE_RE.test(text),
        snippet: wellFormed(truncate(text.trim(), snippetMax)),
      };
      lines.set(index, info);
    }
    // Without surrogates on the line, UTF-16 offsets are code points.
    const column = info.ascii ? pos - info.start + 1 : lineColumn(sf, pos)[1];
    return [index + 1, column, info.snippet];
  };
}

function isNamableFunction(ts, node) {
  return ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node);
}

// The README parent rule: the innermost named function whose span, from its
// parameter list to its end, holds the element. `named` is the stack of named
// function ancestors as [name, parameter-list position].
function parentName(named, pos) {
  for (let i = named.length - 1; i >= 0; i -= 1) {
    if (named[i][1] <= pos) return named[i][0];
  }
  return null;
}

function tagText(sf, opening) {
  return wellFormed(opening.tagName.getText(sf).replace(WHITESPACE_RE, ""));
}

function children(ts, node) {
  const kids = [];
  ts.forEachChild(node, (child) => {
    kids.push(child);
  });
  return kids;
}

// The value of a string-literal attribute (`path="/a"`, `path={"/a"}`,
// `path={`/a`}`), else undefined: a dynamic value is not a fact.
function stringAttributeValue(ts, attribute) {
  let value = attribute.initializer;
  if (value !== undefined && ts.isJsxExpression(value)) value = value.expression;
  return value !== undefined && ts.isStringLiteralLike(value) ? wellFormed(value.text) : undefined;
}

function stringAttributes(ts, sf, opening) {
  const out = {};
  for (const attribute of children(ts, opening.attributes)) {
    if (!ts.isJsxAttribute(attribute)) continue;
    const value = stringAttributeValue(ts, attribute);
    if (value !== undefined) out[wellFormed(attribute.name.getText(sf))] = value;
  }
  return out;
}

// Stack entry that opens the scope of an element's children.
class Enter {
  constructor(tag, line, column) {
    this.tag = tag;
    this.line = line;
    this.column = column;
  }
}

// extractJsx(ts, sf, ctx) -> [{tag, line, column, kind, parent, snippet, attributes?, ancestor?}]
//
// An element whose tag is in ctx.attributeTags also carries `attributes` (its
// string-literal attributes by name) and `ancestor` ({line, column} of the
// nearest enclosing element with the same tag, or null; the opening element's
// own attributes are not "inside" it).
//
// Iterative pre-order walk: the TypeScript parser accepts nesting deeper than
// a recursive walk of its tree could follow on Node's default stack.
function extractJsx(ts, sf, ctx) {
  const out = [];
  const named = [];
  const tagged = [];
  const EXIT = null;
  const EXIT_TAGGED = { exit: true };
  const position = lineCache(sf, ctx.snippetMax);
  const stack = [sf];
  while (stack.length > 0) {
    const node = stack.pop();
    if (node === EXIT) {
      named.pop();
      continue;
    }
    if (node === EXIT_TAGGED) {
      tagged.pop();
      continue;
    }
    if (node instanceof Enter) {
      tagged.push(node);
      continue;
    }
    if (isNamableFunction(ts, node)) {
      const name = readmeFunctionName(ts, node);
      if (name !== undefined) {
        named.push([name, node.parameters.pos]);
        stack.push(EXIT);
      }
    }
    const selfClose = ts.isJsxSelfClosingElement(node);
    if (selfClose || ts.isJsxOpeningElement(node)) {
      const start = node.getStart(sf);
      const [line, column, snippet] = position(start);
      const tag = tagText(sf, node);
      const element = {
        tag,
        line,
        column,
        kind: selfClose ? "self_close" : "open",
        parent: parentName(named, start),
        snippet,
      };
      if (ctx.attributeTags.has(tag)) {
        element.attributes = stringAttributes(ts, sf, node);
        const outer = tagged.findLast((entry) => entry.tag === tag);
        element.ancestor = outer === undefined ? null : { line: outer.line, column: outer.column };
      }
      out.push(element);
    }
    const kids = children(ts, node);
    const scoped = ts.isJsxElement(node) && ctx.attributeTags.has(tagText(sf, node.openingElement));
    for (let i = kids.length - 1; i >= 0; i -= 1) {
      const kid = kids[i];
      if (scoped && ts.isJsxClosingElement(kid)) {
        stack.push(kid, EXIT_TAGGED);
      } else if (scoped && ts.isJsxOpeningElement(kid)) {
        const [line, column] = position(kid.getStart(sf));
        stack.push(new Enter(tagText(sf, kid), line, column), kid);
      } else {
        stack.push(kid);
      }
    }
  }
  return out;
}

module.exports = { extractJsx };
