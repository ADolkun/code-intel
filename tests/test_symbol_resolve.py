"""Symbol resolution: TypeScript declarations from the compiler, the Python index, ranking.

TypeScript fixtures share one tree and one helper run per module
(``symbols``); the tree is also compared against ``oracles/decl_oracle.cjs``,
an independent implementation of the declaration rules. Python fixtures need
no toolchain.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from fixture_repo import FULL_CONFIG, MINIMAL_CONFIG, git_init, write

from code_intel import cli, code_paths, symbol_resolve, ts_bridge, ts_facts
from code_intel.config import parse_config
from code_intel.oracles import compare_facts
from code_intel.repo_files import RepoFilesError

pytestmark = pytest.mark.usefixtures("module_git")

DECL_ORACLE = Path(compare_facts.__file__).parent / "decl_oracle.cjs"
_PARSE_ERRORS: tuple[type[Exception], ...] = (SyntaxError, ValueError)
LAYOUT = symbol_resolve.SymbolLayout(
    "backend",
    "frontend",
    ("frontend/components/", "frontend/lib/", "frontend/hooks/", "backend/service_backend/"),
)


def _index(
    root: Path, facts: ts_facts.FactsCache, layout: symbol_resolve.SymbolLayout, **kwargs
) -> symbol_resolve.SymbolIndex:
    """An index over ``root`` made a git work tree: the walk covers the files git sees."""
    return symbol_resolve.SymbolIndex(git_init(root), facts, layout, **kwargs)


# ── Fixtures ─────────────────────────────────────────────────────────────

# Declaration corpora (corpus/, corpus2/): hard TypeScript declaration shapes, all
# checked against the oracle.
CORPUS_TS: dict[str, str | bytes] = {
    "corpus/c.jsx": (
        "export function JsxComp({ a }) {\n"
        "  return <div>{a}</div>;\n"
        "}\n"
        "const JsxArrow = ({ a }) => <p>{a}</p>;\n"
        "export default function () { return <div />; }\n"
        "var x, y;\n"
        "let z;\n"
    ),
    "corpus/e.ts": 'const beforeFF = 1;\nconst s = "a\u0085b";\nconst afterSep = 2;\n',
    "corpus/d.d.ts": (
        "declare global { interface Window { foo: string } }\n"
        'declare module "*.svg" { const content: string; export default content; }\n'
        "export declare function ambientFn(x: number): void;\n"
    ),
    "corpus/a.ts": (
        "export const\n"
        "  splitConst = 1;\n"
        "export function\n"
        "  splitFn() {}\n"
        "const { alpha, beta: renamedBeta, ...restObj } = obj;\n"
        "const [first, , third = 3, ...restArr] = arr;\n"
        "const {\n"
        "  multiA,\n"
        "  multiB,\n"
        "} = obj;\n"
        "type WithDefault<T = string> = T[];\n"
        "type Multi<\n"
        "  T,\n"
        "> = T;\n"
        "export default function () {}\n"
        "export default class DefaultClass {}\n"
        'declare module "virtual-mod" {\n'
        "  export function insideAmbient(): void;\n"
        "}\n"
        "namespace Ns { export const inNs = 1; }\n"
        "declare namespace DeclNs { const inDecl: number; }\n"
        "export function over(a: string): string;\n"
        "export function over(a: number): number;\n"
        "export function over(a: any) { return a; }\n"
        "export abstract class AbstractThing { abstract run(): void; }\n"
        "@decorator\n"
        "export class Decorated {}\n"
        "@decorator export class DecoratedInline {}\n"
        "const cfg = { a: 1 } satisfies Cfg;\n"
        "export const enum ConstEnum { A }\n"
        "declare enum DeclEnum { B }\n"
        "declare const declConst: number;\n"
        "declare function declFn(): void;\n"
        "let defAssign!: number;\n"
        "var v1, v2;\n"
        "let typedFn: (x: number) => void;\n"
        "export { splitConst as aliasConst };\n"
        'export * as nsExport from "./b";\n'
        "export default cfg;\n"
        "for (let loopI = 0; loopI < 3; loopI++) {}\n"
        "for (const loopItem of items) {}\n"
        "using res = getRes();\n"
        "function* gen() {}\n"
        "async function* agen() {}\n"
        "const anon = class extends Base {};\n"
        "export function BuildMap(): Map<string, number> { return new Map(); }\n"
        "const MAX_RETRIES =\n"
        "  3;\n"
        "const handlers = { onSave: (x) => x, onLoad: async (y) => y, other: function () {}, meth() {} };\n"
        "const t = cond ? a : (b);\n"
        "const o = { offset: (page - 1) * size };\n"
        "interface Props { onClose: (v: string) => void }\n"
        "class Klass { field = 1; method() {} static sm() {} get acc() { return 1; } onEvt: ((e: Event) => void) | null = null; }\n"
        "const re = /class Fake/;\n"
        "const tpl = `${(() => { const inTpl = 1; return inTpl; })()}`;\n"
        "let a = 1, b\n"
        "  = 2;\n"
        "let\n"
        "  nextLineLet = 1;\n"
        "const isType = 1; type\n"
        "Foo = number;\n"
    ),
    "corpus/b.tsx": (
        'import React, { memo, forwardRef } from "react";\n'
        "const MemoComp = memo(function Inner() { return <div />; });\n"
        "const MemoArrow = React.memo(() => <div />);\n"
        "const FwdComp = forwardRef<HTMLDivElement, Props>((props, ref) => <div ref={ref} />);\n"
        "const FwdNs = ReactMod.forwardRef((p, r) => null);\n"
        "export default function () { return <div />; }\n"
        "export default function NamedDefault() { return <div />; }\n"
        "export function LongComponent({\n"
        "  a,\n  b,\n  c,\n  d,\n  e,\n  f,\n  g,\n  h,\n"
        "}: Props) {\n"
        "  const x = 1;\n"
        "  const y = 2;\n"
        "  return <div>{a}</div>;\n"
        "}\n"
        "function Guarded({ a }: Props) {\n"
        "  if (!a) {\n"
        "    return null;\n"
        "  }\n"
        "  const one = 1;\n  const two = 2;\n  const three = 3;\n  const four = 4;\n  const five = 5;\n  const six = 6;\n"
        "  return (\n"
        "    <div />\n"
        "  );\n"
        "}\n"
        "const SCREAMING_FN = (x: number) => x * 2;\n"
        "const LABEL =\n"
        '  "text";\n'
        "const Styled = styled.div`color: red;`;\n"
        "const Ctx = createContext<Value | null>(null);\n"
        "function useThing<T,>(v: T) { return v; }\n"
        "const Generic = <T,>(p: P<T>) => <div />;\n"
        "export const Satisfied = (() => <div />) satisfies FC;\n"
        "function helper() { return <span />; }\n"
        "function Render(): JSX.Element {\n"
        "  return <p>class Fake in text and const fake = 1</p>;\n"
        "}\n"
    ),
    "corpus2/f.tsx": (
        "function FormatValue(v: number) { return String(v); }\n"
        "const ToLabel = (v: number) => String(v);\n"
        "export function Row() { return <div />; }\n"
        "const Ctx = createContext(null);\n"
        "const Upper = (x: string) => x.toUpperCase();\n"
    ),
    # corpus3/frontend/bad.ts: a UTF-16 byte-order mark and nothing else.
    "corpus3/bad.ts": b"\xff\xfe",
    "corpus3/utf16be.ts": "\ufeffexport function Utf16Big() {}\n".encode("utf-16-be"),
    "corpus3/utf16le.ts": "\ufeffexport const Utf16Little = 1;\n".encode("utf-16-le"),
}

BEHAVIOUR_TS: dict[str, str | bytes] = {
    "app/components/DataTable.tsx": (
        "export function DataTable({ columns }: { columns: string[] }) {\n"
        "  return (\n"
        "    <table>\n"
        "      <tbody />\n"
        "    </table>\n"
        "  );\n"
        "}\n"
        "\n"
        "export const Helper = () => <span />;\n"
    ),
    "app/lib/types.ts": (
        "export interface Appointment {\n"
        "  id: string;\n"
        "}\n"
        "\n"
        'export type Status = "active" | "inactive";\n'
        "\n"
        "function internalHelper() {\n"
        "  return 1;\n"
        "}\n"
    ),
    "app/node_modules/ignored/DataTable.tsx": "export function DataTable() {\n  return null;\n}\n",
    "app/.next/server/DataTable.tsx": "export function DataTable() {\n  return null;\n}\n",
    "words/util.ts": (
        "// Helper functions for the page; functionality is limited.\n"
        "export function plain() {}\n"
        "export async function* stream() {}\n"
        "function*gen() {}\n"
    ),
    "vars/auth.ts": (
        "function decode() {\n"
        "  let decodedToken;\n"
        "  var first, second;\n"
        "  let tail\n"
        "  // let commented;\n"
        '  const s = "let quoted;";\n'
        "  let m: Map<K, V> = new Map(f(x, y)), last;\n"
        "  const one = 1, two = () => 2;\n"
        "}\n"
    ),
    "echo/EchoIntegration.dev.jsx": (
        "export function EchoIntegration({ principalId }) {\n"
        "  return <div data-id={principalId} />;\n"
        "}\n"
        "export const Panel = () => <p>let notAVariable;</p>;\n"
    ),
    "echo/EchoIntegration.tsx": "export function EchoIntegration() {}\n",
    "echo/EchoGuarded.dev.jsx": (
        "export function EchoGuarded({ principalId }) {\n"
        "  const [mounted, setMounted] = useState(false);\n"
        "  if (!mounted) return null;\n"
        '  return <Echo options={options(principalId, "dark")} />;\n'
        "}\n"
    ),
    "kinds/auth.ts": (
        "export enum AccessRole {\n"
        '  User = "SERVICE:Users",\n'
        "}\n"
        "const enum Flag { A }\n"
        "export type Paged<T> = { items: T[] };\n"
        "export interface Props { id: string }\n"
        "let counter = 0;\n"
        'export const apiBase: string = "/api";\n'
        "export abstract class Store {}\n"
    ),
    "kinds/ctx.tsx": (
        "export const MeContext = createContext<Me | null>(null);\n"
        "export const Labels: Record<string, string> = {};\n"
        "export const Card = ({ id }: Props) => <div>{id}</div>;\n"
        "export const Memo = React.memo(function Memo() { return null; });\n"
    ),
    "prose/notes.tsx": (
        "// Pick the enum to use; this class handles retries.\n"
        "/* interface shown in the picker */\n"
        'export const hint = "Choose a class to edit";\n'
        "export const Label = () => <p title={`enum ${x}`}>Pick one</p>;\n"
        "export enum Mode { A } // const enum Flag\n"
    ),
    "prose/prose.ts": (
        "// type Fake = string\n"
        "/* interface Fake2 {} */\n"
        "const s = 'let Fake3 = 1; var Fake4 = 2';\n"
        "const t = `enum Fake5 {} function Fake6() {} class Fake7 {}`;\n"
    ),
    "prose/jsx-text.tsx": (
        "export const Help = () => (\n"
        "  <p>\n"
        "    a class Foo or a class Bar, then type Baz = 1\n"
        "    {(() => { const Inner = 1; return Inner; })()}\n"
        "  </p>\n"
        ");\n"
    ),
    "gaps/backtick.tsx": (
        "export const Help = () => <p>Wrap code in ` backticks</p>;\n"
        "export function Lost() {}\n"
        "export const AlsoLost = 1;\n"
    ),
    "gaps/glob.tsx": (
        "export const Glob = () => <p>Matches src/*.ts</p>;\nexport function GlobLost() {}\nexport const GlobAlsoLost = 1;\n"
    ),
    "gaps/phantoms.tsx": (
        "/*\nconst Phantom = 1;\n*/\nconst sql = `\nconst Fake: number = 1\n`;\nexport const Real = () => <p>{sql}</p>;\n"
    ),
    "gaps/multiline.tsx": (
        "export const Help = () => (\n"
        "  <p>\n"
        "    Wrap code in ` backticks\n"
        "  </p>\n"
        ");\n"
        "export const Real = 1;\n"
        "export const Glob = () => (\n"
        "  <div>\n"
        "    <b>Files</b> {count}, matching\n"
        "    src/*.ts\n"
        "  </div>\n"
        ");\n"
        "export function realToo() {}\n"
        "export default () => <p>It's</p>; export const answer = 'ok';\n"
    ),
    "gaps/near_jsx.tsx": (
        "const el = <Foo />;\n"
        "const sql = `\n"
        "const AfterElement = 1\n"
        "`;\n"
        "function Page() {\n"
        "  return <div>hi</div>;\n"
        "}\n"
        "/*\n"
        "export const AfterFunction = 1;\n"
        "*/\n"
        "const a = <div className={`\n"
        "const InAttribute = 1\n"
        "`}>{/*\n"
        "const InChild = 1\n"
        "*/}</div>;\n"
    ),
    "gaps/cast.ts": "const s = <Foo>`\nconst InCast = 1\n`;\n",
    "gaps/generics.tsx": (
        "export const pick = <T extends object>(\n"
        "  // the function to call when the class changes\n"
        "  value: T,\n"
        ") => value;\n"
        'const f = <T,>(a = "const Fake = 1", b: T): "x" | "enum Lit" => a;\n'
        'type Fn = <T,>(m: "interface Shown") => void;\n'
        "function useRows() {\n"
        "  const e = <div />\n"
        "  return useQuery<Row>(`\n"
        "const M = 1\n"
        "`)\n"
        "}\n"
        "const g = <div />\n"
        "class A extends B<C> {}\n"
        'foo<Bar>("x", /* const P = 1 */ 2)\n'
        "export const updateFilter = <K extends keyof F>(key: K, value: F[K]) => {\n"
        "  setFilters((prev) => ({ ...prev, [key]: value }));\n"
        "};\n"
    ),
    "gaps/steps.tsx": (
        "export const Steps = () => (\n  <ul>\n"
        + '    <li>Click "Save N" to continue</li>\n' * 40
        + "    <li>Matches src/*.ts</li>\n  </ul>\n);\nexport const Real = 1;\n"
    ),
    "gaps/terms.tsx": (
        "export const Terms = () => (\n  <p>\n"
        + "".join(f"    Clause {n} applies.<br />\n" for n in range(600))
        + "    See https://example.com/terms\n  </p>\n);\nexport const Late = 1;\n"
    ),
    "gaps/text.tsx": (
        "const a = <p>Files (glob)<code>src/*.ts</code> files</p>;\n"
        "export const AfterParen = 1;\n"
        "const b = <p>[1]<a>http://example.com</a></p>;\n"
        "export const AfterBracket = 1;\n"
        'const c = <Foo extends="x">say "hi" `tick</Foo>;\n'
        "export const AfterExtends = 1;\n"
        "export const pick = <const T,>(\n"
        "  // const Phantom = 1\n"
        "  v: T,\n"
        ") => v;\n"
        "export const withDefault = <T = unknown>(\n"
        "  /* const Phantom2 = 1 */\n"
        "  v: T,\n"
        ") => v;\n"
    ),
    "gaps/smile.tsx": (
        "export function Smile() {\n"
        "  return <p>Smile :( it</p>;\n"
        "}\n"
        "// const Phantom = 1\n"
        "export function Draft() {\n"
        "  return <p>[draft it</p>;\n"
        "}\n"
        "/* const Phantom2 = 1 */\n"
        'const t = <p>{`a ${<b>x</b>} c`} and "z" `tick</p>;\n'
        "export const AfterTemplate = 1;\n"
        "const u = <p>{n} items ` left</p>;\n"
        "export const AfterContainer = 1;\n"
    ),
    "gaps/render.tsx": (
        "type Render = <Row>(row: Row) => string;\n"
        "export function helper() { return 1; }\n"
        "export const MAX_ROWS = 5;\n"
        "export const View = () => <Row>x</Row>;\n"
    ),
    "oneline/one_line.tsx": (
        "const View = () => <p>It's</p>; const answer = 'ok';\n"
        "export type A = string; export type B = number;\n"
        "let first = 1; let second = 2;\n"
        "const Card = () => <p>Hi</p>; const MAX_ROWS = 5;\n"
    ),
    "oneline/long.ts": "; ".join(f"const c{i} = 1" for i in range(8000))
    + "; export default async function* gen() {}\n",
    # Type parameters (with defaults) declare nothing.
    "type_params/generics.tsx": (
        "export function pickPair<T, U = string>(a: T, b?: U): [T, U] { return [a, b as U]; }\n"
        "export type Pair<T, U = string> = [T, U];\n"
        "export interface Box<T, U = string> { value: T; label: U }\n"
        "export class Holder<T, U = string> { constructor(public t: T) {} }\n"
        "export const mapPair = <T, U = string>(a: T): U => a as unknown as U;\n"
        "type Fn = <T, U = string>(a: T) => U;\n"
    ),
    # Component classification reads the declaration's own body.
    "component_body/f.tsx": (
        "function FormatValue(v: number) { return String(v); }\n"
        "export function Row() { return <div />; }\n"
        "function BuildColumns() {\n"
        "  return items.map((item) => <Cell key={item} />);\n"
        "}\n"
        "export function Header() {\n"
        "  return null;\n"
        "}\n"
        "const Toolbar = () => (\n"
        "  <div />\n"
        ");\n"
        "const MakeRenderer = (x: number) => (y: number) => <span />;\n"
        "export const MAX_WIDTH = () => <div />;\n"
    ),
    "component_body/generic.ts": (
        "export function Identity<T>(v: T): T { return <T>v; }\nexport const Cast = (v: unknown) => <string>v;\n"
    ),
    # Members of interfaces, types and parameters, `key: (expr)` and
    # ternaries are not declarations; object-literal function values are.
    "object_members/members.tsx": (
        "interface Props { onClose: (v: string) => void; render(): void }\n"
        "type Handlers = { onSave: (x: number) => void };\n"
        "function useThing(opts: { onPick: (v: string) => void }) { return opts; }\n"
        "const layout = { offset: (page - 1) * size, flag: cond ? (a) : (b) };\n"
        "export const api = {\n"
        "  fetchMe: () => request('/me'),\n"
        "  'quoted-key': async function () { return 1; },\n"
        "  nested: { deep: (x: number) => x },\n"
        "  Mock: () => <div />,\n"
        "  method() { return 1; },\n"
        "};\n"
        "vi.mock('x', () => ({ inMock: () => null }));\n"
    ),
    "class_members/members.ts": (
        "export class Service {\n"
        "  private count = 0;\n"
        "  handle = (e: Event) => e;\n"
        "  constructor(private readonly dep: Dep) {}\n"
        "  run(): void {}\n"
        "  get size() { return this.count; }\n"
        "  #secret() {}\n"
        "  load(a: string): void;\n"
        "  load(a: unknown) {}\n"
        "}\n"
        "export class Widget extends Base<Props> implements Thing {}\n"
    ),
    "class_expressions/expr.ts": (
        "window.ResizeObserver = class ResizeObserver { observe() {} };\n"
        "const Holder = class Held {};\n"
        "export const Exported = class ExportedImpl {};\n"
        "register(class Registered {});\n"
        "const fn = function named() {};\n"
    ),
    "class_expressions/real.ts": "export class ResizeObserver {}\n",
    "namespaces/exports.ts": (
        "namespace Outer.Inner { export const deep = 1; }\n"
        "export { localA, localB as renamedB };\n"
        "const localA = 1;\n"
        "function localB() {}\n"
        "const notExported = 2;\n"
        'export * from "./x";\n'
        "export default 42;\n"
    ),
    "ranking/components/ui/data-table.tsx": "export const DataTable = memo(DataTableInner);\n",
    "ranking/lib/table.ts": "export function useTable() {}\n",
    "ranking/hooks/useTable.ts": "export function useTable() {}\n",
    "ranking/test/mocks/data-table-shim.tsx": "export const shim = { DataTable: () => <div /> };\n",
    "ranking/components/__tests__/Page.test.tsx": "vi.mock('x', () => ({ DataTable: () => <div /> }));\n",
}

# Edge corpus (edges/): declaration shapes, each also checked against the oracle.
EDGE_TS = {
    "edges/overloads.ts": "function f1(a: string): void;\nfunction f1(a: number): void;\nfunction f1(a: any) {}\n",
    "edges/declare_overloads.ts": "declare function g2(): void;\ndeclare function g2(a: string): void;\n",
    "edges/export_default.ts": "export default Foo3;\nfunction Foo3() { return 1; }\nconst Bar3 = 1;\nexport = Bar3;\n",
    "edges/members.ts": (
        "abstract class C4 {\n  static #priv4() {}\n  get x4() { return 1; }\n  set x4(v) {}\n  constructor() {}\n"
        "  field4 = 1;\n  arrow4 = () => 1;\n  abstract m4(): void;\n  o4(a: string): void;\n  o4(a: any) {}\n}\n"
    ),
    "edges/destructuring.ts": "const { a5 = 1, b5: { c5 }, ...rest5 } = obj;\nconst [x5, , y5 = 2, ...zs5] = arr;\n",
    "edges/using.ts": (
        "async function h6() {\n  using r6 = res();\n  await using q6 = res();\n  for await (const z6 of zs) {}\n}\n"
    ),
    "edges/enums.ts": "const enum E7 { A }\ndeclare enum F7 { B }\nenum G7 { C }\n",
    "edges/reexports.ts": (
        "export { a8 as b8 } from './x';\nexport * as ns8 from './y';\nconst local8 = 1;\nexport { local8 as renamed8 };\n"
    ),
    "edges/namespaces.ts": (
        "namespace A9.B9 { export const x9 = 1; }\ndeclare global { interface Window9 {} }\n"
        "declare module 'm9' { function k9(): void; }\nmodule Legacy9 {}\n"
        "namespace Outer17 { namespace Inner17 { export const y17 = 1; } }\n"
    ),
    "edges/u2028.ts": "const s18 = 'a\u2028b';\nexport const after18 = 1;\n",
}
TS_FIXTURES = {**CORPUS_TS, **BEHAVIOUR_TS, **EDGE_TS}

# Python declaration corpus (corpus/): hard binding, line-break and decoding shapes.
CORPUS_PY: dict[str, str | bytes] = {
    "corpus/aw.py": "import x\nasync with lock:\n    ASYNC_WITH = 1\n",
    "corpus/bad.py": b"\xff\xfe bad",
    "corpus/ff.py": 'x = "a\x0cb"\ndef after_ff():\n    pass\n',
    "corpus/ff2.py": "x = 1\n\x0c\ndef after_form_feed_line():\n    pass\n",
    "corpus/m.py": (
        "from typing import TYPE_CHECKING, Annotated, TypeAlias, TypeVar, Final, Optional, Sequence\n"
        "import typing as t\n"
        "if TYPE_CHECKING:\n"
        "    from x import Y\n"
        "    class OnlyForTypes: ...\n"
        "try:\n"
        "    from fast import loads\n"
        "except ImportError:\n"
        "    def loads(s): return s\n"
        'DbDep = Annotated[int, "dep"]\n'
        'Qualified = t.Annotated[int, "dep"]\n'
        'Explicit: TypeAlias = "int | None"\n'
        "QualExplicit: t.TypeAlias = int\n"
        "type Pep695[T] = list[T]\n"
        "Pep604 = int | None\n"
        "Pep585 = dict[str, list[int]]\n"
        "AbcAlias = Sequence[int]\n"
        'T = TypeVar("T")\n'
        "MAX: Final = 3\n"
        "Opt = Optional[int]\n"
        '__all__ = ["loads"]\n'
        "a = b = 1\n"
        "(c, [d, *e]) = 1, [2, 3]\n"
        "obj.attr = 5\n"
        "counter += 1\n"
        "if (walrus := 3): pass\n"
        "for loop_var in range(3):\n"
        "    LOOP_DEF = loop_var\n"
        "while False:\n"
        "    def in_while(): ...\n"
        "match 1:\n"
        "    case 1:\n"
        "        def in_match(): ...\n"
        "async def amain():\n"
        "    async with ctx() as c2:\n"
        "        pass\n"
        "class Outer:\n"
        "    attr: int = 1\n"
        "    CONST = 2\n"
        "    class Inner:\n"
        "        def inner_method(self): ...\n"
        "    if TYPE_CHECKING:\n"
        "        def guarded_method(self): ...\n"
        "    @property\n"
        "    def prop(self): ...\n"
        "    @prop.setter\n"
        "    def prop(self, v): ...\n"
        "@decorator\n"
        "@other(\n"
        "    arg=1,\n"
        ")\n"
        "async def decorated(\n"
        "    x: int,\n"
        ") -> None:\n"
        "    def nested(): ...\n"
        "    class LocalCls: ...\n"
        "def f(): pass; g = 1\n"
        "class E(Enum):\n"
        "    RED = 1\n"
        "class P(t.Protocol):\n"
        "    def m(self) -> int: ...\n"
    ),
}

BEHAVIOUR_PY: dict[str, str | bytes] = {
    "service_backend/service.py": (
        "class Appointment:\n"
        "    pass\n"
        "\n"
        "async def list_articles(db):\n"
        "    pass\n"
        "\n"
        "def get_current_user():\n"
        "    pass\n"
        "\n"
        "class SquadService:\n"
        "    def list_articles(self):\n"
        "        pass\n"
    ),
    "service_backend/routes.py": (
        "from typing import Annotated, Final, TYPE_CHECKING, TypeAlias\n"
        "\n"
        'router = APIRouter(tags=["x"])\n'
        "DbDep = Annotated[Session, Depends(get_db)]\n"
        '_require_list_access = require_any_permission("x.view")\n'
        "MAX_PAGE = 100\n"
        "limit: Final = 10\n"
        "timeout: int = 30\n"
        "Pair: TypeAlias = tuple[int, int]\n"
        "type Alias = int\n"
        "first, second = 1, 2\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    hidden = None\n"
        "else:\n"
        "    def hidden_fn():\n"
        "        pass\n"
        "\n"
        "class Model:\n"
        '    field_name: str = "x"\n'
        "\n"
        "def handler():\n"
        "    local_only = 1\n"
    ),
    "service_backend/enums.py": (
        "from enum import Enum, IntEnum\n"
        "class Color(str, Enum):\n"
        "    RED = 'red'\n"
        "class Level(IntEnum):\n"
        "    LOW = 1\n"
        "class Shade(Color):\n"
        "    pass\n"
        "class Reader(Protocol[T]):\n"
        "    def read(self) -> T: ...\n"
        "Status = Literal['a', 'b'] | None\n"
        "Ids = list[int]\n"
        "UserId = NewType('UserId', int)\n"
        "P = ParamSpec('P')\n"
        "Plain = some_call()\n"
        "Number = int | float\n"
        "Mask = FLAG_A | FLAG_B\n"
        "_bindable = {n for n in names if (m := match(n))}\n"
    ),
    "tests/test_service.py": "def get_current_user():\n    pass\n",
    ".venv/lib/fake.py": "def get_current_user():\n    pass\n",
    "service_backend/unicode.py": "LABEL = 'ä'; Ünïcode = 1\n",
}

PY_FIXTURES = {**CORPUS_PY, **BEHAVIOUR_PY}


def _write_tree(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, bytes):
            path.write_bytes(source)
        else:
            path.write_text(source, encoding="utf-8", newline="")


@dataclass
class Repo:
    root: Path
    index: symbol_resolve.SymbolIndex
    toolchain: ts_bridge.Toolchain | None

    def resolve(self, name: str, **kwargs) -> symbol_resolve.SymbolResult:
        return self.index.resolve(name, **kwargs)

    def found(self, name: str, prefix: str = "", *fields: str, **kwargs) -> list[tuple]:
        fields = fields or ("kind", "start_line")
        return [
            tuple(getattr(c, f) for f in fields)
            for c in self.resolve(name, **kwargs).candidates
            if c.file.startswith(prefix)
        ]


@pytest.fixture(scope="module")
def symbols(typescript_toolchain: ts_bridge.Toolchain, tmp_path_factory: pytest.TempPathFactory) -> Repo:
    root = tmp_path_factory.mktemp("symbols_repo")
    _write_tree(root / "frontend", TS_FIXTURES)
    _write_tree(root / "backend", PY_FIXTURES)
    facts = ts_facts.FactsCache(root, lambda: typescript_toolchain)
    repo = Repo(root, _index(root, facts, LAYOUT), typescript_toolchain)
    repo.resolve("prime")
    assert facts.stats()["helper_runs"] == 1
    return repo


def _unavailable_toolchain() -> ts_bridge.Toolchain:
    problem = ts_bridge.ToolchainProblem(
        "typescript_not_found",
        "tools/code-intel/ts/node_modules/typescript/package.json not found",
        "tools/code-intel/ts/node_modules/typescript/package.json",
        ts_bridge.FRONTEND_TS_FIX,
    )
    return ts_bridge.Toolchain("/node", "v24", None, None, None, None, problem, ())


@pytest.fixture
def python_repo(tmp_path: Path) -> Repo:
    _write_tree(tmp_path / "backend", PY_FIXTURES)
    # frontend/ exists, so the missing toolchain is what the frontend side reports.
    (tmp_path / "frontend").mkdir()
    toolchain = _unavailable_toolchain()
    facts = ts_facts.FactsCache(tmp_path, lambda: toolchain)
    return Repo(tmp_path, _index(tmp_path, facts, LAYOUT), None)


# ── Oracle ───────────────────────────────────────────────────────────────


def test_ts_declarations_equal_the_declaration_oracle(symbols: Repo, tmp_path: Path) -> None:
    """Every fixture, the declaration corpora included, equals oracles/decl_oracle.cjs
    in name, line, column, kind, end line, scope, container, exported and test flag."""
    out = tmp_path / "decls.json"
    toolchain = symbols.toolchain
    assert toolchain is not None and toolchain.node is not None
    subprocess.run(
        [
            toolchain.node,
            str(DECL_ORACLE),
            str(toolchain.typescript_dir),
            str(symbols.root / "frontend"),
            str(out),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    oracle = json.loads(out.read_text(encoding="utf-8"))
    problems: list = []
    tool = [
        {"file": rel, **vars(decl), "is_test": code_paths.is_test_path(rel)}
        for rel, decl in symbols.index.typescript_decls(problems)
    ]
    result = compare_facts.compare(
        [compare_facts.decl_row(r) for r in oracle["decls"]],
        [compare_facts.decl_row(r) for r in tool],
        ("file", "line", "column", "name"),
        compare_facts.DECL_FIELDS,
    )
    assert (result["missing"], result["extra"], result["mismatches"]) == ([], [], {})
    assert result["tool"] > 8000


# ── TypeScript: corpus and declaration forms ──────────────────────────────


def _decls(symbols: Repo, rel: str) -> list[tuple]:
    problems: list = []
    return [
        (d.name, d.kind, d.line, d.scope, d.container, d.exported)
        for r, d in symbols.index.typescript_decls(problems)
        if r == f"frontend/{rel}"
    ]


def test_corpus_a_declarations(symbols: Repo) -> None:
    """Declaration forms in corpus a.ts: split lines, destructuring, definite
    assignment, `for` headers, type parameter defaults and namespaces."""
    names = {(n, k) for n, k, *_ in _decls(symbols, "corpus/a.ts")}
    expected_present = {
        ("splitConst", "const"),  # keyword and name on different lines
        ("splitFn", "function"),
        ("alpha", "const"),  # destructuring
        ("renamedBeta", "const"),
        ("restObj", "const"),
        ("first", "const"),
        ("third", "const"),
        ("restArr", "const"),
        ("multiA", "const"),
        ("multiB", "const"),
        ("WithDefault", "type_alias"),  # type parameter default
        ("Multi", "type_alias"),
        ("DefaultClass", "class"),
        ("insideAmbient", "function"),
        ("Ns", "namespace"),  # namespaces are a kind
        ("inNs", "const"),
        ("DeclNs", "namespace"),
        ("inDecl", "const"),
        ("over", "function"),
        ("AbstractThing", "class"),
        ("run", "method"),
        ("Decorated", "class"),
        ("DecoratedInline", "class"),
        ("ConstEnum", "enum"),
        ("DeclEnum", "enum"),
        ("declConst", "const"),
        ("declFn", "function"),
        ("defAssign", "variable"),  # let x!: T
        ("v1", "variable"),
        ("v2", "variable"),
        ("typedFn", "variable"),
        ("loopI", "variable"),  # for headers
        ("loopItem", "const"),
        ("res", "variable"),
        ("gen", "function"),
        ("agen", "function"),
        ("anon", "const"),
        ("BuildMap", "function"),
        ("MAX_RETRIES", "const"),
        ("onSave", "function"),
        ("onLoad", "function"),
        ("other", "function"),
        ("meth", "function"),
        ("Klass", "class"),
        ("method", "method"),
        ("sm", "method"),
        ("acc", "method"),
        ("inTpl", "const"),
        ("a", "variable"),
        ("b", "variable"),
        ("nextLineLet", "variable"),
        ("isType", "const"),
    }
    assert expected_present <= names
    # `extends` is never a name; specifiers, `export * as`,
    # `export default <expr>`, anonymous defaults and string-named modules are not
    # definitions; class fields without a function value and type members are not either.
    for absent in (
        "extends",
        "aliasConst",
        "nsExport",
        "default",
        "virtual-mod",
        "field",
        "onEvt",
        "onClose",
        "Foo",
        "Fake",
    ):
        assert absent not in {n for n, *_ in _decls(symbols, "corpus/a.ts")}, absent
    over = [d for d in _decls(symbols, "corpus/a.ts") if d[0] == "over"]
    assert over == [("over", "function", 24, "module", None, True)], "overload signatures are not definitions"
    assert ("inNs", "const", 20, "namespace", "Ns", True) in _decls(symbols, "corpus/a.ts")
    assert ("splitConst", "const", 2, "module", None, True) in _decls(symbols, "corpus/a.ts")


def test_corpus_b_components_and_corpus_c_jsx(symbols: Repo) -> None:
    kinds = {n: k for n, k, *_ in _decls(symbols, "corpus/b.tsx")}
    assert {
        n: kinds[n] for n in ("MemoComp", "MemoArrow", "FwdComp", "FwdNs", "NamedDefault", "LongComponent", "Guarded")
    } == {
        n: "component"
        for n in ("MemoComp", "MemoArrow", "FwdComp", "FwdNs", "NamedDefault", "LongComponent", "Guarded")
    }
    assert {n: kinds[n] for n in ("Generic", "Satisfied", "Render")} == {
        "Generic": "component",
        "Satisfied": "component",
        "Render": "component",
    }
    assert {n: kinds[n] for n in ("SCREAMING_FN", "useThing", "helper")} == {
        "SCREAMING_FN": "function",
        "useThing": "function",
        "helper": "function",
    }
    assert {n: kinds[n] for n in ("LABEL", "Styled", "Ctx")} == {"LABEL": "const", "Styled": "const", "Ctx": "const"}
    assert "Fake" not in kinds and "fake" not in kinds
    assert _decls(symbols, "corpus/c.jsx") == [
        ("JsxComp", "component", 1, "module", None, True),
        ("JsxArrow", "component", 4, "module", None, False),
        ("x", "variable", 6, "module", None, False),
        ("y", "variable", 6, "module", None, False),
        ("z", "variable", 7, "module", None, False),
    ]


def test_corpus_d_ts_ambient_declarations(symbols: Repo) -> None:
    """`declare global` augments the global scope and `declare module "x"` names a
    module: neither is a namespace definition, but their members are indexed."""
    assert _decls(symbols, "corpus/d.d.ts") == [
        ("Window", "interface", 1, "namespace", "global", False),
        ("content", "const", 2, "namespace", "*.svg", False),
        ("ambientFn", "function", 3, "module", None, True),
    ]


def test_typescript_line_numbers_follow_typescript_line_terminators(symbols: Repo) -> None:
    """U+0085 is not a line terminator for TypeScript, so later lines keep their numbers."""
    assert [(n, line) for n, _k, line, *_ in _decls(symbols, "corpus/e.ts")] == [
        ("beforeFF", 1),
        ("s", 2),
        ("afterSep", 3),
    ]
    assert _decls(symbols, "corpus3/bad.ts") == []


def test_type_parameter_defaults_declare_nothing(symbols: Repo) -> None:
    """`<T, U = string>` in functions, types, interfaces, classes and
    arrows declares neither T nor U."""
    names = [n for n, *_ in _decls(symbols, "type_params/generics.tsx")]
    assert names == ["pickPair", "Pair", "Box", "Holder", "mapPair", "Fn"]
    assert symbols.found("U", "frontend/type_params") == []


def test_nearby_jsx_return_does_not_make_a_plain_function_a_component(symbols: Repo) -> None:
    """A function is a component only when JSX sits in its own body
    (outside nested functions) and its name is PascalCase with a lowercase letter."""
    assert {n: k for n, k, *_ in _decls(symbols, "component_body/f.tsx")} == {
        "FormatValue": "function",  # the next line's `return <div />` is Row's
        "Row": "component",
        "BuildColumns": "function",  # JSX only inside the nested callback
        "Header": "function",
        "Toolbar": "component",
        "MakeRenderer": "function",  # its body is a function; the JSX is that function's
        "MAX_WIDTH": "function",  # SCREAMING_CASE never counts
    }
    # In a .ts file `<T>v` is a type assertion, never JSX.
    assert {n: k for n, k, *_ in _decls(symbols, "component_body/generic.ts")} == {
        "Identity": "function",
        "Cast": "function",
    }
    assert symbols.found("FormatValue", "frontend/corpus2") == [("function", 1)]
    assert symbols.found("Row", "frontend/corpus2") == [("component", 3)]
    assert symbols.found("ToLabel", "frontend/corpus2") == [("function", 2)]


def test_only_object_literal_function_values_are_declarations(symbols: Repo) -> None:
    rows = _decls(symbols, "object_members/members.tsx")
    assert [(n, k, s, c) for n, k, _line, s, c, _e in rows] == [
        ("Props", "interface", "module", None),
        ("Handlers", "type_alias", "module", None),
        ("useThing", "function", "module", None),
        ("layout", "const", "module", None),
        ("api", "const", "module", None),
        ("fetchMe", "function", "object", "api"),
        ("quoted-key", "function", "object", "api"),
        ("deep", "function", "object", "nested"),
        ("Mock", "component", "object", "api"),
        ("method", "function", "object", "api"),
        ("inMock", "function", "object", None),
    ]


def test_class_members_are_methods_and_kind_method_returns_typescript(symbols: Repo) -> None:
    rows = _decls(symbols, "class_members/members.ts")
    assert [(n, k, s, c) for n, k, _l, s, c, _e in rows] == [
        ("Service", "class", "module", None),
        ("handle", "method", "class", "Service"),
        ("run", "method", "class", "Service"),
        ("size", "method", "class", "Service"),
        ("#secret", "method", "class", "Service"),
        ("load", "method", "class", "Service"),
        ("Widget", "class", "module", None),
    ]
    assert symbols.found("run", "frontend/class_members", "kind", "language", kind_filter="method") == [
        ("method", "typescript")
    ]


def test_class_expression_names_are_local_and_rank_below_module_classes(symbols: Repo) -> None:
    assert _decls(symbols, "class_expressions/expr.ts") == [
        ("ResizeObserver", "class", 1, "local", None, False),
        ("observe", "method", 1, "class", "ResizeObserver", False),
        ("Holder", "const", 2, "module", None, False),
        ("Held", "class", 2, "local", None, False),
        ("Exported", "const", 3, "module", None, True),
        ("ExportedImpl", "class", 3, "local", None, False),
        ("Registered", "class", 4, "local", None, False),
        ("fn", "function", 5, "module", None, False),
    ]
    ranked = [
        (c.file, c.scope, c.score)
        for c in symbols.resolve("ResizeObserver", side="frontend").candidates
        if c.file.startswith("frontend/class_expressions")
    ]
    assert ranked == [
        ("frontend/class_expressions/real.ts", "module", 0.4),
        ("frontend/class_expressions/expr.ts", "local", pytest.approx(0.3)),
    ]
    assert symbols.found("named", "frontend/class_expressions") == []


def test_namespaces_and_local_export_lists(symbols: Repo) -> None:
    assert _decls(symbols, "namespaces/exports.ts") == [
        ("Outer", "namespace", 1, "module", None, False),
        ("Inner", "namespace", 1, "namespace", "Outer", False),
        ("deep", "const", 1, "namespace", "Outer.Inner", True),
        ("localA", "const", 3, "module", None, True),
        ("localB", "function", 4, "module", None, True),
        ("notExported", "const", 5, "module", None, False),
    ]
    assert symbols.found("Outer", "frontend/namespaces", kind_filter="namespace") == [("namespace", 1)]


def test_typescript_candidates_carry_end_line_scope_and_header_signature(symbols: Repo) -> None:
    """TS candidates have an end line, and the signature is the header, not the raw line."""
    [table] = [
        c
        for c in symbols.resolve("DataTable", side="frontend", file_hint="app/components").candidates
        if c.score == 1.0
    ]
    assert (table.kind, table.start_line, table.end_line, table.scope, table.exported) == (
        "component",
        1,
        7,
        "module",
        True,
    )
    assert table.signature == "export function DataTable({ columns }: { columns: string[] })"
    [mode] = symbols.resolve("Mode", side="frontend").candidates
    assert mode.signature == "export enum Mode"


# ── TypeScript: behaviour ──────────────────────────────────────────────


def test_resolve_typescript_component_function_const_interface_type(symbols: Repo) -> None:
    assert symbols.found("DataTable", "frontend/app", "kind", "file", side="frontend") == [
        ("component", "frontend/app/components/DataTable.tsx")
    ]
    assert symbols.found("Appointment", "frontend/app") == [("interface", 1)]
    assert symbols.found("Status", "frontend/app") == [("type_alias", 5)]
    assert symbols.found("Helper", "frontend/app") == [("component", 9)]
    assert symbols.found("internalHelper", "frontend/app", kind_filter="function") == [("function", 7)]


def test_resolve_typescript_names_come_from_declarations_not_words(symbols: Repo) -> None:
    for word in ("s", "ality"):
        assert symbols.found(word, "frontend/words") == []
    assert symbols.found("plain", "frontend/words") == [("function", 2)]
    assert symbols.found("stream", "frontend/words") == [("function", 3)]
    assert symbols.found("gen", "frontend/words") == [("function", 4)]


def test_resolve_typescript_variables_without_initializers(symbols: Repo) -> None:
    def f(name: str) -> list[tuple]:
        return symbols.found(name, "frontend/vars")

    assert f("decodedToken") == [("variable", 2)]
    assert f("first") == [("variable", 3)]
    assert f("second") == [("variable", 3)]
    assert f("tail") == [("variable", 4)]
    assert f("commented") == []
    assert f("quoted") == []
    assert f("m") == [("variable", 7)]
    assert f("last") == [("variable", 7)]
    assert f("V") == [] and f("y") == []
    assert f("one") == [("const", 8)]
    # An arrow initializer makes a const a function.
    assert f("two") == [("function", 8)]


def test_resolve_reads_jsx_files(symbols: Repo) -> None:
    found = symbols.resolve("EchoIntegration", side="frontend", file_hint="EchoIntegration.dev.jsx").candidates
    assert [(c.file, c.kind, c.score) for c in found] == [
        ("frontend/echo/EchoIntegration.dev.jsx", "component", 1.0),
        ("frontend/echo/EchoIntegration.tsx", "function", 0.4),
    ]
    assert symbols.found("EchoGuarded", "frontend/echo", kind_filter="component") == [("component", 1)]
    assert symbols.found("Panel", "frontend/echo") == [("component", 4)]
    assert symbols.found("notAVariable") == []


def test_resolve_typescript_kinds(symbols: Repo) -> None:
    def kinds(name: str) -> list[str]:
        return [k for (k,) in symbols.found(name, "frontend/kinds", "kind")]

    assert kinds("AccessRole") == ["enum"]
    assert kinds("Flag") == ["enum"]
    assert kinds("Paged") == ["type_alias"]
    assert kinds("Props") == ["interface"]
    assert kinds("counter") == ["variable"]
    assert kinds("apiBase") == ["const"]
    assert kinds("Store") == ["class"]
    # PascalCase consts are components only with a function value or a component wrapper call.
    assert kinds("MeContext") == ["const"]
    assert kinds("Labels") == ["const"]
    assert kinds("Card") == ["component"]
    assert kinds("Memo") == ["component"]


def test_resolve_typescript_ignores_keywords_in_comments_strings_and_jsx_text(symbols: Repo) -> None:
    for prose in ("to", "handles", "shown", "Flag"):
        assert symbols.found(prose, "frontend/prose") == [], prose
    assert symbols.found("hint", "frontend/prose") == [("const", 3)]
    assert symbols.found("Label", "frontend/prose") == [("component", 4)]
    assert symbols.found("Mode", "frontend/prose") == [("enum", 5)]
    for name in ("Fake", "Fake2", "Fake3", "Fake4", "Fake5", "Fake6", "Fake7", "Foo", "Bar", "Baz"):
        assert symbols.found(name, "frontend/prose") == [], name
    assert symbols.found("Inner", "frontend/prose", "kind", "start_line", "scope") == [("const", 4, "local")]
    assert symbols.found("Help", "frontend/prose") == [("component", 1)]


GAPS = {
    "backtick": [("Lost", "function", 2), ("AlsoLost", "const", 3)],
    "glob": [("GlobLost", "function", 2), ("GlobAlsoLost", "const", 3)],
    "phantoms": [("Phantom", None, None), ("Fake", None, None), ("sql", "const", 4), ("Real", "component", 7)],
    "multiline": [("Real", "const", 6), ("realToo", "function", 13), ("answer", "const", 14)],
    "near_jsx": [
        ("AfterElement", None, None),
        ("AfterFunction", None, None),
        ("InAttribute", None, None),
        ("InChild", None, None),
        ("sql", "const", 2),
        ("Page", "component", 5),
    ],
    "cast": [("InCast", None, None)],
    "generics": [
        ("Fake", None, None),
        ("Lit", None, None),
        ("Shown", None, None),
        ("M", None, None),
        ("P", None, None),
        ("pick", "function", 1),
        ("f", "function", 5),
        ("Fn", "type_alias", 6),
        ("A", "class", 14),
        ("updateFilter", "function", 16),
    ],
    "steps": [("Real", "const", 46)],
    "terms": [("Late", "const", 606)],
    "text": [
        ("AfterParen", "const", 2),
        ("AfterBracket", "const", 4),
        ("AfterExtends", "const", 6),
        ("pick", "function", 7),
        ("withDefault", "function", 11),
        ("Phantom", None, None),
        ("Phantom2", None, None),
    ],
    "smile": [
        ("Phantom", None, None),
        ("Phantom2", None, None),
        ("AfterTemplate", "const", 10),
        ("AfterContainer", "const", 12),
    ],
    "render": [("helper", "function", 2), ("MAX_ROWS", "const", 3), ("View", "component", 4)],
}


@pytest.mark.parametrize("case", sorted(GAPS))
def test_resolve_typescript_reads_declarations_as_typescript_does(symbols: Repo, case: str) -> None:
    """Text-level traps for declaration scanning: stray backticks, globs, quotes and
    brackets in JSX text; comments and templates near JSX; type parameters and
    arguments; long sibling chains. Each declaration is what TypeScript parses."""
    ext = ".ts" if case == "cast" else ".tsx"
    rel = f"frontend/gaps/{case}{ext}"
    for name, kind, line in GAPS[case]:
        got = [(c.kind, c.start_line) for c in symbols.resolve(name, side="frontend").candidates if c.file == rel]
        assert got == ([] if kind is None else [(kind, line)]), name


def test_resolve_typescript_indexes_every_declaration_on_a_line(symbols: Repo) -> None:
    def f(name: str) -> list[tuple]:
        return symbols.found(name, "frontend/oneline/one_line.tsx")

    assert f("View") == [("component", 1)]
    assert f("answer") == [("const", 1)]
    assert f("A") == [("type_alias", 2)]
    assert f("B") == [("type_alias", 2)]
    assert f("first") == [("variable", 3)]
    assert f("second") == [("variable", 3)]
    assert f("Card") == [("component", 4)]
    assert f("MAX_ROWS") == [("const", 4)]
    assert symbols.found("c7999", "frontend/oneline/long.ts", "kind", "start_line") == [("const", 1)]
    assert symbols.found("gen", "frontend/oneline/long.ts", "kind") == [("function",)]


def test_skips_node_modules_and_dot_directories(symbols: Repo) -> None:
    files = [c.file for c in symbols.resolve("DataTable", side="frontend").candidates]
    assert not any("node_modules" in f or ".next" in f for f in files)


# ── Ranking ───────────────────────────────────────────────────────────────


def test_ranking_demotes_tests_mocks_and_object_properties(symbols: Repo) -> None:
    ranked = [
        (c.file, c.score, c.scope)
        for c in symbols.resolve("DataTable", side="frontend").candidates
        if c.file.startswith("frontend/ranking")
    ]
    assert ranked == [
        ("frontend/ranking/components/ui/data-table.tsx", 0.4, "module"),
        ("frontend/ranking/components/__tests__/Page.test.tsx", 0.0, "object"),
        ("frontend/ranking/test/mocks/data-table-shim.tsx", 0.0, "object"),
    ]


def test_score_weighs_paths_scopes_and_tests() -> None:
    def score(file: str, scope: str = "module", is_test: bool = False, hint: str | None = None) -> float:
        candidate = symbol_resolve.SymbolCandidate(
            name="x",
            file=file,
            start_line=1,
            end_line=1,
            kind="function",
            signature="",
            language="typescript",
            scope=scope,
            is_test=is_test,
        )
        return symbol_resolve.score_candidate(candidate, hint, LAYOUT)

    assert score("frontend/components/a.tsx") == 0.5
    assert score("frontend/lib/a.ts") == 0.5
    assert score("frontend/hooks/a.ts") == 0.5
    assert score("backend/service_backend/a.py") == 0.5
    assert score("frontend/app/a.tsx") == 0.4
    assert score("backend/scripts/a.py") == 0.4
    assert score("tools/a.py") == 0.1
    assert score("frontend/lib/a.ts", is_test=True) == 0.2
    assert score("frontend/lib/a.ts", scope="object") == 0.3
    assert score("frontend/lib/a.ts", scope="local") == 0.4
    assert score("frontend/lib/a.ts", scope="class") == 0.4
    assert score("frontend/app/a.tsx", scope="object", is_test=True) == 0.0
    assert score("tools/a.py", hint="tools/") == 1.0
    assert score("tools/a.py", hint="*.py") == 1.0
    assert score("tools/a.py", hint="nomatch") == 0.1


def test_ranking_ties_break_on_scope_and_depth_not_language() -> None:
    def candidate(
        file: str, language: symbol_resolve.Language, scope: str = "module"
    ) -> symbol_resolve.SymbolCandidate:
        return symbol_resolve.SymbolCandidate(
            name="Item", file=file, start_line=1, end_line=1, kind="class", signature="", language=language, scope=scope
        )

    ranked = symbol_resolve.rank(
        [
            candidate("backend/service_backend/models/deep/item.py", "python"),
            candidate("frontend/lib/item.ts", "typescript", scope="class"),
            candidate("frontend/lib/item.ts", "typescript"),
        ],
        None,
        LAYOUT,
    )
    assert [(c.file, c.scope) for c in ranked] == [
        ("frontend/lib/item.ts", "module"),
        ("backend/service_backend/models/deep/item.py", "module"),
        ("frontend/lib/item.ts", "class"),
    ]


def test_file_hint_scoring(symbols: Repo) -> None:
    [hit] = [
        c
        for c in symbols.resolve("DataTable", side="frontend", file_hint="app/components/DataTable").candidates
        if c.score == 1.0
    ]
    assert hit.file == "frontend/app/components/DataTable.tsx"


@pytest.mark.parametrize(
    ("path", "hint", "matches"),
    [
        ("frontend/components/ui/DataTable.tsx", "components/ui", True),
        ("frontend/components/ui/DataTable.tsx", "ui/*.tsx", True),
        ("frontend/components/ui/DataTable.tsx", "frontend/*/DataTable.tsx", True),
        ("frontend\\components\\ui\\DataTable.tsx", "components/ui", True),
        ("frontend/components/ui/DataTable.tsx", "Components", False),
        ("frontend/components/ui/DataTable.tsx", "*.py", False),
    ],
)
def test_file_hint_is_a_case_sensitive_substring_or_glob(path: str, hint: str, matches: bool) -> None:
    assert symbol_resolve.file_hint_matches(path, hint) is matches


# ── Sides, kinds and the toolchain ───────────────────────────────────────


def test_side_filter_backend_frontend_both(symbols: Repo) -> None:
    assert {c.language for c in symbols.resolve("get_current_user", side="backend").candidates} == {"python"}
    assert {c.language for c in symbols.resolve("DataTable", side="frontend").candidates} == {"typescript"}
    both = symbols.resolve("Appointment", side="both")
    assert {c.language for c in both.candidates} == {"python", "typescript"}


def test_kind_filter(symbols: Repo) -> None:
    components = symbols.resolve("DataTable", side="frontend", kind_filter="component").candidates
    assert {c.kind for c in components} == {"component"}
    assert len(symbols.resolve("Appointment", side="both", kind_filter="none").candidates) == 2
    with pytest.raises(ValueError, match="kind 'bogus' is not one of function, class, component"):
        symbols.resolve("x", kind_filter="bogus")
    with pytest.raises(ValueError, match="side 'bogus' is not one of backend, frontend, both"):
        symbols.resolve("x", side="bogus")
    assert symbol_resolve.SYMBOL_KINDS[-1] == "namespace"


def test_frontend_side_raises_and_both_degrades_without_the_toolchain(python_repo: Repo) -> None:
    with pytest.raises(ts_facts.ToolchainUnavailable):
        python_repo.resolve("Appointment", side="frontend")
    result = python_repo.resolve("Appointment", side="both")
    assert [c.language for c in result.candidates] == ["python"]
    assert result.complete is False
    assert result.frontend_problem is not None and result.frontend_problem.code == "typescript_not_found"
    [problem] = [p for p in result.problems if p.kind == "frontend_unavailable"]
    assert (
        problem.message
        == "frontend symbols were not searched: tools/code-intel/ts/node_modules/typescript/package.json not found"
    )
    as_dict = problem.as_diagnostic()
    assert (as_dict["problem"], as_dict["missing"], as_dict["fix"]) == (
        "typescript_not_found",
        "tools/code-intel/ts/node_modules/typescript/package.json",
        ts_bridge.FRONTEND_TS_FIX,
    )
    backend = python_repo.resolve("Appointment", side="backend")
    assert backend.frontend_problem is None


def test_edge_declarations_exports_namespaces_and_lines(symbols: Repo) -> None:
    def found(name: str, *fields: str) -> list[tuple]:
        return symbols.found(name, "frontend/edges/", *fields, side="frontend")

    # `export default X;` and `export = X;` export that declaration.
    assert found("Foo3", "exported") == [(True,)]
    assert found("Bar3", "exported") == [(True,)]
    # Nested and dotted namespaces qualify their members the same way.
    assert found("y17", "scope", "container") == [("namespace", "Outer17.Inner17")]
    assert found("x9", "scope", "container") == [("namespace", "A9.B9")]
    # U+2028 inside a string does not end the line.
    assert found("after18", "start_line") == [(2,)]


def test_both_sides_keep_the_backend_when_the_helper_fails(tmp_path: Path) -> None:
    _write_tree(tmp_path / "backend", PY_FIXTURES)
    _write_tree(tmp_path / "frontend", {"a.ts": "export const Appointment = 1;\n"})
    toolchain = ts_bridge.Toolchain("/node", "v24", Path("/ts"), "7.1.0-beta", "env", "7.1.0-beta", None, ())

    def crashed(*_args, **_kwargs):
        raise ts_bridge.HelperError("facts", "exit", "node exited with code 1: RangeError")

    facts = ts_facts.FactsCache(tmp_path, lambda: toolchain, run_helper=crashed)
    index = _index(tmp_path, facts, LAYOUT)
    with pytest.raises(ts_bridge.HelperError):
        index.resolve("Appointment", side="frontend")
    result = index.resolve("Appointment", side="both")
    assert [c.language for c in result.candidates] == ["python"]
    assert result.complete is False
    assert result.frontend_problem is not None and result.frontend_problem.code == "helper_failed"
    [problem] = [p for p in result.problems if p.kind == "frontend_unavailable"]
    assert "step 'exit'" in problem.message and "RangeError" in problem.message
    assert (problem.problem, problem.missing, problem.fix) == ("helper_failed", None, ts_bridge.HELPER_FIX)


def test_a_checkout_without_frontend_reports_frontend_not_found(typescript_toolchain, tmp_path: Path) -> None:
    _write_tree(tmp_path / "backend", PY_FIXTURES)
    index = _index(tmp_path, ts_facts.FactsCache(tmp_path, lambda: typescript_toolchain), LAYOUT)
    with pytest.raises(ts_bridge.ToolchainUnavailable) as raised:
        index.resolve("Appointment", side="frontend")
    assert (raised.value.problem.code, raised.value.problem.missing) == ("frontend_not_found", "frontend")
    result = index.resolve("Appointment", side="both")
    assert [c.language for c in result.candidates] == ["python"] and result.complete is False
    assert result.frontend_problem is not None and result.frontend_problem.code == "frontend_not_found"
    [problem] = [p for p in result.problems if p.kind == "frontend_unavailable"]
    assert (problem.kind, problem.problem, problem.missing) == (
        "frontend_unavailable",
        "frontend_not_found",
        "frontend",
    )
    assert problem.message == "frontend symbols were not searched: frontend: frontend root not found"


def test_resolve_typescript_extract_failures_are_diagnostics(tmp_path: Path) -> None:
    """A file the helper could not analyze is a named diagnostic; others still resolve."""
    toolchain = ts_bridge.Toolchain("/node", "v24", Path("/ts"), "7.1.0-beta", "env", "7.1.0-beta", None, ())

    def helper(toolchain, mode, request, *, timeout_s):
        files = []
        for path in request["files"]:
            ok = path.endswith("ok.tsx")
            decl = {
                "name": "Target",
                "kind": "const",
                "line": 1,
                "end_line": 1,
                "column": 14,
                "scope": "module",
                "container": None,
                "exported": True,
                "signature": "export const Target = 1",
            }
            files.append(
                {
                    "path": path,
                    "status": "ok" if ok else "extract_failed",
                    "error": None if ok else "RangeError: Maximum call stack size exceeded",
                    "syntax_errors": 0,
                    "first_syntax_error": None,
                    "elements": [],
                    "decls": [decl] if ok else [],
                }
            )
        return {"protocol": 1, "mode": "facts", "facts_schema": 1, "files": files}

    _write_tree(
        tmp_path / "frontend", {"ok.tsx": "export const Target = 1;\n", "bad.tsx": "export const Target = 2;\n"}
    )
    index = _index(tmp_path, ts_facts.FactsCache(tmp_path, lambda: toolchain, run_helper=helper), LAYOUT)
    result = index.resolve("Target", side="frontend")
    assert [c.file for c in result.candidates] == ["frontend/ok.tsx"]
    assert result.complete is False
    assert [(p.kind, p.step, p.file) for p in result.problems] == [
        ("analysis_failed", "extract facts", "frontend/bad.tsx")
    ]


# ── Python ───────────────────────────────────────────────────────────────


def test_resolve_python_function_class_async_method(python_repo: Repo) -> None:
    fns = python_repo.resolve("list_articles", side="backend").candidates
    assert sorted((c.kind, c.scope, c.container) for c in fns) == [
        ("function", "module", None),
        ("method", "class", "SquadService"),
    ]
    [cls] = python_repo.resolve("Appointment", side="backend").candidates
    assert (cls.kind, cls.language, cls.exported) == ("class", "python", None)
    [async_fn] = python_repo.resolve("list_articles", side="backend", kind_filter="function").candidates
    assert async_fn.signature == "async def list_articles(db):"
    assert [c.kind for c in python_repo.resolve("list_articles", side="backend", kind_filter="method").candidates] == [
        "method"
    ]


def test_resolve_python_module_level_assignments(python_repo: Repo) -> None:
    def f(name: str) -> list[tuple]:
        return python_repo.found(name, "backend/service_backend/routes.py", "kind", "start_line", side="backend")

    assert f("router") == [("variable", 3)]
    assert f("DbDep") == [("type_alias", 4)]
    assert f("_require_list_access") == [("variable", 5)]
    assert f("MAX_PAGE") == [("const", 6)]
    assert f("limit") == [("const", 7)]
    assert f("timeout") == [("variable", 8)]
    assert f("Pair") == [("type_alias", 9)]
    assert f("Alias") == [("type_alias", 10)]
    assert f("second") == [("variable", 11)]
    assert f("hidden") == [("variable", 14)]
    assert f("hidden_fn") == [("function", 16)]
    # Class fields are indexed (scope class); function locals are not.
    assert python_repo.found("field_name", "", "kind", "scope", "container", side="backend") == [
        ("variable", "class", "Model")
    ]
    assert f("local_only") == []
    [router] = python_repo.resolve("router", side="backend").candidates
    assert (router.signature, router.end_line) == ('router = APIRouter(tags=["x"])', 3)


def test_python_type_alias_kinds(python_repo: Repo) -> None:
    def kind(name: str, prefix: str = "backend/corpus/m.py") -> list[str]:
        return [k for (k,) in python_repo.found(name, prefix, "kind", side="backend")]

    for name in (
        "DbDep",
        "Qualified",
        "Explicit",
        "QualExplicit",
        "Pep695",
        "Pep604",
        "Pep585",
        "AbcAlias",
        "T",
        "Opt",
    ):
        assert kind(name) == ["type_alias"], name
    assert kind("MAX") == ["const"]
    enums = "backend/service_backend/enums.py"
    for name in ("Status", "Ids", "UserId", "P", "Number"):
        assert kind(name, enums) == ["type_alias"], name
    assert kind("Plain", enums) == ["variable"]
    assert kind("Mask", enums) == ["variable"], "a | of plain names is not a type"


def test_python_control_flow_walrus_class_bodies_and_nested_defs(python_repo: Repo) -> None:
    def rows(name: str) -> list[tuple]:
        return python_repo.found(name, "backend/corpus/", "kind", "start_line", "scope", "container", side="backend")

    assert rows("OnlyForTypes") == [("class", 5, "module", None)]
    assert sorted(rows("loads")) == [("function", 9, "module", None)]
    assert rows("walrus") == [("variable", 26, "module", None)]
    assert rows("loop_var") == [("variable", 27, "module", None)]
    assert rows("LOOP_DEF") == [("const", 28, "module", None)]
    assert rows("in_while") == [("function", 30, "module", None)]
    assert rows("in_match") == [("function", 33, "module", None)]
    assert rows("ASYNC_WITH") == [("const", 3, "module", None)]
    assert rows("c2") == [], "function locals are not indexed"
    assert rows("attr") == [("variable", 38, "class", "Outer")]
    assert rows("CONST") == [("const", 39, "class", "Outer")]
    assert rows("Inner") == [("class", 40, "class", "Outer")]
    assert rows("inner_method") == [("method", 41, "class", "Outer.Inner")]
    assert rows("guarded_method") == [("method", 43, "class", "Outer")]
    assert rows("prop") == [("method", 45, "class", "Outer"), ("method", 47, "class", "Outer")]
    assert rows("decorated") == [("function", 52, "module", None)]
    assert rows("nested") == [("function", 55, "local", None)]
    assert rows("LocalCls") == [("class", 56, "local", None)]
    assert rows("g") == []
    assert rows("a") == [("variable", 22, "module", None)]
    assert rows("b") == [("variable", 22, "module", None)]
    assert [r[0] for r in rows("e")] == ["variable"]
    assert rows("attr") and python_repo.found("obj", "backend/corpus/", side="backend") == []
    bindable = python_repo.found("m", "backend/service_backend/enums.py", "kind", "start_line", side="backend")
    assert bindable == [("variable", 17)], "a walrus in a module-level comprehension binds at module scope"


def test_python_enum_and_protocol_kinds(python_repo: Repo) -> None:
    def kind(name: str) -> list[str]:
        return [k for (k,) in python_repo.found(name, "backend/", "kind", side="backend")]

    assert kind("E") == ["enum"]
    assert sorted(kind("P")) == ["interface", "type_alias"]
    assert kind("Color") == ["enum"]
    assert kind("Level") == ["enum"]
    assert kind("Shade") == ["enum"], "a subclass of an enum in the same module"
    assert kind("Reader") == ["interface"]
    assert [c.kind for c in python_repo.resolve("m", side="backend", kind_filter="method").candidates] == ["method"]


def test_python_lines_follow_the_parser(python_repo: Repo) -> None:
    """A form feed or U+0085 is not a line break for `ast`, so signatures and
    lines after one stay aligned; columns count code points."""
    [after_ff] = python_repo.resolve("after_ff", side="backend").candidates
    assert (after_ff.start_line, after_ff.signature) == (2, "def after_ff():")
    [after_line] = python_repo.resolve("after_form_feed_line", side="backend").candidates
    assert (after_line.start_line, after_line.signature) == (3, "def after_form_feed_line():")
    problems: list = []
    unicode_rows = [
        (s.name, s.column) for rel, s in python_repo.index.python_symbols(problems) if rel.endswith("unicode.py")
    ]
    assert unicode_rows == [("LABEL", 1), ("Ünïcode", 14)]


def test_python_read_decode_and_parse_are_separate_steps(python_repo: Repo) -> None:
    root = python_repo.root / "backend"
    (root / "broken.py").write_text("def target(:\n", encoding="utf-8")
    (root / "ok.py").write_text("def target():\n    pass\n", encoding="utf-8")
    result = python_repo.resolve("target", side="backend")
    assert [c.file for c in result.candidates] == ["backend/ok.py"]
    by_file = {p.file: (p.kind, p.step, p.message) for p in result.problems}
    assert by_file["backend/broken.py"][:2] == ("parse_failed", "parse file")
    assert by_file["backend/broken.py"][2].startswith("backend/broken.py: could not parse file (SyntaxError")
    assert by_file["backend/corpus/bad.py"][:2] == ("decode_failed", "decode file")
    assert by_file["backend/corpus/bad.py"][2].startswith("backend/corpus/bad.py: could not decode file (")
    assert result.complete is False


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs POSIX permissions")
def test_python_read_failures_are_reported_and_not_cached(python_repo: Repo) -> None:
    locked = python_repo.root / "backend" / "locked.py"
    locked.write_text("def target():\n    pass\n", encoding="utf-8")
    locked.chmod(0)
    try:
        result = python_repo.resolve("target", side="backend")
    finally:
        locked.chmod(0o644)
    assert result.candidates == []
    [problem] = [p for p in result.problems if p.file == "backend/locked.py"]
    assert (problem.kind, problem.step) == ("read_failed", "read file")
    assert problem.message.startswith("backend/locked.py: could not read file (PermissionError")
    assert [c.file for c in python_repo.resolve("target", side="backend").candidates] == ["backend/locked.py"]


def test_resolve_reports_deeply_nested_expressions_as_parse_failures(python_repo: Repo) -> None:
    root = python_repo.root / "backend"
    (root / "ok.py").write_text("def target():\n    pass\n", encoding="utf-8")
    (root / "deep.py").write_text("X = 1" + " + 1" * 200000 + "\n", encoding="utf-8")
    result = python_repo.resolve("target", side="backend")
    assert [c.file for c in result.candidates] == ["backend/ok.py"]
    [deep] = [p for p in result.problems if p.file == "backend/deep.py"]
    assert (deep.kind, deep.step) == ("parse_failed", "parse file")


def test_resolve_reports_stat_failures(python_repo: Repo, monkeypatch) -> None:
    root = python_repo.root / "backend"
    (root / "ok.py").write_text("def target():\n    pass\n", encoding="utf-8")
    (root / "locked.py").write_text("def target():\n    pass\n", encoding="utf-8")
    real_stat = code_paths._stat

    def flaky_stat(entry):
        if entry.name == "locked.py":
            raise PermissionError(13, "Permission denied")
        return real_stat(entry)

    monkeypatch.setattr(code_paths, "_stat", flaky_stat)
    result = python_repo.resolve("target", side="backend")
    assert [c.file for c in result.candidates] == ["backend/ok.py"]
    [problem] = [p for p in result.problems if p.file == "backend/locked.py"]
    assert (problem.kind, problem.step) == ("stat_failed", "stat file")


def test_python_index_failures_are_contained_per_file(python_repo: Repo, monkeypatch) -> None:
    """An unexpected exception while indexing one file names the file and step."""
    real = symbol_resolve.index_python_module

    def flaky(tree, lines):
        if any("after_ff" in line for line in lines):
            raise RuntimeError("boom")
        return real(tree, lines)

    monkeypatch.setattr(symbol_resolve, "index_python_module", flaky)
    result = python_repo.resolve("Appointment", side="backend")
    assert [c.file for c in result.candidates] == ["backend/service_backend/service.py"]
    [problem] = [p for p in result.problems if p.file == "backend/corpus/ff.py"]
    assert (problem.kind, problem.step, problem.exception) == ("analysis_failed", "index symbols", "RuntimeError")


@pytest.mark.usefixtures("ctime_follows_mtime")
def test_resolve_caches_per_file_index(tmp_path: Path, monkeypatch) -> None:
    """Old stamps are trusted alone; ctime follows mtime so the old mtimes set
    here make them old, as the kernel's ctime of a fresh file would not."""
    backend = tmp_path / "backend"
    module = backend / "service.py"
    _write_tree(backend, {"service.py": "def target():\n    pass\n", "other.py": "def unrelated():\n    pass\n"})
    for path in backend.iterdir():
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    builds: list[str] = []
    real = symbol_resolve._index_python_file

    def counting(source, **kwargs):
        builds.append(Path(source.rel).name)
        return real(source, **kwargs)

    monkeypatch.setattr(symbol_resolve, "_index_python_file", counting)
    toolchain = _unavailable_toolchain()
    index = _index(tmp_path, ts_facts.FactsCache(tmp_path, lambda: toolchain), LAYOUT)

    def lookup(name: str) -> list[int]:
        return [c.start_line for c in index.resolve(name, side="backend").candidates]

    assert lookup("target") == [1]
    assert sorted(builds) == ["other.py", "service.py"]
    builds.clear()
    assert lookup("target") == [1] and lookup("unrelated") == [1]
    assert builds == []
    module.write_text("\n\ndef target():\n    pass\n", encoding="utf-8")
    os.utime(module, ns=(2_000_000_000, 2_000_000_000))
    assert lookup("target") == [3]
    assert builds == ["service.py"]
    module.unlink()
    assert lookup("target") == []
    assert code_paths.cache_key(module) not in index._py


@pytest.mark.usefixtures("ctime_follows_mtime")
def test_python_racy_entries_are_hash_checked(tmp_path: Path) -> None:
    """The racy-clean rule with an injected clock: a same-stamp edit within the
    window is seen. ctime follows mtime, as for two writes in one ctime tick,
    so the edit keeps the stamp and only the content check can catch it."""
    backend = tmp_path / "backend"
    _write_tree(backend, {"svc.py": "def alpha(): pass\n"})
    path = backend / "svc.py"
    now = 10_000_000_000_000
    os.utime(path, ns=(now - 1, now - 1))
    toolchain = _unavailable_toolchain()
    index = _index(tmp_path, ts_facts.FactsCache(tmp_path, lambda: toolchain), LAYOUT, clock=lambda: now)
    assert [c.name for c in index.resolve("alpha", side="backend").candidates] == ["alpha"]
    path.write_text("def gamma(): pass\n", encoding="utf-8")  # same size
    os.utime(path, ns=(now - 1, now - 1))
    assert index.resolve("alpha", side="backend").candidates == []
    assert [c.name for c in index.resolve("gamma", side="backend").candidates] == ["gamma"]


def _edit_in_place(path: Path, text: str) -> None:
    """Rewrite ``path`` with same-size content, keeping its whole stamp when
    ``ctime_follows_mtime`` is active."""
    st = path.stat()
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size


def _edit_after_first_read(monkeypatch: pytest.MonkeyPatch, path: Path, text: str) -> None:
    """Make the first read of ``path`` return its old bytes and then change the
    file, as an edit landing between the read and the cache insertion would."""
    real = code_paths.read_file_bytes
    pending = [True]

    def read(target: Path) -> bytes:
        data = real(target)
        if pending and target == path:
            pending.clear()
            _edit_in_place(path, text)
        return data

    monkeypatch.setattr(code_paths, "read_file_bytes", read)


@pytest.mark.usefixtures("ctime_follows_mtime")
def test_python_edit_between_the_read_and_the_cache_is_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The digest stored for a racy file is of the bytes indexed, so an edit
    right after the read makes the next lookup index the file again. ctime
    follows mtime, so the edit keeps the stamp and only the digest shows it."""
    backend = tmp_path / "backend"
    _write_tree(backend, {"svc.py": "def alpha(): pass\n"})
    path = backend / "svc.py"
    now = 10_000_000_000_000
    os.utime(path, ns=(now - 1, now - 1))
    _edit_after_first_read(monkeypatch, path, "def gamma(): pass\n")
    toolchain = _unavailable_toolchain()
    index = _index(tmp_path, ts_facts.FactsCache(tmp_path, lambda: toolchain), LAYOUT, clock=lambda: now)
    assert [c.name for c in index.resolve("alpha", side="backend").candidates] == ["alpha"]
    assert index.resolve("alpha", side="backend").candidates == []
    assert [c.name for c in index.resolve("gamma", side="backend").candidates] == ["gamma"]


def test_skips_venv_and_marks_backend_tests(python_repo: Repo) -> None:
    rows = [(c.file, c.is_test, c.score) for c in python_repo.resolve("get_current_user", side="backend").candidates]
    assert rows == [("backend/service_backend/service.py", False, 0.5), ("backend/tests/test_service.py", True, 0.1)]


def test_python_index_has_every_module_level_def_and_class(python_repo: Repo) -> None:
    """Every module-level FunctionDef/AsyncFunctionDef/ClassDef found by a plain
    `ast` walk is indexed at the same line."""
    problems: list = []
    indexed = {(rel, s.name, s.line) for rel, s in python_repo.index.python_symbols(problems)}
    for path in sorted((python_repo.root / "backend").rglob("*.py")):
        rel = path.relative_to(python_repo.root).as_posix()
        if ".venv" in rel:
            continue
        try:
            tree = ast.parse(path.read_bytes())
        except _PARSE_ERRORS:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                assert (rel, node.name, node.lineno) in indexed, (rel, node.name)


# ── Protocol matches ─────────────────────────────────────────────────────

PROTOCOL_FIXTURES = {
    "service_backend/__init__.py": "",
    "service_backend/gateway.py": (
        "from typing import Protocol\n"
        "\n"
        "\n"
        "class Gateway(Protocol):\n"
        "    name: str\n"
        "\n"
        "    def fetch(self) -> int: ...\n"
        "\n"
        "    def store(self, item) -> None: ...\n"
        "\n"
        "\n"
        "class Closer(Protocol):\n"
        "    def fetch(self) -> int: ...\n"
        "\n"
        "    def close(self) -> None: ...\n"
        "\n"
        "\n"
        "class Context(Protocol):\n"
        "    def __enter__(self): ...\n"
        "\n"
        "    def run(self) -> None: ...\n"
        "\n"
        "\n"
        "class Base:\n"
        "    def store(self, item): ...\n"
        "\n"
        "\n"
        "class Matching:\n"
        "    def __init__(self):\n"
        "        self.name = 'm'\n"
        "\n"
        "    def fetch(self): ...\n"
        "\n"
        "    def store(self, item): ...\n"
        "\n"
        "\n"
        "class MissingStore:\n"
        "    name = 'x'\n"
        "\n"
        "    def fetch(self): ...\n"
        "\n"
        "\n"
        "class Explicit(Gateway):\n"
        "    def fetch(self): ...\n"
        "\n"
        "\n"
        "class Inherits(Base):\n"
        "    name = 'x'\n"
        "\n"
        "    def fetch(self): ...\n"
        "\n"
        "\n"
        "class Wide:\n"
        "    name: str\n"
        "\n"
        "    def fetch(self): ...\n"
        "\n"
        "    def store(self, item): ...\n"
        "\n"
        "    def close(self): ...\n"
        "\n"
        "\n"
        "class Runner:\n"
        "    def run(self): ...\n"
        "\n"
        "\n"
        "class Unrelated:\n"
        "    def close(self): ...\n"
        "\n"
        "\n"
        "class Entered:\n"
        "    def __enter__(self): ...\n"
        "\n"
        "    def run(self): ...\n"
        "\n"
        "\n"
        "class Sub(Gateway, Protocol):\n"
        "    def extra(self): ...\n"
        "\n"
        "\n"
        "class ViaSub(Sub):\n"
        "    def extra(self): ...\n"
        "\n"
        "\n"
        "class HasExtra:\n"
        "    def extra(self): ...\n"
        "\n"
        "\n"
        "class Outer:\n"
        "    class Inner:\n"
        "        name = 'x'\n"
        "\n"
        "        def fetch(self): ...\n"
        "\n"
        "        def store(self, item): ...\n"
        "\n"
        "    def fetch(self): ...\n"
        "\n"
        "\n"
        "class Namespace:\n"
        "    class NestedGateway(Protocol):\n"
        "        def ping(self): ...\n"
        "\n"
        "\n"
        "class Pinger:\n"
        "    def ping(self): ...\n"
        "\n"
        "\n"
        "class Builder:\n"
        "    def build(self): ...\n"
    ),
    "service_backend/construct.py": (
        "from typing import Protocol\n"
        "\n"
        "\n"
        "class Constructs(Protocol):\n"
        "    def __init__(self, x): ...\n"
        "\n"
        "    def build(self): ...\n"
    ),
}

# Further modules: bases reached through imports, a name that two classes share, generic bases,
# and classes defined in functions or in the two arms of an `if TYPE_CHECKING`.
PROTOCOL_FIXTURES_MORE = {
    "service_backend/solo.py": ("class Solo:\n    name = 'x'\n\n    def store(self, item): ...\n"),
    "service_backend/twin_a.py": "class Twin:\n    name = 'a'\n\n    def store(self, item): ...\n",
    "service_backend/twin_b.py": "class Twin:\n    name = 'b'\n\n    def store(self, item): ...\n",
    "service_backend/impls.py": (
        "import service_backend.gateway as gw\n"
        "from . import gateway as gmod\n"
        "from .gateway import Gateway as RelativeGateway\n"
        "from service_backend import Solo, Twin\n"
        "from service_backend.gateway import Gateway\n"
        "from some_lib import Closer\n"
        "\n"
        "\n"
        "class ImportedBase(Gateway):\n"
        "    def fetch(self): ...\n"
        "\n"
        "\n"
        "class ModuleAliasBase(gw.Gateway):\n"
        "    def fetch(self): ...\n"
        "\n"
        "\n"
        "class SubmoduleBase(gmod.Gateway):\n"
        "    def fetch(self): ...\n"
        "\n"
        "\n"
        "class RelativeBase(RelativeGateway):\n"
        "    def fetch(self): ...\n"
        "\n"
        "\n"
        "class ExternalBase(Closer):\n"
        "    def fetch(self): ...\n"
        "\n"
        "\n"
        "class ReexportedBase(Solo):\n"
        "    def fetch(self): ...\n"
        "\n"
        "\n"
        "class AmbiguousBase(Twin):\n"
        "    def fetch(self): ...\n"
    ),
    "service_backend/generic.py": (
        "from typing import Generic, Protocol, TypeVar\n"
        "\n"
        "T = TypeVar('T')\n"
        "\n"
        "\n"
        "class Box(Protocol[T]):\n"
        "    def get(self) -> T: ...\n"
        "\n"
        "\n"
        "class Holder(Generic[T]):\n"
        "    def get(self): ...\n"
        "\n"
        "\n"
        "class Declared(Box[int]):\n"
        "    def get(self): ...\n"
        "\n"
        "\n"
        "class Lacking(Generic[T]):\n"
        "    def put(self): ...\n"
    ),
    "service_backend/scopes.py": (
        "from typing import TYPE_CHECKING, Protocol\n"
        "\n"
        "\n"
        "class Two(Protocol):\n"
        "    def left(self): ...\n"
        "\n"
        "    def right(self): ...\n"
        "\n"
        "\n"
        "class Foo:\n"
        "    def right(self): ...\n"
        "\n"
        "\n"
        "def factory():\n"
        "    class Foo:\n"
        "        def left(self): ...\n"
        "\n"
        "    class Both:\n"
        "        def left(self): ...\n"
        "\n"
        "        def right(self): ...\n"
        "\n"
        "    return Foo, Both\n"
        "\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "\n"
        "    class Impl:\n"
        "        def left(self): ...\n"
        "\n"
        "else:\n"
        "\n"
        "    class Impl:\n"
        "        def left(self): ...\n"
        "\n"
        "        def right(self): ...\n"
    ),
}


@pytest.fixture
def protocol_repo(tmp_path: Path) -> Repo:
    _write_tree(tmp_path / "backend", {**PROTOCOL_FIXTURES, **PROTOCOL_FIXTURES_MORE})
    (tmp_path / "frontend").mkdir()
    toolchain = _unavailable_toolchain()
    return Repo(tmp_path, _index(tmp_path, ts_facts.FactsCache(tmp_path, lambda: toolchain), LAYOUT), None)


def _matches(repo: Repo, name: str, file: str = "gateway.py") -> dict[str | None, list[str]]:
    """Owning class -> `Protocol:line` of every Protocol its method matches, in one file."""
    return {
        c.container: [f"{m.name}:{m.line}" for m in c.protocols]
        for c in repo.resolve(name, side="backend").candidates
        if c.file.endswith(f"/{file}")
    }


def test_a_class_matches_a_protocol_when_it_defines_every_member_by_name(protocol_repo: Repo) -> None:
    matches = _matches(protocol_repo, "fetch")
    assert matches["Matching"] == ["Gateway:7"], "name comes from __init__, store from the class"
    assert matches["Wide"] == ["Gateway:7", "Closer:13"], "sorted by file, then the Protocol method's line"
    assert matches["Inherits"] == ["Gateway:7"], "a member inherited from an in-repo base counts"


def test_a_class_missing_a_member_matches_no_protocol(protocol_repo: Repo) -> None:
    assert _matches(protocol_repo, "fetch")["MissingStore"] == []
    assert _matches(protocol_repo, "close")["Unrelated"] == []


def test_a_class_naming_the_protocol_as_a_base_matches_without_the_members(protocol_repo: Repo) -> None:
    assert _matches(protocol_repo, "fetch")["Explicit"] == ["Gateway:7"]


def test_only_the_dunders_every_class_has_are_not_required(protocol_repo: Repo) -> None:
    run = _matches(protocol_repo, "run")
    assert run["Runner"] == [], "__enter__ is asked of the class, as runtime_checkable asks it"
    assert run["Entered"] == ["Context:21"]
    # `__init__` comes from `object` on every class, so the Protocol in construct.py asks only for `build`.
    assert _matches(protocol_repo, "build")["Builder"] == ["Constructs:7"]


def test_a_protocol_method_candidate_is_labelled_and_lists_no_matches(protocol_repo: Repo) -> None:
    by_container = {
        c.container: c
        for c in protocol_repo.resolve("fetch", side="backend").candidates
        if c.file.endswith("/gateway.py")
    }
    assert {name: c.protocol_method for name, c in by_container.items()} == {
        "Gateway": True,
        "Closer": True,
        "Matching": False,
        "MissingStore": False,
        "Explicit": False,
        "Inherits": False,
        "Wide": False,
        "Outer": False,
        "Outer.Inner": False,
    }
    assert by_container["Gateway"].protocols == by_container["Closer"].protocols == []


def test_a_protocol_match_carries_the_protocol_id_and_method_location(protocol_repo: Repo) -> None:
    [match] = [c for c in protocol_repo.resolve("fetch", side="backend").candidates if c.container == "Matching"][
        0
    ].protocols
    assert (match.name, match.id, match.file, match.line) == (
        "Gateway",
        "service_backend.gateway:Gateway",
        "backend/service_backend/gateway.py",
        7,
    )


def test_only_methods_are_annotated(protocol_repo: Repo) -> None:
    [gateway] = [c for c in protocol_repo.resolve("Gateway", side="backend").candidates]
    assert (gateway.kind, gateway.protocol_method, gateway.protocols) == ("interface", False, [])


def test_protocol_matches_are_in_the_cli_json(protocol_repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    write(protocol_repo.root / ".code-intel" / "config.toml", MINIMAL_CONFIG)
    argv = ["dump", "symbols", "fetch", "--side", "backend", "--json", "--repo-root", str(protocol_repo.root)]
    assert cli.main(argv) == 0
    rows = {(c["file"], c["container"]): c for c in json.loads(capsys.readouterr().out)["candidates"]}
    assert rows["backend/service_backend/gateway.py", "Gateway"]["protocol_method"] is True
    assert rows["backend/service_backend/gateway.py", "Matching"]["protocols"] == [
        {
            "name": "Gateway",
            "id": "service_backend.gateway:Gateway",
            "file": "backend/service_backend/gateway.py",
            "line": 7,
        }
    ]


def test_a_base_imported_from_the_protocols_module_matches_across_files(protocol_repo: Repo) -> None:
    """`ImportedBase` only defines `fetch`: it matches because its base resolves to the Protocol."""
    matches = _matches(protocol_repo, "fetch", "impls.py")
    for name in ("ImportedBase", "ModuleAliasBase", "SubmoduleBase", "RelativeBase"):
        assert matches[name] == ["Gateway:7"], name


def test_an_unrelated_imported_base_sharing_the_protocols_name_matches_nothing(protocol_repo: Repo) -> None:
    """`from some_lib import Closer`: not the in-repo `Closer`, and it brings no members."""
    assert _matches(protocol_repo, "fetch", "impls.py")["ExternalBase"] == []


def test_a_reexported_base_is_found_by_its_unique_name_only(protocol_repo: Repo) -> None:
    """`from service_backend import Solo` names no module that defines it; `Solo` is
    the one class of that name, so its members count; `Twin` is two classes, so none do."""
    matches = _matches(protocol_repo, "fetch", "impls.py")
    assert matches["ReexportedBase"] == ["Gateway:7"]
    assert matches["AmbiguousBase"] == []


def test_a_name_match_alone_is_never_a_nominal_match(protocol_repo: Repo) -> None:
    """The nominal match needs the Protocol's id; a base that merely shares the short name
    gets the member check like any class."""
    [rec] = [c for c in protocol_repo.resolve("fetch", side="backend").candidates if c.container == "ExternalBase"]
    assert rec.protocols == []


def test_nested_classes_are_owners_with_dotted_qualnames(protocol_repo: Repo) -> None:
    fetch = _matches(protocol_repo, "fetch")
    assert fetch["Outer.Inner"] == ["Gateway:7"]
    assert fetch["Outer"] == [], "Outer.fetch is not Outer.Inner.fetch"
    [match] = [c for c in protocol_repo.resolve("ping", side="backend").candidates if c.container == "Pinger"][
        0
    ].protocols
    assert (match.name, match.id) == ("NestedGateway", "service_backend.gateway:Namespace.NestedGateway")


def test_generic_and_protocol_subscripts_are_bases_by_their_names(protocol_repo: Repo) -> None:
    get = _matches(protocol_repo, "get", "generic.py")
    assert get["Holder"] == ["Box:7"], "Protocol[T] makes Box a Protocol; Generic[T] on Holder is no member"
    assert get["Declared"] == ["Box:7"], "Box[int] is a base that resolves to Box"
    assert _matches(protocol_repo, "put", "generic.py")["Lacking"] == []


def test_a_protocol_inheriting_another_and_a_class_reaching_it_through_the_subprotocol(protocol_repo: Repo) -> None:
    extra = _matches(protocol_repo, "extra")
    assert extra["ViaSub"] == ["Sub:78"], "ViaSub names Sub"
    assert extra["HasExtra"] == [], "Sub also asks for the members it inherits from Gateway"
    [sub] = [c for c in protocol_repo.resolve("extra", side="backend").candidates if c.container == "Sub"]
    assert sub.protocol_method is True


def test_function_local_classes_are_told_apart_from_a_module_class_of_the_same_name(protocol_repo: Repo) -> None:
    candidates = [c for c in protocol_repo.resolve("left", side="backend").candidates if c.file.endswith("scopes.py")]
    by_line = {c.start_line: [m.name for m in c.protocols] for c in candidates}
    # Two's own left (line 5) is a Protocol method; local Foo.left (16) misses `right`; local Both.left (19) has it.
    assert by_line == {5: [], 16: [], 19: ["Two"], 29: [], 34: ["Two"]}


def test_the_two_arms_of_an_if_type_checking_are_two_classes(protocol_repo: Repo) -> None:
    impl = {
        c.start_line: [m.name for m in c.protocols]
        for c in protocol_repo.resolve("left", side="backend").candidates
        if c.file.endswith("scopes.py") and c.container == "Impl"
    }
    assert impl == {29: [], 34: ["Two"]}


def test_an_edited_file_is_indexed_again_and_its_matches_change(protocol_repo: Repo) -> None:
    path = protocol_repo.root / "backend/service_backend/solo.py"
    before = protocol_repo.resolve("fetch", side="backend")
    reexported = [c for c in before.candidates if c.container == "ReexportedBase"][0]
    assert [m.name for m in reexported.protocols] == ["Gateway"]
    stat = path.stat()
    path.write_text("class Solo:\n    def store(self, item): ...\n", encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
    after = protocol_repo.resolve("fetch", side="backend")
    reexported = [c for c in after.candidates if c.container == "ReexportedBase"][0]
    assert reexported.protocols == [], "Solo lost `name`, so ReexportedBase no longer has every member"


def test_instance_attributes_skip_nested_functions_at_every_depth() -> None:
    tree = ast.parse(
        "class C:\n"
        "    def __init__(self):\n"
        "        def helper():\n"
        "            self.x = 1\n"
        "        self.z = 3\n"
        "        if self.z:\n"
        "            self.y: int = 2\n"
        "        key = lambda: setattr(self, 'q', 1)\n"
    )
    [cls] = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    assert sorted(symbol_resolve._instance_attributes(cls)) == ["y", "z"]


# ── CLI ──────────────────────────────────────────────────────────────────


def test_symbol_resolve_cli_dumps(typescript_toolchain, tmp_path: Path) -> None:
    _write_tree(tmp_path / "frontend", {"a.tsx": "export const App = () => <div />;\n"})
    _write_tree(tmp_path / "backend", {"m.py": "class M:\n    X = 1\n"})
    write(tmp_path / ".code-intel" / "config.toml", MINIMAL_CONFIG)
    git_init(tmp_path)
    env = {**os.environ, ts_bridge.TS_DIR_ENV: str(typescript_toolchain.typescript_dir)}

    def run(*args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, "-m", "code_intel.cli", "dump", "symbols", *args, "--json", "--repo-root", str(tmp_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=True,
            timeout=120,
        )
        return json.loads(proc.stdout)

    ts_dump = run("--dump-ts")
    assert ts_dump["counts"] == {"component": 1}
    assert [(d["file"], d["name"], d["kind"], d["is_test"]) for d in ts_dump["decls"]] == [
        ("frontend/a.tsx", "App", "component", False)
    ]
    py_dump = run("--dump-py")
    assert py_dump["counts"] == {"class": 1, "const": 1}
    lookup = run("M")
    assert [(c["file"], c["kind"], c["score"]) for c in lookup["candidates"]] == [("backend/m.py", "class", 0.4)]


def test_the_layout_comes_from_the_config() -> None:
    config = parse_config(FULL_CONFIG)
    assert symbol_resolve.SymbolLayout.from_config(config) == symbol_resolve.SymbolLayout(
        "backend", "frontend", ("frontend/components/", "backend/service_backend/")
    )
    layout = symbol_resolve.SymbolLayout("backend", "web/app", ("backend/app/",))
    assert layout.secondary_prefixes == ("web/app/", "backend/")


def _scored(layout: symbol_resolve.SymbolLayout, file: str) -> float:
    candidate = symbol_resolve.SymbolCandidate(
        name="x", file=file, start_line=1, end_line=1, kind="function", signature="", language="python"
    )
    return symbol_resolve.score_candidate(candidate, None, layout)


def test_ranking_prefixes_are_the_configured_ones() -> None:
    layout = symbol_resolve.SymbolLayout("api", "web", ("api/core/", "web/src/lib/"))
    assert _scored(layout, "api/core/auth.py") == 0.5
    assert _scored(layout, "web/src/lib/http.ts") == 0.5
    assert _scored(layout, "api/routes/users.py") == 0.4
    assert _scored(layout, "web/src/pages/home.tsx") == 0.4
    assert _scored(layout, "backend/core/auth.py") == 0.1


def test_a_backend_at_the_repository_root_ranks_every_file_at_least_second() -> None:
    layout = symbol_resolve.SymbolLayout(".", "web", ("pkg/",))
    assert _scored(layout, "pkg/mod.py") == 0.5
    assert _scored(layout, "scripts/tool.py") == 0.4


def test_layout_roots_decide_where_symbols_are_looked_up(typescript_toolchain, tmp_path: Path) -> None:
    _write_tree(tmp_path / "web" / "src", {"lib/util.ts": "export const current = 1;\n"})
    _write_tree(tmp_path / "api", {"core/ctx.py": "def current(): ...\n", "legacy/current.py": "def current(): ...\n"})
    _write_tree(tmp_path / "backend", {"stray.py": "def current(): ...\n"})
    layout = symbol_resolve.SymbolLayout("api", "web", ("api/core/",))
    index = _index(tmp_path, ts_facts.FactsCache(tmp_path, lambda: typescript_toolchain), layout)
    ranked = [(c.file, c.score) for c in index.resolve("current").candidates]
    assert ranked == [("api/core/ctx.py", 0.5), ("api/legacy/current.py", 0.4), ("web/src/lib/util.ts", 0.4)]


def test_only_git_visible_files_are_indexed(typescript_toolchain, tmp_path: Path) -> None:
    """Tracked and untracked-not-ignored files rank; ignored build output on either side never does."""
    _write_tree(
        tmp_path,
        {
            ".gitignore": "build/\nfrontend/src/generated/\n",
            "backend/app/ctx.py": "def current(): ...\n",
            "backend/build/lib/app/ctx.py": "def current(): ...\n",
            "frontend/src/lib/util.ts": "export const current = 1;\n",
            "frontend/src/generated/util.ts": "export const current = 2;\n",
        },
    )
    git_init(tmp_path)
    subprocess.run(["git", "add", "backend/app/ctx.py"], cwd=tmp_path, check=True)
    layout = symbol_resolve.SymbolLayout("backend", "frontend", ("backend/app/",))
    index = _index(tmp_path, ts_facts.FactsCache(tmp_path, lambda: typescript_toolchain), layout)
    assert [c.file for c in index.resolve("current").candidates] == [
        "backend/app/ctx.py",
        "frontend/src/lib/util.ts",
    ]
    assert [c.file for c in index.resolve("current", side="backend").candidates] == ["backend/app/ctx.py"]
    assert [rel for rel, _ in index.python_symbols([])] == ["backend/app/ctx.py"]
    assert [rel for rel, _ in index.typescript_decls([])] == ["frontend/src/lib/util.ts"]


def test_a_directory_that_git_cannot_list_is_an_error_not_a_filesystem_walk(tmp_path: Path) -> None:
    _write_tree(tmp_path / "backend", {"m.py": "def current(): ...\n"})
    toolchain = _unavailable_toolchain()
    index = symbol_resolve.SymbolIndex(tmp_path, ts_facts.FactsCache(tmp_path, lambda: toolchain), LAYOUT)
    with pytest.raises(RepoFilesError):
        index.resolve("current", side="backend")


def test_generated_output_directly_under_frontend_is_skipped(typescript_toolchain, tmp_path: Path) -> None:
    _write_tree(
        tmp_path / "frontend",
        {
            "app/build/page.tsx": "export const Built = 1;\n",
            "coverage/x.ts": "export const Built = 2;\n",
            "storybook-static/x.ts": "export const Built = 3;\n",
        },
    )
    _write_tree(tmp_path / "backend", {"coverage/__init__.py": "Built = 4\n"})
    index = _index(tmp_path, ts_facts.FactsCache(tmp_path, lambda: typescript_toolchain), LAYOUT)
    result = index.resolve("Built", side="both")
    assert sorted(c.file for c in result.candidates) == ["backend/coverage/__init__.py", "frontend/app/build/page.tsx"]
