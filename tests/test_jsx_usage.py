"""JSX usage from the TypeScript compiler: behaviour, parent and tag-kind edge cases, and the oracle.

Every behaviour fixture lives in one tree, read by one helper run per module
(``jsx_tree``); each test queries its own case directory. The whole tree is
also compared against ``oracles/jsx_oracle.cjs``, which walks the TypeScript
AST independently of ``ts/jsx_facts.cjs``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

import pytest
from fixture_repo import MINIMAL_CONFIG

from code_intel import code_paths, jsx_usage, ts_bridge, ts_facts
from code_intel.oracles import compare_facts

ORACLE = Path(compare_facts.__file__).parent / "jsx_oracle.cjs"

# ── Fixtures: {case/file: source}; every file lands under frontend/ ───────

# Syntax corpus (syn/): 11 files of hard JSX shapes, all checked against the oracle.
SYN = {
    "syn/cls.tsx": (
        "export class Klass extends React.Component {\n"
        "  handle = () => <Kid />;\n"
        "  renderRow(r) { return <Row r={r} />; }\n"
        "  render() {\n"
        "    return <Kid other={this.renderRow(1)} />;\n"
        "  }\n"
        "}\n"
        "const obj = { render() { return <Kid />; }, cell: () => <Kid /> };\n"
        "export const columns = [{ cell: ({ row }) => <Kid row={row} /> }];\n"
    ),
    "syn/defaults.tsx": "export default function Page() { return <Kid />; }\n",
    "syn/defaults2.tsx": "export default function () { return <Kid />; }\n",
    "syn/defaults3.tsx": "export default () => <Kid />;\n",
    "syn/defaults4.tsx": "export default memo(function Named() { return <Kid />; });\n",
    "syn/exprs.tsx": (
        "export function E1({ a, b, items }) {\n"
        '  const m = { a: <Kid />, b: [<Kid key="1" />, <Kid key="2" />] };\n'
        "  const rows = items.map((it) => <Kid key={it} />);\n"
        "  const sum = compute((a + b), (x) => <Kid />);\n"
        "  const v = a ? <Kid /> : b ? <Kid></Kid> : <Kid />;\n"
        "  const w = a && <Kid />;\n"
        '  const s = `pre ${a ? "x" : "y"} post`;\n'
        "  const t = `${<Kid />}`;\n"
        "  const u = css`a: ${(p) => <Kid />};`;\n"
        "  const cls = <div className={`a ${b}`}><Kid /></div>;\n"
        "  return (\n"
        "    <div>\n"
        "      <Kid {...a} />\n"
        "      <Kid{...a}/>\n"
        "      <Kid {...a} b={<Kid />} />\n"
        "      <Kid icon=<Kid /> />\n"
        "      <Kid render={() => <Kid />} />\n"
        "      <Kid/* c */ />\n"
        "      <Kid\n"
        '        a="1"\n'
        "      />\n"
        "      Don't<Kid />\n"
        "      {/* <Kid /> */}\n"
        '      {"<Kid />"}\n'
        "      <Kid>{(x) => <Kid />}</Kid>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
        "function* Gen() { yield <Kid />; }\n"
        "async function As() { return await <Kid />; }\n"
        "function Sw(x) { switch (x) { case 1: return <Kid />; default: return <Kid></Kid>; } }\n"
        "function Ifs(x) { if (x) <Kid />; for (;;) <Kid />; }\n"
        "const Sat = (() => <Kid />) satisfies Fn;\n"
        "function Uni() { return <Ärger />; }\n"
        "function Uni2() { return <Kid>é<Ärger>x</Ärger></Kid>; }\n"
    ),
    "syn/generic.tsx": (
        "export function G1() {\n"
        "  return (\n"
        "    <div>\n"
        "      <DataTable<Row> columns={c} />\n"
        "      <DataTable<Row>>x</DataTable>\n"
        "      <DataTable<Map<string, Row>> columns={c} />\n"
        "      <DataTable<{ a: string; b: () => void }> columns={c} />\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    ),
    "syn/hoc.tsx": (
        "export const A1 = memo(function A1(props) { return <Kid />; });\n"
        "export const A2 = memo(function Inner(props) { return <Kid />; });\n"
        "export const A3 = forwardRef((p, r) => <Kid ref={r} />);\n"
        "export const A4 = React.memo(React.forwardRef<HTMLDivElement, Props>((p, r) => <Kid />));\n"
        "export const A5 = React.forwardRef<HTMLDivElement, { a: string; b: number }>((p, r) => <Kid />);\n"
        "export const A6 = forwardRef<HTMLDivElement, { onChange?: (v: string) => void }>((p, r) => <Kid />);\n"
        "export const A7: React.FC<{ a: string; b: number }> = (p) => <Kid />;\n"
        "export const A8: Handler<(e: Event) => void> = (p) => <Kid />;\n"
        "export const A9 = <T,>(p: T) => <Kid />;\n"
        "export const B1 = <T extends Record<string, unknown>>(p: T) => <Kid />;\n"
        "export function B2<T extends Array<string>>(p: T) { return <Kid />; }\n"
        "export function B3<T = () => void>(p: T) { return <Kid />; }\n"
        "export const B4 = function Zed() { return <Kid />; };\n"
        "export const Zz = function Alpha() { return <Kid />; };\n"
        "export const B5 = (() => <Kid />);\n"
        "export const B6 = useCallback(async () => <Kid />, []);\n"
        'export const B7 = dynamic(() => import("x"), { loading: () => <Kid /> });\n'
        "export const B8 = (p: Props): JSX.Element => <Kid />;\n"
        "export const B9 = (p: Props): { a: string; b: number } => ({ a: <Kid /> });\n"
        "export const C1 = memo(Inner as any) || (() => <Kid />);\n"
        "export const C2 = styled.div`color: ${(p) => p.c};`;\n"
        "export const C3 = withX(Kid)((p) => <Kid />);\n"
        "export const C4 = observer(\n"
        "  // comment ( with paren\n"
        "  (p) => <Kid />,\n"
        ");\n"
        "export const C5 = memo<Props>(function C5(p) { return <Kid />; });\n"
        "export const C6 = memo((p) => <Kid />) as typeof Kid;\n"
        "export const C7 = cond ? () => <Kid /> : null;\n"
    ),
    "syn/member.tsx": (
        "export function M1() {\n"
        "  return (\n"
        '    <Foo.Bar a="1">\n'
        "      <Foo.Bar.Baz />\n"
        '      <svg:rect x="1" />\n'
        "      <a:b-c />\n"
        '      <my-el data-x="1" />\n'
        "      <Foo.Bar>{x}</Foo.Bar>\n"
        "      <></>\n"
        '      <React.Fragment key="k"><Kid /></React.Fragment>\n'
        "    </Foo.Bar>\n"
        "  );\n"
        "}\n"
    ),
    "syn/mod.mjs": "export const J2 = () => <JsComp />;\n",
    "syn/plain.js": 'export function J1() { return <JsComp a="1" />; }\n',
}

# Library-style shapes with hard parent attribution.
REAL = {
    # Type arguments holding `;` and `(`, and a doc comment before the callback.
    "real/popover.tsx": (
        "const PopoverContent = React.forwardRef<\n"
        "  React.ElementRef<typeof PopoverPrimitive.Content>,\n"
        "  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Content> & {\n"
        "    /**\n"
        "     * Portal target. Defaults to document.body (inside a modal Dialog; see\n"
        "     * react-remove-scroll).\n"
        "     */\n"
        "    container?: HTMLElement | null;\n"
        "  }\n"
        '>(({ className, align = "center", sideOffset = 4, container, ...props }, ref) => (\n'
        "  <PopoverPrimitive.Portal container={container ?? undefined}>\n"
        "    <PopoverPrimitive.Content ref={ref} align={align} sideOffset={sideOffset} />\n"
        "  </PopoverPrimitive.Portal>\n"
        "))\n"
    ),
    # forwardRef generics with an intersection type, a long body, then the
    # provider tree.
    "real/sidebar.tsx": (
        "const SidebarProvider = React.forwardRef<\n"
        "  HTMLDivElement,\n"
        '  React.ComponentProps<"div"> & {\n'
        "    defaultOpen?: boolean\n"
        "    open?: boolean\n"
        "    onOpenChange?: (open: boolean) => void\n"
        "  }\n"
        ">(\n"
        "  (\n"
        "    { defaultOpen = true, open: openProp, onOpenChange: setOpenProp, children, ...props },\n"
        "    ref\n"
        "  ) => {\n"
        "    const setOpen = React.useCallback(\n"
        "      (value: boolean | ((value: boolean) => boolean)) => {\n"
        '        const openState = typeof value === "function" ? value(open) : value\n'
        "        document.cookie = `${SIDEBAR_COOKIE_NAME}=${openState}; path=/;`\n"
        "      },\n"
        "      [setOpenProp, open]\n"
        "    )\n"
        "    return (\n"
        "      <SidebarContext.Provider value={contextValue}>\n"
        "        <TooltipProvider delayDuration={0}>\n"
        "          <div ref={ref} {...props}>{children}</div>\n"
        "        </TooltipProvider>\n"
        "      </SidebarContext.Provider>\n"
        "    )\n"
        "  }\n"
        ")\n"
    ),
    # The loading fallback is an object property of dynamic()'s options, not
    # the component.
    "real/squad-map-page.tsx": (
        "const SquadMapView = dynamic(\n"
        "  () =>\n"
        '    import("@/components/squad-map/SquadMapView").then(\n'
        "      (mod) => mod.SquadMapView\n"
        "    ),\n"
        "  {\n"
        "    ssr: false,\n"
        "    loading: () => (\n"
        '      <div className="h-full">\n'
        '        <RefreshCcw className="h-8 w-8" />\n'
        "      </div>\n"
        "    )\n"
        "  }\n"
        ");\n"
        "export default function SquadMapPage() {\n"
        "  return <SquadMapView />;\n"
        "}\n"
    ),
    # A loading fallback that takes props and returns a conditional element.
    "real/AppShell.tsx": (
        "const EchoIntegration = dynamic(\n"
        '  () => import("@/components/echo/EchoIntegration").then((module) => module.EchoIntegration),\n'
        "  {\n"
        "    ssr: false,\n"
        "    loading: ({ error }) => error\n"
        '      ? <ErrorBanner message="The assistant could not be loaded." />\n'
        "      : null,\n"
        "  },\n"
        ");\n"
    ),
}

# Behaviour fixtures: JSX forms, parents, tag kinds, test files and suffixes.
BEHAVIOUR = {
    "basic/pages/policies.tsx": (
        "function PoliciesAdminPage() {\n"
        "  return (\n"
        "    <div>\n"
        "      <DataTable columns={columns} data={policies} />\n"
        '      <RequireRole permission="x">\n'
        "        <span>child</span>\n"
        "      </RequireRole>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    ),
    "exact/table.tsx": (
        "function Page() {\n  return <><DataTableFoo columns={[]} /><Ctx.Provider value={1} /><Ctx /></>;\n}\n"
    ),
    "patterns/mixed.tsx": (
        "export default function DefaultPage() {\n"
        "  return <Widget />;\n"
        "}\n"
        "\n"
        "const ArrowPage = () => {\n"
        "  return <Widget />;\n"
        "};\n"
        "\n"
        "function NamedPage() {\n"
        "  function InnerNested() {\n"
        "    return <Widget />;\n"
        "  }\n"
        "  return InnerNested();\n"
        "}\n"
    ),
    "walk/real/page.tsx": "function Page() { return <Widget />; }\n",
    "walk/node_modules/pkg/page.tsx": "function Page() { return <Widget />; }\n",
    "walk/.next/server/page.tsx": "function Page() { return <Widget />; }\n",
    "walk/dist/page.tsx": "function Page() { return <Widget />; }\n",
    "selfclose/page.tsx": (
        "function Page() {\n  return (\n    <>\n      <Widget />\n      <Widget><span /></Widget>\n    </>\n  );\n}\n"
    ),
    "generics/page.tsx": (
        "const columns: ColumnDef<Widget>[] = [];\n"
        "type Props = React.FC<Widget>;\n"
        "const list: Array<Widget> = [];\n"
        "const [w, setW] = useState<Widget | null>(null);\n"
        'const x = api.get<Widget>("/w");\n'
        "const y = $<Widget>();\n"
        "// return <Widget />;\n"
        "/* <Widget /> */\n"
        "const s = '<Widget />';\n"
        "const t = `<Widget>${s}</Widget>`;\n"
        "type U = Foo<A> | Bar<Widget>;\n"
        "function Page() {\n"
        "  const el = <div>x</div>, other = Array<Widget>;\n"
        '  const pair = [<b key="b" />, useRef<Widget>(null)];\n'
        "  const cmp = a > b ? make<Widget>() : null;\n"
        "  return <div />;\n"
        "}\n"
    ),
    # A .ts file cannot contain JSX: `<Widget>value` there is a type assertion.
    "generics/cast.ts": "const w = <Widget>value;\nexport const v = <Widget />;\n",
    "forms/forms.tsx": (
        "function Page({ ok, items }: Props) {\n"
        '  const el = <Widget a="eq" />;\n'
        '  const list = [<Widget key="arr" />];\n'
        '  render(ok, <Widget a="arg" />);\n'
        '  return(<Widget a="paren" />);\n'
        "}\n"
        "function Other({ ok, items }: Props) {\n"
        "  return (\n"
        "    <Shell>\n"
        '      {ok && <Widget a="and" />}\n'
        '      {ok ? <Widget a="then" /> : <Widget a="else" />}\n'
        "      {items.map((i) =><Widget key={i} />)}\n"
        '      {<Widget a="brace" />}\n'
        "      <Widget\n"
        '        a="multiline"\n'
        "      />\n"
        '      <A><Widget a="child" /></A>\n'
        '      <Widget><Widget a="nested" /></Widget>\n'
        '      <Widget<Row> a="generic" />\n'
        "    </Shell>\n"
        "  );\n"
        "}\n"
    ),
    "braces/provider.tsx": (
        "export function MeProvider({ children }: Readonly<{ children: React.ReactNode }>) {\n"
        "  useEffect(() => {\n"
        "    const handleVisibilityChange = () => {\n"
        '      if (document.visibilityState === "visible") {\n'
        "        refresh();\n"
        "      }\n"
        "    };\n"
        '    document.addEventListener("visibilitychange", handleVisibilityChange);\n'
        "  }, []);\n"
        '  const label = "it\'s {"; // a brace in a string and an apostrophe\n'
        "  /* a stray } in a comment */\n"
        "  const pattern = /[{]/;\n"
        "  const tpl = `${label}{`;\n"
        "  if (loading) {\n"
        "    return <Widget />;\n"
        "  }\n"
        "  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;\n"
        "}\n"
    ),
    "parentforms/forms.tsx": (
        "const Typed: React.FC<Props> = ({ a }) => (\n"
        "  <Widget a={a} />\n"
        ");\n"
        "function Annotated(): JSX.Element {\n"
        "  return <Widget />;\n"
        "}\n"
        "function Shell() {\n"
        "  const handleOpen = useCallback(() => {\n"
        "    return <Widget />;\n"
        "  }, []);\n"
        "  return null;\n"
        "}\n"
        "const Forwarded = forwardRef<HTMLDivElement, Props>((props, ref) => <Widget ref={ref} />);\n"
        "const Short = () => <Other />;\n"
        "const columns = [{ cell: (row: Row) => <Widget row={row} /> }];\n"
    ),
    "tricky/separators.tsx": (
        "const Card = () => <p>Hello, <Widget /></p>;\n"
        'const Row = () => <li><b>a, b</b>; <Widget kind="x" /></li>;\n'
        "const Frag = () => <>Wait, <Widget /></>;\n"
        'const Less = (a: number) => a < 3, rows = [<Widget key="m" />];\n'
        'const data = [<Widget key="x" />];\n'
    ),
    "tricky/typeargs-page.tsx": (
        "export function Page() {\n  const grid = () => <DataTable<Row> rows={rows} />;\n  return <Widget />;\n}\n"
    ),
    "tricky/typeargs-presets.tsx": (
        'const Grid = () => <DataTable<Row> rows={rows} />;\nexport const presets = [<Widget key="a" />];\n'
    ),
    "tricky/compare.tsx": (
        'const F = (a) => a <b && c > d, nxt = [<Widget key="a" />];\n'
        'const G = (a) => a <b ? c > d : e, more = [<Widget key="b" />];\n'
        'export const after = [<Widget key="c" />];\n'
    ),
    "tricky/closers.tsx": (
        "const Card = () => <p>Hello ) <Widget /></p>;\n"
        'const Item = () => <li>a] first <Widget kind="x" /></li>;\n'
        "export function Page() {\n"
        '  const row = () => <li>a) first <Widget key="r" /></li>;\n'
        '  const tag = () => <li>[b] <Widget key="t" /></li>;\n'
        "  return null;\n"
        "}\n"
        'export const after = [<Widget key="z" />];\n'
    ),
    "tricky/apostrophe.tsx": (
        "export function Alpha() {\n"
        "  return (\n"
        "    <p>\n"
        "      Don't miss {items.map((i) => (\n"
        "        <Widget key={i} />\n"
        "      ))}\n"
        "      <Widget />\n"
        "    </p>\n"
        "  );\n"
        "}\n"
        "\n"
        "export function Beta() {\n"
        '  const label = "it\'s";\n'
        "  return <Widget />;\n"
        "}\n"
    ),
    "tricky/openers.tsx": (
        "export function Alpha() {\n"
        "  return (\n"
        "    <p>\n"
        "      Matches src/*.ts, see http://example.com\n"
        '      <Widget a="after-glob" />\n'
        "      Wrap in ` backticks\n"
        "    </p>\n"
        "  );\n"
        "}\n"
        "\n"
        'export const Beta = () => <Widget a="later" />;\n'
        'export const Gamma = () => <div>{/* <Widget a="commented" /> */}</div>;\n'
    ),
    "tricky/text-after.tsx": (
        "export const Delta = () => {\n"
        "  return <div>Hello<Widget /></div>;\n"
        "};\n"
        "export function Epsilon({ n }: Props) {\n"
        "  return (\n"
        "    <p>\n"
        '      <b>Total</b>: 5<Widget a="after-tag" />\n'
        '      {n}items<Widget a="after-expr" />\n'
        '      Loading...<Widget a="dots" />\n'
        "    </p>\n"
        "  );\n"
        "}\n"
    ),
    "tricky/type-params.tsx": (
        "export const id = <T,>(v: T) => v;\n"
        "export const pick = <T extends Row>(v: T) => v;\n"
        "export const keep = <const T,>(v: T) => v;\n"
        'export const View = () => <T a="1" />;\n'
        "export const Wrap = () => (\n"
        "  <p>\n"
        '    <T extends="x">text</T>\n'
        "  </p>\n"
        ");\n"
    ),
    "tricky/type-params-comments.tsx": (
        'export const keep = <T,>(row: T, /* fallback: <Widget a="old" /> */) => row;\n'
        "export function Page() {\n"
        "  const e = <div />\n"
        '  foo<Bar>("x", /* <Widget a="old2" /> */ 2)\n'
        '  return <Widget a="real" />;\n'
        "}\n"
    ),
    "tricky/tag-kind.tsx": (
        "export const Page = () => (\n"
        "  <div>\n"
        "    <Widget onClick={() => go()} />\n"
        "    <Widget\n"
        '      a="1"\n'
        "    />\n"
        '    <Widget title="a > b" />\n'
        "    <Widget<Row> rows={r} />\n"
        "    <Widget a={x > 1 ? 1 : 2}>child</Widget>\n"
        "    <Widget\n"
        '      b="2"\n'
        "    >\n"
        "      text\n"
        "    </Widget>\n"
        "  </div>\n"
        ");\n"
    ),
    "tricky/strings.tsx": (
        "const html = \"<Widget a='x' />\";\n"
        "const tpl = `<Widget>${html}</Widget>`;\n"
        "export const Page = () => <Widget a=<B /> />;\n"
    ),
    "tricky/sibling-chain.tsx": (
        "export const Chain = () => (\n  <p>\n" + "    a<b/>\n" * 600 + '    say "hi" <Widget />\n  </p>\n);\n'
    ),
    "tricky/long-text.tsx": (
        "export const Text = () => <div>"
        + "word " * 600
        + 'end<Widget a="text" /></div>;\n'
        + 'export const Tag = () => <Widget data="'
        + "x" * 25000
        + '" />;\n'
    ),
    "tricky/extends-child.tsx": (
        "export const E = () => (\n"
        "  <p>\n"
        '    <Foo extends bar="x">say "hi" `tick</Foo>\n'
        '    <Foo a="later" />\n'
        "  </p>\n"
        ");\n"
        'export const g = <T extends bar="x">(v: T) => "<Foo />";\n'
    ),
    "tricky/generic-element.tsx": "const Render = () => <List<Row, Error>><Item /></List>;\n",
    "tricky/deep-attributes.tsx": "export const E = () => <W " + "a=<B " * 300 + "/>" * 300 + " />;\n",
    "ts-reads/attr.tsx": 'export const A = () => <Bar el=<Foo extends a="" /> />;\n',
    "ts-reads/kids.tsx": 'export const B = () => <div><T a="x, y)"><T extends U>a) b<Foo></Foo></T></T></div>;\n',
    "ts-reads/text.tsx": 'export const C = () => <Baz.Q k="v">x =&gt; y<Foo><Baz.Q /></Foo></Baz.Q>;\n',
    "ts-reads/brackets.tsx": "const D=()=>[<div>]<b>(</b></div>,<i><Foo/></i>]\n",
    "ts-reads/types.tsx": (
        'type F = <T>(a: T) => T;\nlet g: <T>(a: T) => T;\n// <Foo a="old" />\nexport const View = () => <Foo />;\n'
    ),
    "ts-reads/alias.tsx": (
        "type Render = <Row>(row: Row) => string;\nconst A = () => <Foo />;\nfunction B() { return <Row>x</Row>; }\n"
    ),
    "ts-reads/member.tsx": (
        "interface P { render: <Row>(r: Row) => string }\nconst A = () => <Foo />;\nconst B = () => <Row>x</Row>;\n"
    ),
    "ts-reads/string.tsx": "type F = <T>(a: T) => T;\nconst A = () => <Foo />;\nconst s = '</T>';\n",
    "ts-reads/comment.tsx": "type F = <T>(a: T) => T;\nconst A = () => <Foo />;\n// </T>\n",
    "ts-reads/block.tsx": "type F = <T>(a: T) => T;\nconst A = () => <Foo />;\n/* old: <T>x</T> */\n",
    "ts-reads/hidden.tsx": (
        "type F = <T>(a: T) => T;\n"
        'const s = "<Foo />";\n'
        "// <Foo />\n"
        "const q = useState<Foo>(null);\n"
        "const B = () => <T>x</T>;\n"
    ),
    "ts-reads/statement.tsx": (
        "function A(x) { if (x) <Foo />; return null; }\n"
        "function B() { return <p>< Foo /></p>; }\n"
        "const n = f(x) < Foo;\n"
    ),
    "ts-reads/for-await.tsx": (
        "async function A(b) { for await (const a of b) <Foo />; }\nconst n = g.await(x) < Foo;\n"
    ),
    "ts-reads/default.tsx": "const f = (x = <Foo>(a</Foo>) => x;\n",
    "ts-reads/field.tsx": "export const Field = ({ hint = <Foo>(optional</Foo> }) => hint;\n",
    "ts-reads/fn.tsx": "function g(a = <Foo>(</Foo>) {}\n",
    "ts-reads/quote.tsx": "const A = () => <Foo>(don't</Foo>;\nconst s = \"x') => \";\n",
    "ts-reads/arrow-text.tsx": "const B = () => <Foo>(a) => b</Foo>;\n",
    "ts-reads/type-param.tsx": "type F = <Foo>(a: Foo) => Foo;\nconst C = () => <Foo>z</Foo>;\n",
    "testfiles/components/Page.tsx": "function Page() { return <Widget />; }\n",
    "testfiles/components/__tests__/Page.test.tsx": "function Page() { return <Widget />; }\n",
    "testfiles/components/Page.spec.tsx": "function Page() { return <Widget />; }\n",
    "testfiles/test/render.tsx": "function Page() { return <Widget />; }\n",
    "suffixes/a.jsx": "export const A = () => <Js />;\n",
    "suffixes/b.js": "export const B = () => <Js />;\n",
    "suffixes/c.mjs": "export const C = () => <Js />;\n",
    "suffixes/d.cjs": "const D = () => <Js />;\n",
    "suffixes/e.ts": "export const E = <Js>value;\n",
    "suffixes/f.mts": "export const F = 1;\n",
}

# Edge corpus (edges/): parent-rule and position edge cases, each also
# checked against the oracle, which derives parents by walking up from the
# element rather than with the helper's own code.
EDGES = {
    "edges/anon_default.tsx": "export default function () {\n  return <A1 />;\n}\n",
    "edges/nested_wrappers.tsx": "const X2 = memo(forwardRef((p, r) => <A2 />));\n",
    "edges/conditional_arrows.tsx": "function Outer3() {\n  const X3 = cond ? () => <A3 /> : () => <B3 />;\n}\n",
    "edges/satisfies.tsx": "const X4 = (() => <A4 />) satisfies FC;\n",
    "edges/curried.tsx": "const X5 = withA(B)(() => <A5 />);\n",
    "edges/class_render.tsx": "class C6 extends Component {\n  render() { return <A6 />; }\n}\n",
    "edges/object_method.tsx": "function Outer7() {\n  const cols = { cell() { return <A7 />; } };\n}\n",
    "edges/generic_default.tsx": "function F8<T = () => void>(p: T) {\n  return <A8 />;\n}\n",
    "edges/param_default.tsx": "function F9(icon = <A9 />) {\n  return icon;\n}\n",
    "edges/iife.tsx": "const X10 = (() => <A10 />)();\n",
    "edges/named_in_memo.tsx": "export const X11 = React.memo(function Inner11() {\n  return <A11 />;\n});\n",
    "edges/astral.tsx": "const s = '\U0001f600\U0001f600'; const \u00dc14 = () => <\u00dcn\u00ef14 />;\n",
    "edges/bom.tsx": "\ufeff<A16 />;\n",
    "edges/u2028.tsx": "const s17 = 'a\u2028b';\nconst X17 = () => <A17 />;\n",
    "edges/dynamic_loading.tsx": "const Map18 = dynamic(() => import('./m'), { loading: () => <Spin18 /> });\n",
    "edges/class_field.tsx": "function Outer20() {\n  class K { renderIt = () => <A20 />; }\n}\n",
    "edges/new_argument.tsx": "const X22 = new Thing(() => <A22 />);\n",
    "edges/utf16be.tsx": "\ufeffconst X23 = () => <A23 />;\n".encode("utf-16-be"),
    "edges/utf16le.tsx": "\ufeffconst X24 = () => <A24 />;\n".encode("utf-16-le"),
}

FIXTURES = {**SYN, **REAL, **BEHAVIOUR, **EDGES}


@dataclass
class JsxTree:
    repo: Path
    facts: ts_facts.FactsCache
    toolchain: ts_bridge.Toolchain

    def find(self, case: str, name: str, **kwargs) -> list[jsx_usage.JsxUsage]:
        problems: list = kwargs.pop("problems", [])
        return jsx_usage.find_jsx_usage(
            name,
            Path("frontend") / case,
            facts=self.facts,
            frontend_root="frontend",
            repo_root=self.repo,
            problems=problems,
            **kwargs,
        )

    def rows(self, case: str, name: str, *fields: str, **kwargs) -> list[tuple]:
        return [tuple(getattr(u, f) for f in fields) for u in self.find(case, name, **kwargs)]


def _write_tree(root: Path, files: Mapping[str, str | bytes]) -> None:
    for rel, source in files.items():
        path = root / "frontend" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, bytes):
            path.write_bytes(source)
        else:
            path.write_text(source, encoding="utf-8", newline="")


@pytest.fixture(scope="module")
def jsx_tree(typescript_toolchain: ts_bridge.Toolchain, tmp_path_factory: pytest.TempPathFactory) -> JsxTree:
    repo = tmp_path_factory.mktemp("jsx_repo")
    _write_tree(repo, FIXTURES)
    facts = ts_facts.FactsCache(repo, lambda: typescript_toolchain)
    # One helper run reads the whole tree; every query after it is a cache hit.
    problems: list = []
    files = code_paths.iter_source_files(repo / "frontend", code_paths.JSX_SUFFIXES, repo, problems)
    facts.facts_for(files)
    assert facts.stats()["helper_runs"] == 1
    return JsxTree(repo, facts, typescript_toolchain)


def _fresh(tmp_path: Path, toolchain: ts_bridge.Toolchain, files: dict[str, str]) -> JsxTree:
    _write_tree(tmp_path, files)
    return JsxTree(tmp_path, ts_facts.FactsCache(tmp_path, lambda: toolchain), toolchain)


# ── Oracle ───────────────────────────────────────────────────────────────


def test_jsx_fixtures_equal_the_typescript_oracle(jsx_tree: JsxTree, tmp_path: Path) -> None:
    """Every element of every fixture (the syntax corpus, the real-repo shapes and
    the ported behaviour cases) equals oracles/jsx_oracle.cjs in file, tag, line,
    column, kind, parent and test flag."""
    out = tmp_path / "oracle.json"
    assert jsx_tree.toolchain.node is not None
    subprocess.run(
        [
            jsx_tree.toolchain.node,
            str(ORACLE),
            str(jsx_tree.toolchain.typescript_dir),
            str(jsx_tree.repo),
            "frontend",
            str(jsx_tree.repo / "frontend"),
            str(out),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    oracle = json.loads(out.read_text(encoding="utf-8"))
    problems: list = []
    rows = jsx_usage.all_jsx_elements(
        Path("frontend"),
        facts=jsx_tree.facts,
        frontend_root="frontend",
        repo_root=jsx_tree.repo,
        include_tests=True,
        problems=problems,
    )
    tool = [
        {
            "file": r.file,
            "tag": r.tag,
            "line": r.line,
            "column": r.column,
            "kind": r.kind,
            "parent": r.parent,
            "is_test": r.is_test,
        }
        for r in rows
    ]
    result = compare_facts.compare(
        [compare_facts._jsx_row(r) for r in oracle["elements"]],
        tool,
        ("file", "line", "column", "tag"),
        compare_facts.JSX_FIELDS,
    )
    assert (result["missing"], result["extra"], result["mismatches"]) == ([], [], {})
    assert result["tool"] > 300
    # Syntax errors in the deliberately odd fixtures are reported, never fatal.
    assert all(p.kind == "typescript_syntax_error" for p in problems)


@pytest.mark.parametrize(
    ("case", "tag", "expected"),
    [
        ("edges/anon_default.tsx", "A1", (2, 10, None)),
        ("edges/nested_wrappers.tsx", "A2", (1, 38, "X2")),
        ("edges/conditional_arrows.tsx", "B3", (2, 42, "Outer3")),
        ("edges/satisfies.tsx", "A4", (1, 19, "X4")),
        ("edges/curried.tsx", "A5", (1, 27, "X5")),
        ("edges/class_render.tsx", "A6", (2, 21, None)),
        ("edges/object_method.tsx", "A7", (2, 34, "Outer7")),
        ("edges/generic_default.tsx", "A8", (2, 10, "F8")),
        ("edges/param_default.tsx", "A9", (1, 20, "F9")),
        ("edges/iife.tsx", "A10", (1, 20, None)),
        ("edges/named_in_memo.tsx", "A11", (2, 10, "Inner11")),
        ("edges/astral.tsx", "\u00dcn\u00ef14", (1, 35, "\u00dc14")),
        ("edges/bom.tsx", "A16", (1, 1, None)),
        # U+2028 inside a string does not end the line, as in editors and rg.
        ("edges/u2028.tsx", "A17", (2, 19, "X17")),
        ("edges/dynamic_loading.tsx", "Spin18", (1, 61, None)),
        ("edges/class_field.tsx", "A20", (2, 30, "Outer20")),
        ("edges/new_argument.tsx", "A22", (1, 29, None)),
        ("edges/utf16be.tsx", "A23", (1, 19, "X23")),
        ("edges/utf16le.tsx", "A24", (1, 19, "X24")),
    ],
)
def test_edge_corpus_lines_columns_and_parents(jsx_tree: JsxTree, case: str, tag: str, expected: tuple) -> None:
    found = [(u.line, u.column, u.parent_function) for u in jsx_tree.find("edges", tag) if u.file == f"frontend/{case}"]
    assert found == [expected]


def test_the_oracle_shares_no_code_with_the_helper() -> None:
    """The oracle must derive parents on its own, or the comparison only checks
    the helper against itself."""
    source = ORACLE.read_text(encoding="utf-8")
    assert "require(" in source and "/ts/" not in source and "common.cjs" not in source
    assert "readmeFunctionName" not in source


def test_syntax_corpus_parents_and_kinds(jsx_tree: JsxTree) -> None:
    """Spot checks from the syntax corpus (syn/): methods, class fields and object
    properties are not named functions; fragments are not elements."""
    assert jsx_tree.rows("syn", "Kid", "file", "line", "parent_function")[:8] == [
        ("frontend/syn/cls.tsx", 2, None),
        ("frontend/syn/cls.tsx", 5, None),
        ("frontend/syn/cls.tsx", 8, None),
        ("frontend/syn/cls.tsx", 8, None),
        ("frontend/syn/cls.tsx", 9, None),
        ("frontend/syn/defaults.tsx", 1, "Page"),
        ("frontend/syn/defaults2.tsx", 1, None),
        ("frontend/syn/defaults3.tsx", 1, None),
    ]
    assert jsx_tree.rows("syn", "Row", "line", "parent_function") == [(3, None)]
    member = jsx_tree.find("syn", "Foo.Bar")
    assert [(u.line, u.tag_kind) for u in member] == [(3, "open"), (8, "open")]
    assert jsx_tree.rows("syn", "svg:rect", "line") == [(5,)]
    assert jsx_tree.rows("syn", "a:b-c", "line") == [(6,)]
    assert jsx_tree.rows("syn", "my-el", "line") == [(7,)]
    assert jsx_tree.rows("syn", "React.Fragment", "line", "tag_kind") == [(10, "open")]
    assert jsx_tree.find("syn", "") == []


# ── Parents and tag kinds ─────────────────────────────────────────────────


def test_parent_survives_type_syntax_in_function_headers(jsx_tree: JsxTree) -> None:
    """The parent survives type arguments with `;`/`(`, `React.FC<{…}>`, `<T extends Record<…>>`,
    `<T = () => void>`, parenthesised arrows, `satisfies`, memo(forwardRef()),
    curried `withX(K)(...)` and a comment before the callback."""
    by_line = {u.line: u.parent_function for u in jsx_tree.find("syn", "Kid") if u.file.endswith("hoc.tsx")}
    assert by_line == {
        1: "A1",
        2: "Inner",
        3: "A3",
        4: "A4",
        5: "A5",
        6: "A6",
        7: "A7",
        8: "A8",
        9: "A9",
        10: "B1",
        11: "B2",
        12: "B3",
        13: "Zed",
        14: "Alpha",
        15: "B5",
        16: "B6",
        17: None,
        18: "B8",
        19: "B9",
        20: None,
        22: "C3",
        25: "C4",
        27: "C5",
        28: "C6",
        29: None,
    }
    sat = [u for u in jsx_tree.find("syn", "Kid") if u.file.endswith("exprs.tsx") and u.line == 33]
    assert [u.parent_function for u in sat] == ["Sat"]


def test_parent_of_real_popover_and_sidebar_headers(jsx_tree: JsxTree) -> None:
    """forwardRef headers with long type arguments keep their parent."""
    assert jsx_tree.rows("real", "PopoverPrimitive.Portal", "line", "parent_function") == [(11, "PopoverContent")]
    assert jsx_tree.rows("real", "PopoverPrimitive.Content", "line", "parent_function") == [(12, "PopoverContent")]
    assert jsx_tree.rows("real", "SidebarContext.Provider", "line", "parent_function") == [(21, "SidebarProvider")]
    assert jsx_tree.rows("real", "TooltipProvider", "line", "parent_function") == [(22, "SidebarProvider")]
    assert jsx_tree.rows("real", "div", "file", "parent_function") == [
        ("frontend/real/sidebar.tsx", "SidebarProvider"),
        ("frontend/real/squad-map-page.tsx", None),
    ]


def test_dynamic_loading_fallback_is_not_the_component(jsx_tree: JsxTree) -> None:
    """Only a function passed directly as a call argument takes the
    variable's name; dynamic()'s `loading:` option is an object property
    (squad-map/page.tsx:20-23, AppShell.tsx:52, hoc.tsx B7)."""
    assert jsx_tree.rows("real", "RefreshCcw", "parent_function") == [(None,)]
    assert jsx_tree.rows("real", "ErrorBanner", "line", "parent_function") == [(6, None)]
    assert jsx_tree.rows("real", "SquadMapView", "parent_function") == [("SquadMapPage",)]
    # A callback passed straight to a call that initializes a variable is named
    # after it (the documented rule): `const rows = items.map((it) => …)`.
    exprs = [u for u in jsx_tree.find("syn", "Kid") if u.file.endswith("exprs.tsx")]
    assert [u.parent_function for u in exprs if u.line == 3] == ["rows"]
    assert [u.parent_function for u in exprs if u.line == 4] == ["sum"]


def test_named_function_expression_wins_over_the_variable(jsx_tree: JsxTree) -> None:
    hoc = {u.line: u.parent_function for u in jsx_tree.find("syn", "Kid") if u.file.endswith("hoc.tsx")}
    assert (hoc[13], hoc[14]) == ("Zed", "Alpha")
    assert jsx_tree.rows("syn", "Kid", "file", "parent_function")[8] == ("frontend/syn/defaults4.tsx", "Named")


def test_spread_right_after_the_tag_name(jsx_tree: JsxTree) -> None:
    exprs = [u for u in jsx_tree.find("syn", "Kid") if u.file.endswith("exprs.tsx")]
    assert [(u.line, u.column, u.tag_kind) for u in exprs if u.line == 14] == [(14, 7, "self_close")]


def test_non_ascii_tags_get_the_right_kind(jsx_tree: JsxTree) -> None:
    assert jsx_tree.rows("syn", "Ärger", "line", "column", "tag_kind") == [(34, 25, "self_close"), (35, 32, "open")]


def test_fragments_suffixes_and_ts_files(jsx_tree: JsxTree) -> None:
    """Fragments are not elements; .jsx/.js/.mjs/.cjs are scanned; .ts/.mts are
    not read at all (TypeScript reads `<Js>value` there as a type assertion)."""
    assert jsx_tree.rows("suffixes", "Js", "file", "parent_function") == [
        ("frontend/suffixes/a.jsx", "A"),
        ("frontend/suffixes/b.js", "B"),
        ("frontend/suffixes/c.mjs", "C"),
        ("frontend/suffixes/d.cjs", "D"),
    ]
    assert jsx_tree.rows("syn", "JsComp", "file") == [("frontend/syn/mod.mjs",), ("frontend/syn/plain.js",)]
    kinds = {u.tag_kind for u in jsx_tree.find("selfclose", "Widget")}
    assert kinds == {"open", "self_close"}
    assert get_args(jsx_usage.TagKind) == ("open", "self_close")


# ── Behaviour ────────────────────────────────────────────────────────────


def test_find_jsx_usage_self_close_and_open_tags(jsx_tree: JsxTree) -> None:
    [table] = jsx_tree.find("basic", "DataTable")
    assert (table.file, table.line, table.column, table.tag_kind) == (
        "frontend/basic/pages/policies.tsx",
        4,
        7,
        "self_close",
    )
    assert table.parent_function == "PoliciesAdminPage"
    assert table.snippet == "<DataTable columns={columns} data={policies} />"
    assert jsx_tree.rows("basic", "RequireRole", "tag_kind", "parent_function") == [("open", "PoliciesAdminPage")]


def test_find_jsx_usage_generic_element_tags(jsx_tree: JsxTree) -> None:
    """`<DataTable<Row> …>` is a DataTable element; type arguments are not part of the tag."""
    assert jsx_tree.rows("syn", "DataTable", "line", "tag_kind") == [
        (4, "self_close"),
        (5, "open"),
        (6, "self_close"),
        (7, "self_close"),
    ]
    assert jsx_tree.rows("tricky", "Item", "parent_function") == [("Render",)]


def test_find_jsx_usage_matches_the_tag_exactly(jsx_tree: JsxTree) -> None:
    """Exact tag equality, so neither longer names nor prefixes match."""
    assert jsx_tree.find("exact", "DataTable") == []
    assert jsx_tree.rows("exact", "Ctx.Provider", "column") == [(41,)]
    assert jsx_tree.rows("exact", "Ctx", "column") == [(67,)]
    assert jsx_tree.find("exact", "Ctx.provider") == []


def test_find_jsx_usage_parent_function_patterns(jsx_tree: JsxTree) -> None:
    assert [u.parent_function for u in jsx_tree.find("patterns", "Widget")] == [
        "DefaultPage",
        "ArrowPage",
        "InnerNested",
    ]


def test_find_jsx_usage_skips_node_modules_and_dot_dirs(jsx_tree: JsxTree) -> None:
    assert jsx_tree.rows("walk", "Widget", "file") == [("frontend/walk/real/page.tsx",)]


def test_find_jsx_usage_exclude_self_close(jsx_tree: JsxTree) -> None:
    assert len(jsx_tree.find("selfclose", "Widget")) == 2
    assert jsx_tree.rows("selfclose", "Widget", "tag_kind", include_self_close=False) == [("open",)]


def test_find_jsx_usage_ignores_typescript_generics_comments_and_ts_files(jsx_tree: JsxTree) -> None:
    assert jsx_tree.find("generics", "Widget", include_tests=True) == []


def test_find_jsx_usage_counts_real_jsx_forms(jsx_tree: JsxTree) -> None:
    usages = jsx_tree.find("forms", "Widget")
    assert len(usages) == 14
    assert [u.parent_function for u in usages[:4]] == ["Page"] * 4
    assert [u.parent_function for u in usages[4:]] == ["Other"] * 5 + ["Other"] * 5
    assert [u.column for u in usages if u.snippet.startswith("<A><Widget")] == [10]
    nested = [u for u in usages if u.snippet.startswith("<Widget><Widget")]
    assert [(u.column, u.tag_kind) for u in nested] == [(7, "open"), (15, "self_close")]


def test_find_jsx_usage_parent_function_uses_the_enclosing_function(jsx_tree: JsxTree) -> None:
    assert [u.parent_function for u in jsx_tree.find("braces", "Widget")] == ["MeProvider"]
    assert [u.parent_function for u in jsx_tree.find("braces", "Ctx.Provider")] == ["MeProvider"]


def test_find_jsx_usage_parent_function_forms(jsx_tree: JsxTree) -> None:
    parents = [u.parent_function for u in jsx_tree.find("parentforms", "Widget")]
    assert parents == ["Typed", "Annotated", "handleOpen", "Forwarded", None]


TRICKY = {
    "separators": ("tricky/separators.tsx", "Widget", [(1, "Card"), (2, "Row"), (3, "Frag"), (4, None), (5, None)]),
    "type_arguments_in_tags": ("tricky/typeargs-page.tsx", "Widget", [(3, "Page")]),
    "type_arguments_presets": ("tricky/typeargs-presets.tsx", "Widget", [(2, None)]),
    "comparison_is_not_a_tag": ("tricky/compare.tsx", "Widget", [(1, None), (2, None), (3, None)]),
    "stray_closers": ("tricky/closers.tsx", "Widget", [(1, "Card"), (2, "Item"), (4, "row"), (5, "tag"), (8, None)]),
    "apostrophe": ("tricky/apostrophe.tsx", "Widget", [(5, "Alpha"), (7, "Alpha"), (14, "Beta")]),
    "literal_openers": ("tricky/openers.tsx", "Widget", [(5, "Alpha"), (11, "Beta")]),
    "tag_after_text": (
        "tricky/text-after.tsx",
        "Widget",
        [(2, "Delta"), (7, "Epsilon"), (8, "Epsilon"), (9, "Epsilon")],
    ),
    "type_parameter_lists": ("tricky/type-params.tsx", "T", [(4, "View"), (7, "Wrap")]),
    "type_parameter_comments": ("tricky/type-params-comments.tsx", "Widget", [(5, "Page")]),
    "sibling_chain": ("tricky/sibling-chain.tsx", "Widget", [(603, "Chain")]),
    "long_text_and_tag": ("tricky/long-text.tsx", "Widget", [(1, "Text"), (2, "Tag")]),
    "extends_in_children": ("tricky/extends-child.tsx", "Foo", [(3, "E"), (4, "E")]),
    "deep_bare_attributes": ("tricky/deep-attributes.tsx", "W", [(1, "E")]),
    "statement_position": ("ts-reads/statement.tsx", "Foo", [(1, "A"), (2, "B")]),
    "for_await": ("ts-reads/for-await.tsx", "Foo", [(1, "A")]),
    "type_syntax": ("ts-reads/types.tsx", "Foo", [(4, "View")]),
    "hidden": ("ts-reads/hidden.tsx", "Foo", []),
}


@pytest.mark.parametrize("case", sorted(TRICKY))
def test_find_jsx_usage_reads_jsx_as_typescript_does(jsx_tree: JsxTree, case: str) -> None:
    """Text-level traps for JSX scanning: JSX text holding separators, closers,
    quotes, globs or backticks; type parameters and arguments next to JSX; tags
    right after text; long and deep elements. TypeScript reads each one."""
    rel, name, expected = TRICKY[case]
    usages = [u for u in jsx_tree.find(str(Path(rel).parent), name) if u.file == f"frontend/{rel}"]
    assert [(u.line, u.parent_function) for u in usages] == expected


def test_find_jsx_usage_columns_and_parents_in_typescript_reads(jsx_tree: JsxTree) -> None:
    rows = sorted(
        (u.file.split("/")[-1], u.line, u.column, u.parent_function) for u in jsx_tree.find("ts-reads", "Foo")
    )
    assert rows == [
        ("alias.tsx", 2, 17, "A"),
        ("arrow-text.tsx", 1, 17, "B"),
        ("attr.tsx", 1, 32, "A"),
        ("block.tsx", 2, 17, "A"),
        ("brackets.tsx", 1, 38, "D"),
        ("comment.tsx", 2, 17, "A"),
        ("default.tsx", 1, 16, "f"),
        ("field.tsx", 1, 32, "Field"),
        ("fn.tsx", 1, 16, "g"),
        ("for-await.tsx", 1, 48, "A"),
        ("kids.tsx", 1, 59, "B"),
        ("member.tsx", 2, 17, "A"),
        ("quote.tsx", 1, 17, "A"),
        ("statement.tsx", 1, 24, "A"),
        ("statement.tsx", 2, 26, "B"),
        ("string.tsx", 2, 17, "A"),
        ("text.tsx", 1, 46, "C"),
        ("type-param.tsx", 2, 17, "C"),
        ("types.tsx", 4, 27, "View"),
    ]


def test_find_jsx_usage_tag_kind_reads_the_whole_tag(jsx_tree: JsxTree) -> None:
    tag_kind = [u for u in jsx_tree.find("tricky", "Widget") if u.file.endswith("tag-kind.tsx")]
    assert [(u.line, u.tag_kind) for u in tag_kind] == [
        (3, "self_close"),
        (4, "self_close"),
        (7, "self_close"),
        (8, "self_close"),
        (9, "open"),
        (10, "open"),
    ]
    open_only = [
        u for u in jsx_tree.find("tricky", "Widget", include_self_close=False) if u.file.endswith("tag-kind.tsx")
    ]
    assert [u.line for u in open_only] == [9, 10]


def test_find_jsx_usage_skips_tags_in_strings_and_reads_element_attributes(jsx_tree: JsxTree) -> None:
    strings = [u for u in jsx_tree.find("tricky", "Widget") if u.file.endswith("strings.tsx")]
    assert [(u.line, u.tag_kind) for u in strings] == [(3, "self_close")]
    assert [u.line for u in jsx_tree.find("tricky", "B") if u.file.endswith("strings.tsx")] == [3]


def test_find_jsx_usage_excludes_or_marks_test_files(jsx_tree: JsxTree) -> None:
    assert jsx_tree.rows("testfiles", "Widget", "file", "is_test") == [
        ("frontend/testfiles/components/Page.tsx", False)
    ]
    assert jsx_tree.rows("testfiles", "Widget", "file", "is_test", include_tests=True) == [
        ("frontend/testfiles/components/Page.spec.tsx", True),
        ("frontend/testfiles/components/Page.tsx", False),
        ("frontend/testfiles/components/__tests__/Page.test.tsx", True),
        ("frontend/testfiles/test/render.tsx", True),
    ]


def test_find_jsx_usage_rejects_roots_outside_the_repo_or_not_directories(jsx_tree: JsxTree, tmp_path: Path) -> None:
    for bad_root in (tmp_path, Path("/"), jsx_tree.repo / ".." / "elsewhere", Path("../..")):
        with pytest.raises(jsx_usage.JsxRootError, match="outside the repo root"):
            jsx_usage.find_jsx_usage(
                "Widget", bad_root, facts=jsx_tree.facts, frontend_root="frontend", repo_root=jsx_tree.repo, problems=[]
            )
    with pytest.raises(jsx_usage.JsxRootError, match="not a directory"):
        jsx_usage.find_jsx_usage(
            "Widget",
            Path("missing"),
            facts=jsx_tree.facts,
            frontend_root="frontend",
            repo_root=jsx_tree.repo,
            problems=[],
        )
    with pytest.raises(jsx_usage.JsxRootError, match="not a directory"):
        jsx_usage.find_jsx_usage(
            "",
            Path("frontend/basic/pages/policies.tsx"),
            facts=jsx_tree.facts,
            frontend_root="frontend",
            repo_root=jsx_tree.repo,
            problems=[],
        )


def test_find_jsx_usage_relative_and_absolute_roots_agree(jsx_tree: JsxTree) -> None:
    relative = jsx_tree.find("basic", "DataTable")
    absolute = jsx_usage.find_jsx_usage(
        "DataTable",
        jsx_tree.repo / "frontend" / "basic",
        facts=jsx_tree.facts,
        frontend_root="frontend",
        repo_root=jsx_tree.repo,
        problems=[],
    )
    assert relative == absolute


# ── Diagnostics, caching and the toolchain ───────────────────────────────


def test_find_jsx_usage_caps_the_snippet_of_a_long_line(typescript_toolchain, tmp_path: Path) -> None:
    """20,000 elements on one line: each snippet is the trimmed line capped at
    500 code points, and the line is read once."""
    tree = _fresh(
        tmp_path,
        typescript_toolchain,
        {"long.tsx": "  const e = <p>" + "<Foo/>" * 20000 + '</p>;\n  export const N = () => <Foo a="1" />;\n'},
    )
    usages = tree.find(".", "Foo")
    assert len(usages) == 20001
    assert {len(u.snippet) for u in usages[:-1]} == {500}
    assert usages[0].snippet.startswith("const e = <p><Foo/>")
    assert usages[-1].snippet == 'export const N = () => <Foo a="1" />;'
    assert usages[-1].parent_function == "N"


def test_find_jsx_usage_syntax_errors_are_diagnostics_not_fatal(typescript_toolchain, tmp_path: Path) -> None:
    tree = _fresh(
        tmp_path,
        typescript_toolchain,
        {"good.tsx": "function Page() { return <Widget />; }\n", "bad.tsx": "function Bad() { return <Widget>; }\n"},
    )
    problems: list = []
    usages = tree.find(".", "Widget", problems=problems)
    assert [(u.file, u.parent_function) for u in usages] == [("frontend/bad.tsx", "Bad"), ("frontend/good.tsx", "Page")]
    assert [(p.kind, p.file, p.step) for p in problems] == [
        ("typescript_syntax_error", "frontend/bad.tsx", "parse file")
    ]
    assert problems[0].message.startswith("TypeScript reported ")


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs POSIX permissions")
def test_find_jsx_usage_reports_unreadable_files_and_keeps_going(typescript_toolchain, tmp_path: Path) -> None:
    tree = _fresh(
        tmp_path,
        typescript_toolchain,
        {"good.tsx": "function Page() { return <Widget />; }\n", "locked.tsx": "function L() { return <Widget />; }\n"},
    )
    locked = tmp_path / "frontend" / "locked.tsx"
    locked.chmod(0)
    try:
        problems: list = []
        usages = tree.find(".", "Widget", problems=problems)
    finally:
        locked.chmod(0o644)
    assert [u.file for u in usages] == ["frontend/good.tsx"]
    assert [(p.kind, p.step, p.file) for p in problems] == [("read_failed", "read file", "frontend/locked.tsx")]
    assert problems[0].message.startswith("frontend/locked.tsx: could not read file (EACCES")
    # Readable again: the failure was not cached.
    assert [u.file for u in tree.find(".", "Widget")] == ["frontend/good.tsx", "frontend/locked.tsx"]


def test_find_jsx_usage_extract_failures_are_diagnostics(tmp_path: Path) -> None:
    """A file the helper could not analyze is reported by name and step; the rest still count."""
    toolchain = ts_bridge.Toolchain("/node", "v24", Path("/ts"), "7.1.0-beta", "env", "7.1.0-beta", None, ())

    def helper(toolchain, mode, request, *, timeout_s):
        files = []
        for path in request["files"]:
            ok = path.endswith("good.tsx")
            files.append(
                {
                    "path": path,
                    "status": "ok" if ok else "extract_failed",
                    "error": None if ok else "RangeError: Maximum call stack size exceeded",
                    "syntax_errors": 0,
                    "first_syntax_error": None,
                    "elements": [
                        {
                            "tag": "Widget",
                            "line": 1,
                            "column": 1,
                            "kind": "self_close",
                            "parent": "Page",
                            "snippet": "x",
                        }
                    ]
                    if ok
                    else [],
                    "decls": [],
                }
            )
        return {"protocol": 1, "mode": "facts", "facts_schema": 1, "files": files}

    _write_tree(tmp_path, {"good.tsx": "x", "bad.tsx": "x"})
    facts = ts_facts.FactsCache(tmp_path, lambda: toolchain, run_helper=helper)
    problems: list = []
    usages = jsx_usage.find_jsx_usage(
        "Widget", Path("frontend"), facts=facts, frontend_root="frontend", repo_root=tmp_path, problems=problems
    )
    assert [u.file for u in usages] == ["frontend/good.tsx"]
    assert [(p.kind, p.step, p.file) for p in problems] == [("analysis_failed", "extract facts", "frontend/bad.tsx")]
    assert "RangeError: Maximum call stack size exceeded" in problems[0].message


def test_find_jsx_usage_sees_jsx_and_js_edits_and_other_roots(typescript_toolchain, tmp_path: Path) -> None:
    """A `.jsx` edit, a `.js` edit and a root outside frontend/ all show on
    the next query; only the edited file is re-read."""
    tree = _fresh(
        tmp_path,
        typescript_toolchain,
        {"a.jsx": "export const A = () => <Old />;\n", "b.tsx": "export const B = () => <Old />;\n"},
    )
    other = tmp_path / "packages" / "ui" / "c.js"
    other.parent.mkdir(parents=True)
    other.write_text("export const C = () => <Old />;\n", encoding="utf-8")
    assert len(tree.find(".", "Old")) == 2
    runs = tree.facts.stats()["helper_runs"]
    assert isinstance(runs, int)
    assert (
        len(
            jsx_usage.find_jsx_usage(
                "Old", Path("packages"), facts=tree.facts, frontend_root="frontend", repo_root=tmp_path, problems=[]
            )
        )
        == 1
    )

    jsx = tmp_path / "frontend" / "a.jsx"
    jsx.write_text("export const A = () => <New />;\n", encoding="utf-8")
    st = jsx.stat()
    os.utime(jsx, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    other.write_text("export const C = () => <New />;\n", encoding="utf-8")
    st = other.stat()
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    assert [u.file for u in tree.find(".", "New")] == ["frontend/a.jsx"]
    assert [u.file for u in tree.find(".", "Old")] == ["frontend/b.tsx"]
    assert [
        u.file
        for u in jsx_usage.find_jsx_usage(
            "New", Path("packages"), facts=tree.facts, frontend_root="frontend", repo_root=tmp_path, problems=[]
        )
    ] == ["packages/ui/c.js"]
    assert tree.facts.stats()["helper_runs"] == runs + 3


def test_find_jsx_usage_raises_when_the_toolchain_is_unavailable(tmp_path: Path) -> None:
    problem = ts_bridge.ToolchainProblem(
        "typescript_not_found",
        "cache/ts/node_modules/typescript/package.json not found",
        "cache/ts/node_modules/typescript/package.json",
        ts_bridge.FRONTEND_TS_FIX,
    )
    toolchain = ts_bridge.Toolchain("/node", "v24", None, None, None, None, problem, ())
    _write_tree(tmp_path, {"a.tsx": "export const A = () => <X />;\n"})
    facts = ts_facts.FactsCache(tmp_path, lambda: toolchain)
    with pytest.raises(jsx_usage.ToolchainUnavailable) as info:
        jsx_usage.find_jsx_usage(
            "X", Path("frontend"), facts=facts, frontend_root="frontend", repo_root=tmp_path, problems=[]
        )
    assert info.value.problem.missing == "cache/ts/node_modules/typescript/package.json"


def _cli(repo: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "code_intel.cli", "dump", "jsx", "--repo-root", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=120,
    )


def _cli_repo(tmp_path: Path, files: dict[str, str], config: str = MINIMAL_CONFIG) -> Path:
    _write_tree(tmp_path, files)
    (tmp_path / ".code-intel").mkdir()
    (tmp_path / ".code-intel" / "config.toml").write_text(config, encoding="utf-8")
    return tmp_path


def test_jsx_usage_cli_all_json(typescript_toolchain, tmp_path: Path) -> None:
    repo = _cli_repo(
        tmp_path,
        {"a.tsx": "export const A = () => <X />;\n", "__tests__/a.test.tsx": "const T = () => <X />;\n"},
    )
    env = {**os.environ, ts_bridge.TS_DIR_ENV: str(typescript_toolchain.typescript_dir)}
    done = _cli(repo, "--all", "--json", env=env)
    assert done.returncode == 0, done.stderr
    body = json.loads(done.stdout)
    assert body["schema_version"] == 2 and body["complete"] is True
    assert [(e["file"], e["tag"], e["parent"], e["is_test"]) for e in body["elements"]] == [
        ("frontend/a.tsx", "X", "A", False)
    ]
    with_tests = json.loads(_cli(repo, "--all", "--json", "--include-tests", env=env).stdout)
    assert [(e["file"], e["is_test"]) for e in with_tests["elements"]] == [
        ("frontend/__tests__/a.test.tsx", True),
        ("frontend/a.tsx", False),
    ]
    usages = json.loads(_cli(repo, "X", "--json", env=env).stdout)["usages"]
    assert [(u["file"], u["line"], u["parent_function"]) for u in usages] == [("frontend/a.tsx", 1, "A")]


def test_jsx_usage_cli_scans_the_configured_frontend_root(typescript_toolchain, tmp_path: Path) -> None:
    config = MINIMAL_CONFIG.replace('root = "frontend"', 'root = "web/app"').replace("frontend/", "web/app/")
    for rel in ("web/app/src/a.tsx", "web/app/coverage/report.tsx", "frontend/b.tsx"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export const A = () => <X />;\n", encoding="utf-8")
    (tmp_path / ".code-intel").mkdir()
    (tmp_path / ".code-intel" / "config.toml").write_text(config, encoding="utf-8")
    env = {**os.environ, ts_bridge.TS_DIR_ENV: str(typescript_toolchain.typescript_dir)}
    body = json.loads(_cli(tmp_path, "--all", "--json", env=env).stdout)
    assert [e["file"] for e in body["elements"]] == ["web/app/src/a.tsx"]


def test_jsx_usage_cli_without_a_toolchain_says_how_to_set_it_up(tmp_path: Path) -> None:
    repo = _cli_repo(tmp_path, {"a.tsx": "export const A = () => <X />;\n"})
    env = {**os.environ, ts_bridge.CACHE_ENV: str(tmp_path / "empty-cache")}
    env.pop(ts_bridge.TS_DIR_ENV, None)
    done = _cli(repo, "--all", "--json", env=env)
    assert done.returncode == 2
    body = json.loads(done.stdout)
    assert body["complete"] is False and body["elements"] == []
    [found] = body["diagnostics"]
    assert (found["kind"], found["problem"]) == ("frontend_unavailable", "typescript_not_found")
    assert 'run "code-intel setup"' in found["fix"]
    assert "typescript_not_found" not in done.stderr
    plain = _cli(repo, "--all", env=env)
    assert plain.returncode == 2 and plain.stdout == ""
    assert 'Fix: run "code-intel setup"' in plain.stderr


def test_jsx_usage_cli_reports_a_missing_frontend_root(tmp_path: Path) -> None:
    repo = _cli_repo(tmp_path, {})
    done = _cli(repo, "--all", "--json", env=dict(os.environ))
    assert done.returncode == 2
    [found] = json.loads(done.stdout)["diagnostics"]
    assert (found["kind"], found["problem"], found["missing"]) == (
        "frontend_unavailable",
        "frontend_not_found",
        "frontend",
    )
    assert "frontend.root" in found["fix"]


def test_generated_output_directly_under_frontend_is_skipped(typescript_toolchain, tmp_path: Path) -> None:
    tree = _fresh(
        tmp_path,
        typescript_toolchain,
        {
            "app/build/page.tsx": "export const P = () => <Widget />;\n",
            "coverage/lcov-report/x.tsx": "export const C = () => <Widget />;\n",
            "out/x.tsx": "export const O = () => <Widget />;\n",
        },
    )
    assert [u.file for u in tree.find(".", "Widget")] == ["frontend/app/build/page.tsx"]
