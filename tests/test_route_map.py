"""Frontend route analysis through the TypeScript helper, and routes JSON v2.

Each fixture is a ``{frontend-relative path: source}`` tree written under
``tmp_path/frontend``. Trees are batched (one helper process per tree) and
each behaviour is a named case. Backend routes are synthetic
``LiveRoute`` lists.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from fixture_repo import MINIMAL_CONFIG, git_init, make_repo
from test_dependency_resolver import FakeResolver, FakeSources

from code_intel import cli, include_model, route_map, ts_bridge
from code_intel.backend_model import BackendModel, CapturedEndpoint, Library, LiveRoute, Located, RouteKind
from code_intel.code_paths import TS_SUFFIXES, iter_source_files
from code_intel.config import FrontendConfig, parse_config
from code_intel.problems import Problem, diagnostic, limitation

pytestmark = pytest.mark.usefixtures("module_git")

API_CLIENTS_CONFIG = """
[[frontend.clients]]
kind = "call"
module = "frontend/lib/api.ts"
export = "apiFetch"

[[frontend.clients]]
kind = "call"
module = "frontend/lib/api.ts"
export = "apiJson"

[[frontend.clients]]
kind = "call"
module = "frontend/lib/api.ts"
export = "apiJsonOr204"
"""
CLIENTS_CONFIG = (
    API_CLIENTS_CONFIG
    + """
[[frontend.clients]]
kind = "options"
module = "frontend/lib/utils/download-export.ts"
export = "downloadExport"
"""
)
CONFIG = parse_config(MINIMAL_CONFIG + CLIENTS_CONFIG)
FRONTEND = CONFIG.frontend

TSCONFIG = json.dumps(
    {
        "compilerOptions": {
            "jsx": "react-jsx",
            "module": "esnext",
            "moduleResolution": "bundler",
            "allowJs": True,
            "paths": {"@/*": ["./*"]},
        }
    }
)
CLIENTS_TS = """\
export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(path, init);
}
async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, init);
  return (await res.json()) as T;
}
async function apiJsonOr204<T>(path: string, init?: RequestInit): Promise<T | null> {
  const res = await apiFetch(path, init);
  return res.status === 204 ? null : ((await res.json()) as T);
}
type X = { id: string };
"""
DOWNLOAD_TS = """\
import { apiFetch } from "@/lib/api";
export interface DownloadExportOptions { url: string; fallbackFilename?: string }
export async function downloadExport(options: DownloadExportOptions): Promise<void> {
  const { url } = options;
  const suffix = "";
  await apiFetch(`${url}${suffix}`);
}
"""


def base_tree(api_body: str = "", **files: str | bytes) -> dict[str, str | bytes]:
    tree: dict[str, str | bytes] = {
        "tsconfig.json": TSCONFIG,
        "lib/api.ts": CLIENTS_TS + api_body,
        "lib/utils/download-export.ts": DOWNLOAD_TS,
    }
    tree.update(files)
    return tree


# ── Running the helper ───────────────────────────────────────────────────


class Scan:
    """One helper run over a fixture tree, with lookups by file and line."""

    def __init__(self, repo: Path, result: route_map.FrontendScan | route_map.FrontendUnavailable) -> None:
        self.repo = repo
        self.result = result
        assert isinstance(result, route_map.FrontendScan), result
        self.calls = result.calls
        self.problems = result.problems

    def at(self, rel: str, line: int) -> list[dict]:
        return [c for c in self.calls if c["file"] == f"frontend/{rel}" and c["line"] == line]

    def one(self, rel: str, line: int) -> dict:
        found = self.at(rel, line)
        assert len(found) == 1, (rel, line, found, [(c["file"], c["line"]) for c in self.calls])
        return found[0]

    def paths(self, rel: str, line: int) -> list[str]:
        return [render(u) for c in self.at(rel, line) for u in c["urls"]]

    def kinds(self, kind: str) -> list[Problem]:
        return [p for p in self.problems if p.kind == kind]


def render(variant: dict) -> str:
    return "".join(p["text"] if "text" in p else "{" + p["hole"] + ":" + p["expr"] + "}" for p in variant["parts"])


def write_tree(repo: Path, tree: Mapping[str, str | bytes]) -> None:
    git_init(repo)
    for rel, source in tree.items():
        path = repo / "frontend" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, bytes):
            path.write_bytes(source)
        else:
            path.write_text(source, encoding="utf-8", newline="")


@pytest.fixture(scope="module")
def scan_tree(
    typescript_toolchain: ts_bridge.Toolchain, tmp_path_factory: pytest.TempPathFactory
) -> Callable[..., Scan]:
    memo: dict[tuple, Scan] = {}

    def run(tree: Mapping[str, str], frontend: FrontendConfig = FRONTEND) -> Scan:
        key = (frontend, *sorted(tree.items()))
        if key not in memo:
            repo = tmp_path_factory.mktemp("routes")
            write_tree(repo, tree)
            memo[key] = Scan(repo, route_map.scan_frontend(repo, frontend, typescript_toolchain))
        return memo[key]

    return run


# ── HTTP method ───────────────────────────────────────────────────────────

METHODS_API = """
const PATCH_INIT = { method: "PATCH" };
export async function archiveThenGet(id: string) {
  await apiFetch(`/squads/${id}/archive`, {
    method: "POST",
  });
  return apiJson<X>(`/squads/${id}`);
}
export async function shorthandMethod(id: string, method: "PUT" | "PATCH") {
  return apiJson<X>(`/squads/${id}`, { method, body: "{}" });
}
export async function quotedKey(id: string) {
  return apiJson<void>(`/squads/${id}`, { "method": "DELETE" });
}
export async function initVariable(id: string, body: unknown) {
  const init: RequestInit = {
    method: "PATCH",
    body: JSON.stringify(body),
  };
  return apiJson<X>(`/squads/${id}`, init);
}
export async function computedKey(id: string) {
  return apiJson<X>(`/squads/${id}`, { ["method"]: "put" });
}
export async function spreadThenMethod(id: string, base: RequestInit) {
  return apiJson<X>(`/squads/${id}`, { ...base, method: "PUT" });
}
export async function methodThenUnknownSpread(id: string, extra: RequestInit) {
  return apiJson<X>(`/squads/${id}`, { method: "POST", ...extra });
}
export async function methodThenBodyOnlySpread(id: string, reason?: string) {
  return apiJson<X>(`/squads/${id}/cancel`, {
    method: "POST",
    ...(reason == null ? {} : { body: JSON.stringify({ reason }) }),
  });
}
export async function moduleConstInit(id: string) {
  return apiJson<X>(`/squads/${id}`, PATCH_INIT);
}
export async function undefinedInit(id: string) {
  return apiFetch(`/squads/${id}/content`, undefined);
}
export async function uploadUserFile(form: FormData) {
  // apiFetch only auto-sets application/json when the body is a string.
  const res = await apiFetch("/user-files", { method: "POST", body: form });
  return res.json();
}
/* apiJson("/commented-out") is not a call */
"""


def _api_line(snippet: str, body: str = METHODS_API) -> int:
    lines = (CLIENTS_TS + body).split("\n")
    return next(i + 1 for i, line in enumerate(lines) if snippet in line)


@pytest.fixture(scope="module")
def methods(scan_tree) -> Scan:
    return scan_tree(base_tree(METHODS_API))


def _method(scan: Scan, snippet: str) -> tuple[str, str]:
    call = scan.one("lib/api.ts", _api_line(snippet))
    return call["method"], call["method_source"]


def test_method_is_not_carried_over_from_an_earlier_post(methods: Scan) -> None:
    assert _method(methods, "apiFetch(`/squads/${id}/archive`") == ("POST", "init")
    assert _method(methods, "return apiJson<X>(`/squads/${id}`);") == ("GET", "default")


def test_method_never_comes_from_a_type_annotation(methods: Scan) -> None:
    assert _method(methods, "{ method, body") == ("UNKNOWN", "unresolved")
    lims = methods.kinds("unresolved_http_method")
    assert [(lim.file, lim.line) for lim in lims if lim.line == _api_line("{ method, body")] == [
        ("frontend/lib/api.ts", _api_line("{ method, body"))
    ]


def test_quoted_and_computed_method_keys(methods: Scan) -> None:
    assert _method(methods, '{ "method": "DELETE" }') == ("DELETE", "init")
    assert _method(methods, '{ ["method"]: "put" }') == ("PUT", "init")


def test_method_from_an_init_object_defined_earlier(methods: Scan) -> None:
    assert _method(methods, "return apiJson<X>(`/squads/${id}`, init);") == ("PATCH", "init")
    assert _method(methods, "PATCH_INIT);") == ("PATCH", "init")


def test_method_with_spreads_in_the_init_object(methods: Scan) -> None:
    assert _method(methods, "{ ...base, method") == ("PUT", "init")
    assert _method(methods, '{ method: "POST", ...extra }') == ("UNKNOWN", "unresolved")
    assert _method(methods, "apiJson<X>(`/squads/${id}/cancel`") == ("POST", "init")


def test_undefined_init_is_the_default_method(methods: Scan) -> None:
    assert _method(methods, "/content`, undefined") == ("GET", "default")


def test_comments_naming_a_client_do_not_move_the_call(methods: Scan) -> None:
    line = _api_line('apiFetch("/user-files"')
    call = methods.one("lib/api.ts", line)
    assert (call["method"], call["function"], methods.paths("lib/api.ts", line)) == (
        "POST",
        "uploadUserFile",
        ["/user-files"],
    )
    assert not [c for c in methods.calls if "commented-out" in json.dumps(c)]


# ── Client calls, scopes and the evaluator ────────────────────────────────

CALLS_API = """
import { AUDIT_BASE, Paths } from "./paths";
import * as P from "./paths";
const SEGMENT = "changes";
const MULTI =
  "/squads/options";
const TYPED = "/typed" satisfies string;
function buildArticlePath(id: string) {
  return `/articles/${id}`;
}
const PATHS = { me: "/me", nested: { deep: "/deep" } } as const;
export async function nestedTemplate(q?: string) {
  return apiJson<X[]>(`/squads${q ? `?${q}` : ""}`);
}
export async function builderArg(id: string) {
  return apiJson<X>(buildArticlePath(id));
}
export async function conditionalArg(mine: boolean) {
  return apiJson<X[]>(mine ? "/squads/mine" : "/squads");
}
export async function memberArg() {
  return apiJson<X>(PATHS.me);
}
export async function nestedMember() {
  return apiJson<X>(PATHS.nested.deep);
}
export async function importedAsConst() {
  return apiJson<X>(`${AUDIT_BASE}/investigation/changes`);
}
export async function namespaceConst() {
  return apiJson<X>(P.AUDIT_BASE + "/x");
}
export async function enumMember() {
  return apiJson<X>(Paths.Health);
}
export async function segmentConst() {
  return apiJson<X>(`/admin/audit/investigation/${SEGMENT}`);
}
export async function multiLineConst() {
  return apiJson<X>(MULTI);
}
export async function satisfiesConst() {
  return apiJson<X>(TYPED);
}
export async function multiLineGeneric() {
  return apiJson<
    Record<string, (string | null)[]>
  >("/subsquads");
}
export const arrowExport = async () => apiJson<X>("/onboarding/status");
const aliasJson = apiJson;
export async function viaAlias() {
  return aliasJson<X>("/admin/audit-logs");
}
export async function optionalCall() {
  return apiFetch?.("/health");
}
export async function nullable() {
  return apiJsonOr204<X>("/maybe");
}
export const arrowLocal = async (id: string) => {
  const url =
    `/subsquads/${id}`;
  return apiJson<X>(url);
};
class Client {
  async ping() {
    return apiJson<X>("/healthz/live");
  }
}
export async function uploadDefined(
  file: File,
): Promise<void> {
  const form = new FormData();
  form.append("file", file);
  await apiFetch("/user-files", { method: "POST", body: form });
}
export function pair() {
  return [apiJson("/first"), apiJson("/second")];
}
export function pairWithUnknown() {
  return [apiJson("/known"), apiJson(MISSING_PATH)];
}
export async function notAPath() {
  return apiJson<X>("relative/path");
}
export async function unknownIdentifier() {
  return apiJson<X>(mysteryVar);
}
const objectWrapper = { get: <T,>(p: string) => apiJson<T>(p) };
export async function viaObjectWrapper() {
  return objectWrapper.get<X>("/appointments/inbox");
}
export const handlers = { load: apiJson };
const HELP = "call apiFetch('/fake') to load";
const TEMPLATE_TEXT = `apiJson("/fake2") or apiJson(FAKE_PATH)`;
const PATTERN = /apiJsonOr204\\("\\/fake3"\\)/;
function buildQuery(params: Record<string, string>): string {
  const s = new URLSearchParams(params).toString();
  return s ? `?${s}` : "";
}
export async function querySuffix(params: Record<string, string>) {
  return apiJson<X>(`/admin/changes${buildQuery(params)}`);
}
const LOOP_A: string = LOOP_B;
const LOOP_B: string = LOOP_A;
export async function constCycle() {
  return apiJson<X>(`/loop/${LOOP_A}`);
}
export async function manyVariants(a: boolean, b: boolean, c: boolean, d: boolean, e: boolean) {
  return apiJson<X>(`/v${a ? "1" : "2"}${b ? "1" : "2"}${c ? "1" : "2"}${d ? "1" : "2"}${e ? "1" : "2"}`);
}
"""
PATHS_TS = """\
export const AUDIT_BASE = "/admin/audit" as const;
export enum Paths { Health = "/healthz/ready" }
"""
SHADOW_TS = """\
function apiJson(path: string) { return path; }
export function localShadow() { return apiJson("/not-a-client"); }
"""


@pytest.fixture(scope="module")
def calls(scan_tree) -> Scan:
    return scan_tree(base_tree(CALLS_API, **{"lib/paths.ts": PATHS_TS, "app/shadow.ts": SHADOW_TS}))


def _calls_line(snippet: str) -> int:
    return _api_line(snippet, CALLS_API)


def _paths(scan: Scan, snippet: str) -> list[str]:
    return scan.paths("lib/api.ts", _calls_line(snippet))


def test_arrow_exports_and_class_methods_get_their_own_names(calls: Scan) -> None:
    arrow = calls.one("lib/api.ts", _calls_line("export const arrowExport"))
    assert (arrow["function"], arrow["def_line"]) == ("arrowExport", _calls_line("export const arrowExport"))
    local = calls.one("lib/api.ts", _calls_line("return apiJson<X>(url);"))
    assert local["function"] == "arrowLocal"
    assert _paths(calls, "return apiJson<X>(url);") == ["/subsquads/{param:id}"]
    ping = calls.one("lib/api.ts", _calls_line('"/healthz/live"'))
    assert ping["function"] == "ping"


def test_def_line_is_separate_from_the_call_line(calls: Scan) -> None:
    call = calls.one("lib/api.ts", _calls_line('await apiFetch("/user-files"'))
    assert (call["function"], call["def_line"], call["line"]) == (
        "uploadDefined",
        _calls_line("export async function uploadDefined("),
        _calls_line('await apiFetch("/user-files"'),
    )


def test_a_local_function_with_a_client_name_is_not_a_client(calls: Scan) -> None:
    assert calls.at("app/shadow.ts", 2) == []
    assert not [c for c in calls.calls if "not-a-client" in c["raw"]]


def test_nested_templates_are_evaluated_not_truncated(calls: Scan) -> None:
    assert _paths(calls, "nestedTemplate(q") == []  # the definition line holds no call
    assert _paths(calls, "`/squads${q ?") == ["/squads?{query:q}", "/squads"]


def test_urls_that_are_not_paths_are_diagnostics(calls: Scan) -> None:
    unresolved = calls.kinds("unresolved_frontend_path")
    lines = {p.line for p in unresolved}
    assert _calls_line('"relative/path"') in lines
    assert _calls_line("apiJson<X>(mysteryVar)") in lines
    assert any("mysteryVar" in p.message for p in unresolved)
    assert calls.at("lib/api.ts", _calls_line('"relative/path"')) == []
    assert all(p.is_diagnostic for p in unresolved)


def test_path_constants_of_every_shape(calls: Scan) -> None:
    assert _paths(calls, "/investigation/${SEGMENT}") == ["/admin/audit/investigation/changes"]
    assert _paths(calls, "${AUDIT_BASE}/investigation") == ["/admin/audit/investigation/changes"]
    assert _paths(calls, "P.AUDIT_BASE") == ["/admin/audit/x"]
    assert _paths(calls, "Paths.Health") == ["/healthz/ready"]
    assert _paths(calls, "apiJson<X>(MULTI)") == ["/squads/options"]
    assert _paths(calls, "apiJson<X>(TYPED)") == ["/typed"]
    origin = calls.one("lib/api.ts", _calls_line("apiJson<X>(MULTI)"))["urls"][0]["origin"]
    assert origin == "const"


def test_query_suffix_from_a_helper_is_stripped_not_a_hole(calls: Scan) -> None:
    assert _paths(calls, "/admin/changes${buildQuery") == [
        "/admin/changes?{query:new URLSearchParams(params).toString()}",
        "/admin/changes",
    ]


def test_evaluator_cycles_become_holes_and_variants_are_capped(calls: Scan) -> None:
    assert _paths(calls, "`/loop/${LOOP_A}`") == ["/loop/{param:LOOP_A}"]
    many = _paths(calls, "`/v${a ?")
    assert len(many) == route_map.MAX_URL_VARIANTS and many[0] == "/v11111"
    lims = [p for p in calls.kinds("url_variants_truncated") if p.line == _calls_line("`/v${a ?")]
    assert len(lims) == 1 and not lims[0].is_diagnostic


def test_calls_through_helpers_conditionals_generics_optional_chaining_and_aliases(calls: Scan) -> None:
    assert _paths(calls, "buildArticlePath(id))") == ["/articles/{param:id}"]
    assert _paths(calls, 'mine ? "/squads/mine"') == ["/squads/mine", "/squads"]
    assert _paths(calls, "PATHS.me") == ["/me"]
    assert _paths(calls, "PATHS.nested.deep") == ["/deep"]
    generic_call = _calls_line('>("/subsquads")') - 2  # the call starts at `apiJson<`, two lines up
    assert calls.paths("lib/api.ts", generic_call) == ["/subsquads"]
    assert _paths(calls, 'apiFetch?.("/health")') == ["/health"]
    assert _paths(calls, 'aliasJson<X>("/admin/audit-logs")') == ["/admin/audit-logs"]
    assert calls.one("lib/api.ts", _calls_line('aliasJson<X>("/admin')).get("client") == "apiJson"
    assert _paths(calls, 'apiJsonOr204<X>("/maybe")') == ["/maybe"]


def test_client_definitions_and_pass_throughs_are_not_consumers(calls: Scan) -> None:
    clients_end = CLIENTS_TS.count("\n")
    assert [c for c in calls.calls if c["file"] == "frontend/lib/api.ts" and c["line"] <= clients_end] == []
    assert calls.at("lib/utils/download-export.ts", 6) == []


def test_unreadable_client_uses_are_diagnostics(calls: Scan) -> None:
    unparsed = calls.kinds("unparsed_frontend_callsite")
    assert [(p.line, "client_reference_not_called" in p.message) for p in unparsed] == [
        (_calls_line("export const handlers"), True)
    ]
    wrapper = [p for p in calls.kinds("unresolved_frontend_path") if p.line == _calls_line("objectWrapper = {")]
    assert len(wrapper) == 1 and "apiJson<T>(p)" in wrapper[0].message
    assert all(p.file == "frontend/lib/api.ts" for p in unparsed + wrapper)


def test_two_calls_on_one_line_are_both_read(calls: Scan) -> None:
    assert _paths(calls, '[apiJson("/first")') == ["/first", "/second"]
    line = _calls_line('[apiJson("/known")')
    assert _paths(calls, '[apiJson("/known")') == ["/known"]
    assert [p for p in calls.kinds("unresolved_frontend_path") if p.line == line and "MISSING_PATH" in p.message]


def test_client_names_in_strings_templates_and_regexes_are_not_calls(calls: Scan) -> None:
    raws = " ".join(c["raw"] for c in calls.calls)
    assert "/fake" not in raws and "FAKE_PATH" not in raws
    assert not [p for p in calls.problems if "fake" in p.message.lower()]


# ── downloadExport, builders, prop flows, WebSockets ──────────────────────

BUILDERS_API = """
const USAGE_PATH = "/analytics/articles";
export function squadExportUrl(id: string) {
  return `/squads/${id}/export`;
}
export function squadExportWithQuery(id: string, q: string) {
  return `${squadExportUrl(id)}?${q}`;
}
export const appointmentsExportUrl = function (q: string) {
  return `/appointments/export?${q}`;
};
export const arrowBuilder = (id: string) => `/arrow/${id}`;
export function genericBuilder<T>(id: T) {
  return `/generic/${id}`;
}
export function literalTypes(id: string): `/t/${string}` | '{' | 'a;b' {
  return `/t/${id}`;
}
export function overloaded(a: string): string;
export function overloaded(a: string) {
  return `/over/${a}`;
}
export function nestedReturns(id: string) {
  const inner = () => {
    return "/inner";
  };
  return `/outer/${id}`;
}
export function articleUsageExportUrl(fields: string[]): string {
  const qs = new URLSearchParams();
  return `${USAGE_PATH}/export?${qs.toString()}`;
}
export function maybeUrl(flag: boolean): string {
  if (!flag) return "";
  return "/never";
}
function localBuilder(id: string) {
  return `/subsquads/${id}/export`;
}
export { localBuilder as subsquadExportUrl };
export function a1() { return "/a1/export"; } export function a2() { return "/a2/export"; }
export async function usesLocalBuilder(id: string) {
  return apiFetch(localBuilder(id));
}
"""
BUTTONS_TSX = """\
import { downloadExport as dl, downloadExport } from "@/lib/utils/download-export";
import * as api from "@/lib/api";
import { squadExportUrl as fx, squadExportWithQuery, appointmentsExportUrl, subsquadExportUrl, a1, a2, genericBuilder, overloaded, nestedReturns, arrowBuilder, maybeUrl, literalTypes } from "@/lib/api";
import { auditChangesExportUrl } from "@/lib/audit/urls";
const SEGMENT = "changes";
const KEYS: Record<string, string> = { k: "v" };
export function Buttons({ id, mine, q }: { id: string; mine: boolean; q: string }) {
  const url = "/articles/export";
  const onA = () => downloadExport({ fallbackFilename: "a.csv", url: "/squads/export" });
  const onB = () => downloadExport({ url, fallbackFilename: "b.csv" });
  const onC = () => downloadExport({ url: mine ? "/squads/mine/export" : "/squads/export" });
  const onD = () => dl({ url: "/subsquads/export" });
  const onE = () => downloadExport({ url: fx(id) });
  const onF = () => downloadExport({ url: api.squadExportUrl(id) });
  const onG = () => downloadExport({ url: squadExportWithQuery(id, q) });
  const onH = () => downloadExport({ url: appointmentsExportUrl(q) });
  const onI = () => downloadExport({ url: subsquadExportUrl(id) });
  const onJ = () => downloadExport({ url: auditChangesExportUrl(q) });
  const onK = () => downloadExport({ url: `/admin/audit/investigation/${SEGMENT}/export` });
  const opts = { url: "/articles/export" };
  const onL = () => downloadExport(opts);
  const onM = () => downloadExport({ url: `/quoted/${KEYS["k"]}/export` });
  const onN = () => downloadExport({ url: genericBuilder<string>(id) });
  const pair = [a1(), a2()];
  const more = [overloaded(id), nestedReturns(id), arrowBuilder(id), maybeUrl(mine), literalTypes(id)];
  return <a href={fx(id)} onClick={onA}>x</a>;
}
function squadExportUrl(id: string) {
  return `/not-api/${id}`;
}
export function Local({ id }: { id: string }) {
  return <a href={squadExportUrl(id)}>local shadow</a>;
}
"""
URLS_TS = """\
export function auditChangesExportUrl(q: string) {
  return `/admin/audit/investigation/changes/export?${q}`;
}
"""
HOOKS_TSX = """\
import { downloadExport } from "@/lib/utils/download-export";
export const useArrowExport = ({ exportPath }: { exportPath: string }) => {
  return () => downloadExport({ url: `/admin/audit/investigation/${exportPath}/export` });
};
export function useConfigExport(config: { exportPath: string }) {
  return () => downloadExport({ url: `/admin/audit/investigation/${config.exportPath}/export` });
}
export function ExportButton({ exportPath }: { exportPath: string }) {
  const go = () => downloadExport({ url: `/admin/audit/investigation/${exportPath}/export` });
  return <button onClick={go}>x</button>;
}
export function useTab({ exportPath }: { exportPath: string }) {
  const suffix = build();
  return () => downloadExport({ url: `/admin/tab/${exportPath}/export${suffix}` });
}
export function plainHelper(segment: string) {
  return () => downloadExport({ url: `/plain/${segment}/export` });
}
const DYNAMIC = pick();
export function Page() {
  useArrowExport({ exportPath: "changes" });
  useConfigExport({ exportPath: "admin-actions" });
  useTab({ exportPath: DYNAMIC });
  useTab({ exportPath: "changes" });
  useTab({ columns: { exportPath: "bogus" }, exportPath: "changes" });
  plainHelper("literal");
  const doc = 'useTab({ exportPath: "invented" })';
  return <ExportButton exportPath="changes" />;
}
export function Prose() {
  return <p>useTab({"{"} exportPath: "prose" {"}"})</p>;
}
"""
WS_TSX = """\
const WS_PATH = "/ws/squads";
// connects to "/ws/commented/heartbeat"
export function sockets(id: string, base: string) {
  const a = new WebSocket(base + "/ws/" + `squads/${id}/heartbeat`);
  const b = new WebSocket(
    `${base}/ws/articles/${
      id
    }/heartbeat`,
  );
  const c = new WebSocket(`${base}${WS_PATH}/${id}`);
  /* const d = new WebSocket("/ws/block-comment"); */
  return [a, b, c];
}
"""
LEGACY_JSX = """\
import { downloadExport } from "@/lib/utils/download-export";
export function Legacy() {
  return <button onClick={() => downloadExport({ url: "/legacy/export" })} />;
}
"""
TEST_FILES = {
    "test/mocks/handlers.ts": (
        'import { squadExportUrl } from "@/lib/api";\n'
        'export const exportMock = () => squadExportUrl("fixture-id");\n'
        'export const ws = () => new WebSocket("/ws/squads/fixture/heartbeat");\n'
    ),
    "app/x/hooks.test.tsx": 'import { useTab } from "./hooks";\nuseTab({ exportPath: "from-a-test" });\n',
    "app/x/__tests__/calls.ts": 'import { apiFetch } from "@/lib/api";\napiFetch(MOCKED);\nconst m = { apiFetch };\n',
}


@pytest.fixture(scope="module")
def frontend(scan_tree) -> Scan:
    return scan_tree(
        base_tree(
            BUILDERS_API,
            **{
                "app/x/Buttons.tsx": BUTTONS_TSX,
                "lib/audit/urls.ts": URLS_TS,
                "app/x/hooks.tsx": HOOKS_TSX,
                "app/x/ws.tsx": WS_TSX,
                "app/legacy/Legacy.jsx": LEGACY_JSX,
                **TEST_FILES,
            },
        )
    )


def _buttons_line(snippet: str) -> int:
    return next(i + 1 for i, line in enumerate(BUTTONS_TSX.split("\n")) if snippet in line)


def _button(scan: Scan, snippet: str) -> list[str]:
    return scan.paths("app/x/Buttons.tsx", _buttons_line(snippet))


def test_download_export_option_shapes(frontend: Scan) -> None:
    assert _button(frontend, "const onA") == ["/squads/export"]
    assert _button(frontend, "const onB") == ["/articles/export"]
    assert _button(frontend, "const onC") == ["/squads/mine/export", "/squads/export"]
    assert _button(frontend, "const onD") == ["/subsquads/export"]
    assert _button(frontend, "const onK") == ["/admin/audit/investigation/changes/export"]
    assert _button(frontend, "const onL") == ["/articles/export"]
    assert _button(frontend, "const onM") == ['/quoted/{param:KEYS["k"]}/export']
    kinds = {c["kind"] for c in frontend.at("app/x/Buttons.tsx", _buttons_line("const onD"))}
    assert kinds == {"options"}


def test_builders_through_aliases_namespaces_reexports_and_other_builders(frontend: Scan) -> None:
    def via(snippet: str) -> list[list[str]]:
        return [u["via"] for c in frontend.at("app/x/Buttons.tsx", _buttons_line(snippet)) for u in c["urls"]]

    assert _button(frontend, "const onE") == ["/squads/{param:id}/export"]
    assert via("const onE") == [["squadExportUrl"]]
    assert _button(frontend, "const onF") == ["/squads/{param:id}/export"]
    assert _button(frontend, "const onG") == ["/squads/{param:id}/export?{query:q}"]
    assert via("const onG") == [["squadExportWithQuery", "squadExportUrl"]]
    assert _button(frontend, "const onH") == ["/appointments/export?{query:q}"]
    assert _button(frontend, "const onI") == ["/subsquads/{param:id}/export"]
    assert via("const onI") == [["subsquadExportUrl"]]
    assert _button(frontend, "const onJ") == ["/admin/audit/investigation/changes/export?{query:q}"]
    assert _button(frontend, "const onN") == ["/generic/{param:id}"]
    assert frontend.paths("lib/api.ts", _api_line("apiFetch(localBuilder(id))", BUILDERS_API)) == [
        "/subsquads/{param:id}/export"
    ]


def test_builder_calls_outside_a_client_url_are_consumers(frontend: Scan) -> None:
    pair = frontend.at("app/x/Buttons.tsx", _buttons_line("const pair"))
    assert sorted((c["builder"], render(c["urls"][0]), c["kind"]) for c in pair) == [
        ("a1", "/a1/export", "builder"),
        ("a2", "/a2/export", "builder"),
    ]
    more = frontend.at("app/x/Buttons.tsx", _buttons_line("const more"))
    assert sorted(c["builder"] for c in more) == ["arrowBuilder", "literalTypes", "nestedReturns", "overloaded"]
    assert {render(c["urls"][0]) for c in more} == {
        "/over/{param:id}",
        "/outer/{param:id}",
        "/arrow/{param:id}",
        "/t/{param:id}",
    }
    href = frontend.at("app/x/Buttons.tsx", _buttons_line("<a href={fx(id)}"))
    assert [(c["builder"], c["function"]) for c in href] == [("squadExportUrl", "Buttons")]


def test_non_builders_and_shadowing_locals_are_not_consumers(frontend: Scan) -> None:
    assert frontend.at("app/x/Buttons.tsx", _buttons_line("local shadow")) == []
    assert not [c for c in frontend.calls if c.get("builder") in ("maybeUrl", "articleUsageExportUrl")]
    builders_inside_builders = [
        c for c in frontend.calls if c["file"] == "frontend/lib/api.ts" and c["kind"] == "builder"
    ]
    assert builders_inside_builders == []


def _hooks_line(snippet: str) -> int:
    return next(i + 1 for i, line in enumerate(HOOKS_TSX.split("\n")) if snippet in line)


def _hook(scan: Scan, snippet: str) -> list[tuple[str, str, str]]:
    return [
        (render(u), u["confidence"], u["origin"])
        for c in scan.at("app/x/hooks.tsx", _hooks_line(snippet))
        for u in c["urls"]
    ]


def test_prop_literals_flow_from_call_and_jsx_sites_as_possible(frontend: Scan) -> None:
    assert _hook(frontend, "useArrowExport = ") == []
    assert _hook(frontend, "  return () => downloadExport({ url: `/admin/audit/investigation/${exportPath}") == [
        ("/admin/audit/investigation/changes/export", "possible", "prop_flow")
    ]
    assert _hook(frontend, "${config.exportPath}") == [
        ("/admin/audit/investigation/admin-actions/export", "possible", "prop_flow")
    ]
    assert _hook(frontend, "const go = ") == [("/admin/audit/investigation/changes/export", "possible", "prop_flow")]


def test_non_literal_prop_site_is_a_limitation_and_keeps_the_hole(frontend: Scan) -> None:
    assert _hook(frontend, "/admin/tab/") == [
        ("/admin/tab/changes/export{unknown:build()}", "possible", "prop_flow"),
        ("/admin/tab/{param:exportPath}/export{unknown:build()}", "possible", "prop_flow"),
    ]
    lims = frontend.kinds("unresolved_prop_value")
    assert [(lim.file, lim.line) for lim in lims] == [("frontend/app/x/hooks.tsx", _hooks_line("DYNAMIC });"))]
    assert "exportPath" in lims[0].message and "useTab" in lims[0].message


def test_prop_flow_ignores_nested_keys_strings_jsx_text_and_test_sites(frontend: Scan) -> None:
    texts = {render(u) for c in frontend.at("app/x/hooks.tsx", _hooks_line("/admin/tab/")) for u in c["urls"]}
    assert not any(word in t for t in texts for word in ("bogus", "invented", "prose", "from-a-test"))


def test_only_components_and_hooks_take_prop_flows(frontend: Scan) -> None:
    assert _hook(frontend, "/plain/") == [("/plain/{param:segment}/export", "exact", "literal")]


def test_websocket_strings_in_code_with_concatenation_and_multiline_templates(frontend: Scan) -> None:
    ws = [c for c in frontend.calls if c["kind"] == "websocket" and not c["is_test"]]
    got = sorted((c["line"], render(c["urls"][0]), c["method"]) for c in ws)
    assert got == [
        (4, "/ws/squads/{param:id}/heartbeat", "WS"),
        (6, "/ws/articles/{param:id}/heartbeat", "WS"),
        (10, "/ws/squads/{param:id}", "WS"),
    ]
    assert {c["function"] for c in ws} == {"sockets"}


def test_test_files_use_the_shared_rule_and_jsx_is_scanned(frontend: Scan) -> None:
    tests = [c for c in frontend.calls if c["is_test"]]
    assert {(c["file"], c["kind"]) for c in tests} == {
        ("frontend/test/mocks/handlers.ts", "builder"),
        ("frontend/test/mocks/handlers.ts", "websocket"),
    }
    assert not [p for p in frontend.problems if "__tests__" in (p.file or "")]
    assert frontend.paths("app/legacy/Legacy.jsx", 3) == ["/legacy/export"]


# ── Client resolution, init keys, prop flows and WebSocket text ──────────────

# The clients exported, so other modules import, re-export and destructure them.
EXPORTED_CLIENTS = "export { apiJson, apiJsonOr204 };\n"
RESOLUTION_FILES = {
    "lib/barrel.ts": 'export { apiJson as default } from "@/lib/api";\n',
    "lib/barrel2.ts": 'import { apiJson } from "@/lib/api";\nexport default apiJson;\n',
    "app/default_reexport.ts": 'import j from "@/lib/barrel";\nexport async function loadA() { return j("/a/items"); }\n',
    "app/default_expr.ts": 'import k from "@/lib/barrel2";\nexport async function loadB() { return k("/b/items"); }\n',
    "app/destructure_rename.ts": (
        'import * as api from "@/lib/api";\nconst { apiJson: get } = api;\n'
        'export async function loadC() { return get("/c/items"); }\n'
    ),
    "app/destructure_same.ts": (
        'import * as api from "@/lib/api";\nconst { apiJson } = api;\n'
        'export async function loadC2() { return apiJson("/c2/items"); }\n'
    ),
    "app/element_access.ts": (
        'import * as api from "@/lib/api";\nexport async function loadCC() { return api["apiJson"]("/cc/items"); }\n'
    ),
    "app/dynamic_destructure.ts": (
        'export async function loadC3() {\n  const { apiJson } = await import("@/lib/api");\n'
        '  return apiJson("/c3/items");\n}\n'
    ),
    "app/dynamic_namespace.ts": (
        'export async function loadC4() {\n  const api = await import("@/lib/api");\n'
        '  return api.apiJson("/c4/items");\n}\n'
    ),
    "app/aliased.ts": 'import { apiFetch as rawFetch } from "@/lib/api";\nexport const ping = () => rawFetch("/ping");\n',
    "app/unresolved.ts": 'export async function u(api: any) {\n  return api.apiJson("/u/items");\n}\n',
    "app/external.ts": 'import { apiJson } from "some-package";\nexport const e = () => apiJson("/external");\n',
    "app/keys.ts": (
        'import { apiFetch } from "@/lib/api";\nconst K = "method";\n'
        'export async function delD(id: string) { return apiFetch(`/d/${id}`, { [K]: "DELETE" }); }\n'
        "export async function anyKey(key: string) {\n"
        '  return apiFetch("/d2", { method: "POST", [key]: "DELETE" });\n}\n'
    ),
    "app/nullish.ts": (
        'import { apiJson } from "@/lib/api";\nexport async function getI(path?: string) { return apiJson(path ?? "/i/default"); }\n'
    ),
    "components/Btn.tsx": (
        'import { downloadExport } from "@/lib/utils/download-export";\n'
        'export function ExportBtn({ path = "/a/export" }: { path?: string }) {\n'
        "  return <button onClick={() => downloadExport({ url: path })}>x</button>;\n}\n"
    ),
    "app/page.tsx": (
        'import { ExportBtn } from "@/components/Btn";\n'
        'export function Page() { return <div><ExportBtn /><ExportBtn path="/b/export" /></div>; }\n'
    ),
    "components/Btn2.tsx": (
        'import { downloadExport } from "@/lib/utils/download-export";\n'
        "const memo = <T,>(c: T) => c;\n"
        "declare function observe<T>(c: T): T;\n"
        "export function ExportBtn2({ path }: { path: string }) {\n"
        "  return <button onClick={() => downloadExport({ url: path })}>x</button>;\n}\n"
        "export const Aliased = ExportBtn2;\n"
        "export const Memo2 = memo(ExportBtn2);\n"
        "export const Observed = observe(ExportBtn2);\n"
    ),
    "app/page2.tsx": (
        'import { ExportBtn2, Aliased, Memo2, Observed } from "@/components/Btn2";\n'
        "export function Page2() {\n"
        '  return <div><ExportBtn2 path="/a/export" /><Aliased path="/b/export" /><Memo2 path="/c/export" />'
        '<Observed path="/d/export" /></div>;\n}\n'
    ),
    "app/then_destructure.ts": 'export const f = () => import("@/lib/api").then(({ apiJson }) => apiJson("/n20"));\n',
    "app/then_namespace.ts": 'export const f = () => import("@/lib/api").then((m) => m.apiJson("/n20b"));\n',
    "app/require_ts.ts": 'const { apiJson: g } = require("@/lib/api");\nexport const f = () => g("/n23");\n',
    "app/param_binding.ts": 'export function p({ apiJson }: any) {\n  return apiJson("/pb");\n}\n',
    "components/DefaultBtn.tsx": (
        'import { downloadExport } from "@/lib/utils/download-export";\n'
        "export default function DefaultBtn({ path }: { path: string }) {\n"
        "  return <button onClick={() => downloadExport({ url: path })}>x</button>;\n}\n"
        "export function NamedBtn({ path }: { path: string }) {\n"
        "  return <button onClick={() => downloadExport({ url: path })}>x</button>;\n}\n"
    ),
    "app/default_sites.tsx": (
        'import dynamic from "next/dynamic";\n'
        'import { lazy } from "react";\n'
        'import Renamed from "@/components/DefaultBtn";\n'
        'const DynBtn = dynamic(() => import("@/components/DefaultBtn"));\n'
        'const LazyBtn = lazy(() => import("@/components/DefaultBtn"));\n'
        'const DynNamed = dynamic(() => import("@/components/DefaultBtn").then((mod) => mod.NamedBtn));\n'
        'const DynOpaque = dynamic(() => import("@/components/DefaultBtn").then((mod) => ({ default: mod.NamedBtn })));\n'
        "export function Sites() {\n"
        '  return <div><Renamed path="/r/export" /><DynBtn path="/d/export" /><LazyBtn path="/l/export" />'
        '<DynNamed path="/n/export" /><DynOpaque path="/o/export" /></div>;\n}\n'
    ),
    "other/styled.tsx": (
        'import styled from "styled-components";\nconst ExportBtn = styled.button``;\n'
        'export function T() { return <ExportBtn path="/zzz" />; }\n'
    ),
    "app/multiline.ts": 'import * as api from "@/lib/api";\nexport function g() {\n  return api\n    .apiFetch("/ml");\n}\n',
    "lib/ws_concat.ts": (
        "declare function getWsBaseUrl(): string;\nexport function open(id: string) {\n"
        '  return new WebSocket(getWsBaseUrl() + "/ws/articles/" + id + "/heartbeat");\n}\n'
    ),
    "lib/ws.ts": (
        "export function connect(id: string) {\n  const url = `/ws/articles/${id}/heartbeat`;\n"
        '  try { return new WebSocket(url); } catch { throw new Error("could not reach /ws/articles"); }\n}\n'
    ),
}


@pytest.fixture(scope="module")
def resolution(scan_tree) -> Scan:
    return scan_tree(base_tree(EXPORTED_CLIENTS, **RESOLUTION_FILES))


def _line(rel: str, snippet: str) -> int:
    return next(i + 1 for i, line in enumerate(RESOLUTION_FILES[rel].split("\n")) if snippet in line)


def _call(scan: Scan, rel: str, snippet: str) -> dict:
    return scan.one(rel, _line(rel, snippet))


@pytest.mark.parametrize(
    ("rel", "snippet", "path", "callee"),
    [
        ("app/default_reexport.ts", 'j("/a/items")', "/a/items", "j"),
        ("app/default_expr.ts", 'k("/b/items")', "/b/items", "k"),
        ("app/destructure_rename.ts", 'get("/c/items")', "/c/items", "get"),
        ("app/destructure_same.ts", 'apiJson("/c2/items")', "/c2/items", "apiJson"),
        ("app/element_access.ts", '["apiJson"]', "/cc/items", '"apiJson"'),
        ("app/dynamic_destructure.ts", 'apiJson("/c3/items")', "/c3/items", "apiJson"),
        ("app/dynamic_namespace.ts", "api.apiJson", "/c4/items", "apiJson"),
        ("app/aliased.ts", "rawFetch(", "/ping", "rawFetch"),
        ("app/then_destructure.ts", 'apiJson("/n20")', "/n20", "apiJson"),
        ("app/then_namespace.ts", "m.apiJson", "/n20b", "apiJson"),
        ("app/require_ts.ts", 'g("/n23")', "/n23", "g"),
    ],
)
def test_client_calls_through_default_exports_destructuring_element_access_and_dynamic_import(
    resolution: Scan, rel: str, snippet: str, path: str, callee: str
) -> None:
    call = _call(resolution, rel, snippet)
    assert [render(u) for u in call["urls"]] == [path]
    assert call["client"] in ("apiJson", "apiFetch")
    # The callee as written, at its column: a reader can check the call from the line text.
    line = RESOLUTION_FILES[rel].split("\n")[call["callee_line"] - 1]
    assert call["callee"] == callee
    assert line[call["callee_column"] - 1 :].startswith(callee)


def test_aliased_client_rows_carry_the_callee_for_route_map_rows(resolution: Scan) -> None:
    result = route_map.build_routes(model(live("GET", "/ping", 0)), resolution.result)
    [row] = [r for r in result["frontend_calls"] if r["file"] == "frontend/app/aliased.ts"]
    assert (row["client"], row["callee"], row["callee_line"], row["callee_column"]) == ("apiFetch", "rawFetch", 2, 27)


def test_a_member_call_split_over_lines_names_the_callee_line(resolution: Scan) -> None:
    call = resolution.one("app/multiline.ts", 3)
    assert (call["column"], call["callee"], call["callee_line"], call["callee_column"]) == (10, "apiFetch", 4, 6)
    result = route_map.build_routes(model(live("GET", "/ml", 0)), resolution.result)
    [row] = [r for r in result["frontend_calls"] if r["file"] == "frontend/app/multiline.ts"]
    assert (row["line"], row["callee_line"], row["callee_column"]) == (3, 4, 6)


def test_re_exports_are_not_uncalled_client_references(resolution: Scan) -> None:
    flagged = {p.file for p in resolution.kinds("unparsed_frontend_callsite")}
    assert "frontend/lib/barrel2.ts" not in flagged
    assert "frontend/app/destructure_rename.ts" not in flagged


def test_a_client_name_whose_declaration_cannot_be_found_is_a_diagnostic(resolution: Scan) -> None:
    assert resolution.at("app/unresolved.ts", 2) == []
    [problem] = [p for p in resolution.kinds("unparsed_frontend_callsite") if p.file == "frontend/app/unresolved.ts"]
    assert (problem.line, problem.step) == (2, "resolve client")
    # The message says what could not be resolved; nothing failed.
    assert problem.message == (
        "frontend/app/unresolved.ts:2: could not resolve callee apiJson: no declaration was found for it, "
        'so whether it is an API client is unknown (client_not_resolved): api.apiJson("/u/items")'
    )
    # A client-named prop of an untyped parameter may be the client.
    [param] = [p for p in resolution.kinds("unparsed_frontend_callsite") if p.file == "frontend/app/param_binding.ts"]
    assert (param.line, "client_not_resolved" in param.message) == (2, True)
    # A same-named function imported from a package is never a frontend client.
    assert resolution.at("app/external.ts", 2) == []
    assert not [p for p in resolution.problems if p.file == "frontend/app/external.ts"]


def test_a_computed_init_key_is_evaluated_or_makes_the_method_unknown(resolution: Scan) -> None:
    deleted = _call(resolution, "app/keys.ts", "[K]")
    assert (deleted["method"], deleted["method_source"]) == ("DELETE", "init")
    unknown = _call(resolution, "app/keys.ts", "[key]")
    assert (unknown["method"], unknown["method_source"]) == ("UNKNOWN", "unresolved")
    [lim] = [p for p in resolution.kinds("unresolved_http_method") if p.file == "frontend/app/keys.ts"]
    assert "a computed key in the init object may set the method" in lim.message


def test_a_url_that_starts_with_an_unreadable_value_is_not_static(resolution: Scan) -> None:
    assert resolution.paths("app/nullish.ts", 2) == ["/i/default"]
    [problem] = [p for p in resolution.kinds("unresolved_frontend_path") if p.file == "frontend/app/nullish.ts"]
    assert "(url_not_static)" in problem.message


def test_prop_flow_uses_the_default_for_sites_that_omit_the_prop(resolution: Scan) -> None:
    call = _call(resolution, "components/Btn.tsx", "downloadExport({")
    assert sorted((render(u), u["confidence"], u["origin"]) for u in call["urls"]) == [
        ("/a/export", "possible", "prop_flow"),
        ("/b/export", "possible", "prop_flow"),
    ]


def test_prop_flow_follows_const_aliases_and_memo_and_flags_untraceable_sites(resolution: Scan) -> None:
    call = _call(resolution, "components/Btn2.tsx", "downloadExport({")
    assert sorted(render(u) for u in call["urls"]) == ["/a/export", "/b/export", "/c/export"]
    # `observe(...)` is not a known wrapper: its site's value stays unknown and is
    # named, and the URL that is only that value is a diagnostic.
    [lim] = [p for p in resolution.kinds("unresolved_prop_value") if p.file == "frontend/app/page2.tsx"]
    assert "Observed" in lim.message and "ExportBtn2" in lim.message
    [lost] = [p for p in resolution.kinds("unresolved_frontend_path") if p.file == "frontend/components/Btn2.tsx"]
    assert "(url_not_static)" in lost.message


def test_prop_flow_reaches_default_imports_dynamic_and_lazy_sites(resolution: Scan) -> None:
    calls = resolution.at("components/DefaultBtn.tsx", 3)
    assert sorted(render(u) for c in calls for u in c["urls"]) == ["/d/export", "/l/export", "/r/export"]
    named = resolution.at("components/DefaultBtn.tsx", 6)
    assert [render(u) for c in named for u in c["urls"]] == ["/n/export"]
    # `.then((mod) => ({ default: mod.NamedBtn }))` picks its export in a way the
    # analysis cannot read: the site is named, never dropped.
    lims = [p for p in resolution.kinds("unresolved_prop_value") if p.file == "frontend/app/default_sites.tsx"]
    assert ["DynOpaque" in p.message for p in lims] == [True, True]


_DL = (
    'import { downloadExport } from "@/lib/utils/download-export";\nimport React, { memo, forwardRef } from "react";\n'
)
_CLICK = "  return <button onClick={() => downloadExport({ url: path })}>x</button>;\n"
WRAPPED_DEFAULTS = {
    "components/MemoBtn.tsx": _DL
    + "export function ExportBtn({ path }: { path: string }) {\n"
    + _CLICK
    + "}\nexport default memo(ExportBtn);\n",
    "components/ReactMemoBtn.tsx": _DL
    + "function RBtn({ path }: { path: string }) {\n"
    + _CLICK
    + "}\nexport default React.memo(RBtn);\n",
    "components/FwdBtn.tsx": _DL
    + "export default forwardRef(function FwdBtn({ path }: { path: string }, ref) {\n"
    + _CLICK
    + "});\n",
    "components/NestedBtn.tsx": _DL
    + "export default memo(forwardRef(function NestBtn({ path }: { path: string }, ref) {\n"
    + _CLICK
    + "}));\n",
    "components/AnonBtn.tsx": _DL + "export default memo(({ path }: { path: string }) => {\n" + _CLICK + "});\n",
    "app/sites.tsx": (
        'import { lazy } from "react";\n'
        'import Memo, { ExportBtn } from "@/components/MemoBtn";\n'
        'import RMemo from "@/components/ReactMemoBtn";\n'
        'import Fwd from "@/components/FwdBtn";\n'
        'import Nested from "@/components/NestedBtn";\n'
        'import Anon from "@/components/AnonBtn";\n'
        'const Lazy = lazy(() => import("@/components/MemoBtn"));\n'
        "export function Sites() {\n"
        '  return <div><ExportBtn path="/g1/a" /><Memo path="/g1/b" /><Lazy path="/f12" /><RMemo path="/rm" />'
        '<Fwd path="/g2/a" /><Fwd path="/g2/b" /><Nested path="/nest" /><Anon path="/anon" /></div>;\n}\n'
    ),
}


@pytest.mark.parametrize(
    ("rel", "line", "paths"),
    [
        ("components/MemoBtn.tsx", 4, ["/f12", "/g1/a", "/g1/b"]),
        ("components/ReactMemoBtn.tsx", 4, ["/rm"]),
        ("components/FwdBtn.tsx", 4, ["/g2/a", "/g2/b"]),
        ("components/NestedBtn.tsx", 4, ["/nest"]),
        ("components/AnonBtn.tsx", 4, ["/anon"]),
    ],
)
def test_prop_flow_reaches_sites_of_a_wrapped_default_export(scan_tree, rel: str, line: int, paths: list[str]) -> None:
    """`export default memo(X)`, `React.memo(X)`, `forwardRef(function X ...)`,
    nested wrappers and an anonymous inline component all reach their default
    imports and `lazy()` loads; nothing is dropped or left unexplained."""
    scan = scan_tree(base_tree(**WRAPPED_DEFAULTS))
    assert sorted(scan.paths(rel, line)) == paths
    assert scan.problems == []


def test_an_unrelated_component_with_the_same_name_is_not_a_prop_flow_site(resolution: Scan) -> None:
    assert not [p for p in resolution.problems if p.file == "frontend/other/styled.tsx"]
    call = _call(resolution, "components/Btn.tsx", "downloadExport({")
    assert all("zzz" not in render(u) for u in call["urls"])


def test_generated_output_directly_under_frontend_is_skipped(scan_tree) -> None:
    call = 'import { apiJson } from "@/lib/api";\nexport const f = () => apiJson("/built");\n'
    scan = scan_tree(
        base_tree(
            EXPORTED_CLIENTS,
            **{"app/build/page.ts": call, "coverage/lcov-report/x.ts": call, "test-results/x.ts": call},
        )
    )
    assert [c["file"] for c in scan.calls] == ["frontend/app/build/page.ts"]


def test_text_with_whitespace_around_the_marker_is_not_a_websocket_url(resolution: Scan) -> None:
    ws = [c for c in resolution.calls if c["kind"] == "websocket" and c["file"] == "frontend/lib/ws.ts"]
    assert [(c["line"], render(c["urls"][0])) for c in ws] == [(2, "/ws/articles/{param:id}/heartbeat")]
    assert all((c["callee"], c["callee_line"], c["callee_column"]) == (None, None, None) for c in ws)


def test_a_concatenated_websocket_url_starts_at_the_expression(resolution: Scan) -> None:
    [ws] = [c for c in resolution.calls if c["file"] == "frontend/lib/ws_concat.ts"]
    assert (ws["kind"], ws["line"], ws["column"]) == ("websocket", 3, 24)
    assert (ws["callee"], ws["callee_line"], ws["callee_column"]) == (None, None, None)
    assert [render(u) for u in ws["urls"]] == ["/ws/articles/{param:id}/heartbeat"]


def test_route_map_end_to_end_on_the_resolution_cases(resolution: Scan) -> None:
    routes = [
        live("GET", "/a/items", 0),
        live("GET", "/c/items", 1),
        live("DELETE", "/d/{id}", 2),
        live("GET", "/d/{id}", 3),
        live("GET", "/a/export", 4),
        live("GET", "/b/export", 5),
    ]
    result = route_map.build_routes(model(*routes), resolution.result)
    pairs = {
        (m["frontend"]["file"], m["backend"]["method"], m["backend"]["path"], m["reason"]) for m in result["matches"]
    }
    assert ("frontend/app/default_reexport.ts", "GET", "/a/items", "literal") in pairs
    assert ("frontend/app/keys.ts", "DELETE", "/d/{id}", "param") in pairs
    assert ("frontend/app/keys.ts", "GET", "/d/{id}", "param") not in pairs
    assert ("frontend/components/Btn.tsx", "GET", "/a/export", "prop_flow") in pairs
    assert ("frontend/components/Btn.tsx", "GET", "/b/export", "prop_flow") in pairs


# ── One file must never take the whole frontend down ──────────────────────

DEEP_FILES = {
    "app/plus.tsx": "export const big = " + " + ".join(['"a"'] * 20_000) + ";\n",
    "app/arrays.tsx": "export const deep = " + "[" * 3_000 + "1" + "]" * 3_000 + ";\n",
    "app/jsx.tsx": "export const J = () => " + "<div>" * 2_000 + "x" + "</div>" * 2_000 + ";\n",
    "app/ok.ts": 'import { apiJson } from "@/lib/api";\nexport const q = () => apiJson("/ok");\n',
}


def test_deeply_nested_files_are_parsed_and_analyzed_like_any_other(scan_tree) -> None:
    """These shapes overflowed the TypeScript 6 parser (a RangeError, reported
    as parse_failed); the native parser handles them, and so does the analysis
    (a 20,000-term `+` chain, 3,000 nested arrays, 2,000 nested JSX elements)."""
    scan = scan_tree(base_tree(EXPORTED_CLIENTS, **DEEP_FILES))
    assert scan.paths("app/ok.ts", 2) == ["/ok"]
    assert not scan.kinds("parse_failed")
    assert not [p for p in scan.problems if p.file.startswith("frontend/app/") and p.file != "frontend/app/ok.ts"]


def test_bytes_that_are_not_utf8_are_a_decode_failed_diagnostic(scan_tree) -> None:
    scan = scan_tree(
        base_tree(
            EXPORTED_CLIENTS,
            **{
                "app/latin1.ts": b'import { apiJson } from "@/lib/api";\n// caf\xe9\nexport const q = () => apiJson("/x");\n'
            },
        )
    )
    [problem] = scan.kinds("decode_failed")
    assert (problem.file, problem.step) == ("frontend/app/latin1.ts", "decode file")
    assert "byte 43 is not valid UTF-8" in problem.message
    assert scan.paths("app/latin1.ts", 3) == ["/x"]


# ── Routes JSON v2 (build_routes) ────────────────────────────────────────

_ENDPOINT = Located("case", ast.Pass())
# The include model of the FastAPI version the backend locks.
_COPY = include_model.for_version("0.135.2", source=None)


def live(method: str, path: str, order: int, *, kind: RouteKind = "http", handler: str | None = None) -> LiveRoute:
    return LiveRoute(
        method=method,
        path=path,
        kind=kind,
        handler=handler or f"h{order}",
        handler_id=f"case.routes:h{order}",
        file="backend/case/routes.py",
        line=10 + order,
        registered_file="backend/case/routes.py",
        registered_line=9 + order,
        chain=("case.main:app",),
        mount="static",
        layers=(),
        order=order,
        endpoint=_ENDPOINT,
    )


def model(
    *routes: LiveRoute,
    diagnostics: tuple[Problem, ...] = (),
    limitations: tuple[Problem, ...] = (),
    unmounted: tuple[LiveRoute, ...] = (),
) -> BackendModel:
    return BackendModel(
        app_id="case.main:app",
        redirect_slashes=False,
        routes=tuple(routes),
        unmounted_routes=unmounted,
        routers=(),
        edges=(),
        diagnostics=diagnostics,
        limitations=limitations,
        files_scanned=1,
        sources=None,
        resolver=FakeResolver(FakeSources({})),
    )


def call(path_parts: list[dict], *, file: str = "frontend/lib/api.ts", line: int = 1, column: int = 10, **kw) -> dict:
    return {
        "file": file,
        "line": line,
        "column": column,
        "is_test": kw.pop("is_test", False),
        "kind": kw.pop("kind", "call"),
        "client": kw.pop("client", "apiJson"),
        "builder": None,
        "function": kw.pop("function", "fn"),
        "def_line": kw.pop("def_line", 1),
        "method": kw.pop("method", "GET"),
        "method_source": "default",
        "urls": kw.pop("urls", [{"parts": path_parts, "confidence": "exact", "origin": "literal", "via": []}]),
        "raw": kw.pop("raw", "`...`"),
    }


def scan_of(*calls: dict, problems: list[Problem] | None = None) -> route_map.FrontendScan:
    return route_map.FrontendScan(calls=list(calls), problems=problems or [], files_scanned=1, typescript_version="6")


def test_build_routes_names_the_app_and_its_include_model() -> None:
    m = replace(model(live("GET", "/a", 0)), include_model=_COPY)
    assert route_map.build_routes(m, scan_of())["app"] == {
        "id": "case.main:app",
        "redirect_slashes": False,
        "fastapi_version": "0.135.2",
        "include_model": "copy",
        "include_model_reason": None,
    }


def test_build_routes_summary_separates_routes_calls_and_rows() -> None:
    m = model(live("GET", "/a", 0), live("GET", "/b", 1))
    scan = scan_of(
        call([{"text": "/a"}], line=1, function="one"),
        call([{"text": "/a"}], line=2, function="two"),
    )
    result = route_map.build_routes(m, scan)
    s = result["summary"]
    assert (s["match_rows"], s["matched_routes"], s["matched_calls"], s["backend_only"]) == (2, 1, 2, 1)
    assert (s["frontend_calls"], s["frontend_only"], s["coverage_pct"]) == (2, 0, 50.0)
    assert result["complete"] is True and result["schema_version"] == 2
    assert [r["path"] for r in result["backend_only"]] == ["/b"]
    assert result["matches"][0]["backend"] == {
        "method": "GET",
        "path": "/a",
        "handler": "h0",
        "file": "backend/case/routes.py",
        "line": 10,
        "library": None,
    }


def library_route(method: str, path: str, order: int, handler: str = "me") -> LiveRoute:
    """A route installed code defines, mounted by the include at backend/app/main.py:170."""
    return replace(
        live(method, path, order, handler=handler),
        file="backend/app/main.py",
        line=170,
        registered_file="backend/app/main.py",
        registered_line=170,
        endpoint=CapturedEndpoint(
            f"fastapi_users.router.users:get_users_router.<locals>.{handler}",
            Library("fastapi-users", "15.0.5", "fastapi_users/router/users.py", 46),
            (),
        ),
    )


def test_a_library_route_matches_calls_but_is_in_neither_backend_only_nor_coverage() -> None:
    m = model(live("GET", "/a", 0), library_route("GET", "/users/me", 1), library_route("POST", "/users/x", 2, "x"))
    scan = scan_of(
        call([{"text": "/a"}], line=1, function="one"),
        call([{"text": "/users/me"}], line=2, function="me"),
        call([{"text": "/users/me"}], line=3, function="again"),
    )
    result = route_map.build_routes(m, scan)
    s = result["summary"]
    assert (s["backend_routes"], s["matched_routes"], s["backend_only"], s["coverage_pct"]) == (1, 1, 0, 100.0)
    assert (s["library_routes"], s["matched_library_routes"]) == (2, 1)
    assert (s["frontend_calls"], s["matched_calls"], s["frontend_only"], s["match_rows"]) == (3, 3, 0, 3)
    assert [r["path"] for r in result["backend_routes"]] == ["/a"]
    assert [(r["path"], (r["library"] or {}).get("dist")) for r in result["library_routes"]] == [
        ("/users/me", "fastapi-users"),
        ("/users/x", "fastapi-users"),
    ]
    assert result["backend_only"] == []
    library_matches = [m for m in result["matches"] if m["backend"]["library"] is not None]
    assert [m["backend"]["path"] for m in library_matches] == ["/users/me", "/users/me"]
    assert library_matches[0]["backend"]["file"] == "backend/app/main.py"
    assert library_matches[0]["backend"]["library"] == {
        "dist": "fastapi-users",
        "version": "15.0.5",
        "file": "fastapi_users/router/users.py",
        "line": 46,
    }
    assert result["matches"][0]["backend"]["library"] is None


def test_a_library_route_without_a_caller_is_not_backend_only_and_a_call_that_hits_none_is_frontend_only() -> None:
    m = model(live("GET", "/a", 0), library_route("GET", "/users/me", 1))
    result = route_map.build_routes(m, scan_of(call([{"text": "/nothing"}], line=1)))
    assert result["backend_only"][0]["path"] == "/a"
    assert [r["path"] for r in result["backend_only"]] == ["/a"]
    assert result["summary"]["matched_library_routes"] == 0
    assert [c["path"] for c in result["frontend_only"]] == ["/nothing"]


def test_the_report_names_where_the_library_defines_a_matched_handler_and_counts_library_routes() -> None:
    m = model(live("GET", "/a", 0), library_route("GET", "/users/me", 1))
    result = route_map.build_routes(m, scan_of(call([{"text": "/users/me"}], line=2, function="me")))
    out = io.StringIO()
    route_map.print_report(result, out=out)
    text = out.getvalue()
    assert "handler me()  [backend/app/main.py:170 · fastapi-users 15.0.5 fastapi_users/router/users.py:46]" in text
    assert "  Backend routes:  1 (0 matched, 1 backend-only)" in text
    assert "  Library routes:  1 (1 matched)" in text
    plain = io.StringIO()
    route_map.print_report(route_map.build_routes(model(live("GET", "/a", 0)), scan_of()), out=plain)
    assert "Library routes" not in plain.getvalue()


def test_dedupe_keeps_same_line_calls_in_different_files_and_columns() -> None:
    m = model(live("GET", "/squads/{squad_id}", 0))
    parts = [{"text": "/squads/"}, {"hole": "param", "expr": "id"}]
    scan = scan_of(
        call(parts, file="frontend/app/A.tsx", line=39),
        call(parts, file="frontend/app/B.tsx", line=39),
        call(parts, file="frontend/app/B.tsx", line=39, column=40),
        call(
            [],
            file="frontend/lib/api.ts",
            line=5,
            urls=[
                {
                    "parts": [{"text": "/squads?"}, {"hole": "query", "expr": "q"}],
                    "confidence": "exact",
                    "origin": "conditional",
                    "via": [],
                },
                {"parts": [{"text": "/squads"}], "confidence": "exact", "origin": "conditional", "via": []},
            ],
        ),
    )
    result = route_map.build_routes(m, scan)
    rows = [(r["file"], r["line"], r["column"], r["path"]) for r in result["frontend_calls"]]
    assert rows == [
        ("frontend/app/A.tsx", 39, 10, "/squads/{id}"),
        ("frontend/app/B.tsx", 39, 10, "/squads/{id}"),
        ("frontend/app/B.tsx", 39, 40, "/squads/{id}"),
        ("frontend/lib/api.ts", 5, 10, "/squads"),
    ]
    assert result["summary"]["match_rows"] == 3
    assert result["summary"]["frontend_calls"] == 4


def test_build_routes_reports_possible_prop_flow_matches_and_frontend_only_rows() -> None:
    m = model(live("GET", "/admin/audit/investigation/changes/export", 0))
    scan = scan_of(
        call(
            [],
            kind="options",
            client="downloadExport",
            urls=[
                {
                    "parts": [{"text": "/admin/audit/investigation/changes/export"}],
                    "confidence": "possible",
                    "origin": "prop_flow",
                    "via": [],
                },
                {
                    "parts": [{"text": "/admin/audit/investigation/admin-actions/export"}],
                    "confidence": "possible",
                    "origin": "prop_flow",
                    "via": [],
                },
            ],
        ),
    )
    result = route_map.build_routes(m, scan)
    assert [(x["confidence"], x["frontend"]["path"]) for x in result["matches"]] == [
        ("possible", "/admin/audit/investigation/changes/export")
    ]
    assert [r["path"] for r in result["frontend_only"]] == ["/admin/audit/investigation/admin-actions/export"]
    assert result["frontend_calls"][0]["origin"] == "prop_flow"


def test_build_routes_skips_test_calls_and_counts_them() -> None:
    m = model(live("GET", "/a", 0))
    scan = scan_of(call([{"text": "/a"}], is_test=True, file="frontend/test/x.ts"), call([{"text": "/b"}], line=3))
    result = route_map.build_routes(m, scan)
    assert result["summary"]["test_calls_skipped"] == 1
    assert [r["file"] for r in result["frontend_calls"]] == ["frontend/lib/api.ts"]
    assert [r["path"] for r in result["frontend_only"]] == ["/b"]
    assert [r["path"] for r in result["backend_only"]] == ["/a"]


def test_build_routes_limitations_never_flip_complete() -> None:
    lim = limitation("unresolved_http_method", stage="frontend routes", file="frontend/lib/api.ts", line=3, message="m")
    m = model(
        live("GET", "/a", 0), limitations=(limitation("opaque_router_call", stage="s", file="f", line=1, message="x"),)
    )
    result = route_map.build_routes(m, scan_of(call([{"text": "/a"}]), problems=[lim]))
    assert result["complete"] is True
    assert result["summary"]["coverage_pct"] == 100.0
    assert [x["kind"] for x in result["analysis_limitations"]] == ["opaque_router_call", "unresolved_http_method"]


def test_build_routes_any_diagnostic_nulls_coverage() -> None:
    lost = diagnostic("parse_failed", stage="backend", step="parse file", file="backend/x.py", message="m")
    result = route_map.build_routes(model(live("GET", "/ok", 0), diagnostics=(lost,)), scan_of(call([{"text": "/ok"}])))
    assert result["complete"] is False
    assert result["summary"]["coverage_pct"] is None
    assert result["diagnostics"] == [lost.as_dict()]
    assert result["summary"]["backend_routes"] == 1


def test_build_routes_lists_unmounted_routes_separately() -> None:
    dead = live("GET", "/dead", 7)
    result = route_map.build_routes(model(live("GET", "/a", 0), unmounted=(dead,)), scan_of())
    assert [r["path"] for r in result["unmounted_routes"]] == ["/dead"]
    assert [r["path"] for r in result["backend_routes"]] == ["/a"]


# ── Instance and global clients ──────────────────────────────────────────

INSTANCE_CONFIG = """
[[frontend.clients]]
kind = "instance"
module = "frontend/lib/http.ts"
export = "api"
base_path = "/api"

[[frontend.clients]]
kind = "global"
name = "fetch"
url_arg = 0
init_arg = 1
same_origin_only = true
"""
INSTANCES = parse_config(MINIMAL_CONFIG + INSTANCE_CONFIG).frontend
HTTP_TS = """\
declare const axios: { create(options: { baseURL: string }): any };
export const api = axios.create({ baseURL: "/api" });
api.interceptors.request.use((config: unknown) => config);
export default api;
"""
INSTANCE_CALLS = """\
import api, { api as http } from "../lib/http";
import * as client from "../lib/http";
import { api as viaAlias } from "@/lib/http";

export const list = () => api.get("/accounts");
export const create = (body: unknown) => api.post("/accounts", body);
export const update = (id: string) => api.put(`/accounts/${id}`, {});
export const patch = (id: string) => api.patch(`/accounts/${id}`, {});
export const remove = (id: string) => api.delete(`/accounts/${id}`);
export const headIt = () => api.head("/ping");
export const optionsIt = () => api.options("/ping");
export const typed = () => api.get<string[]>("/typed");
export const renamed = () => http.get("/renamed");
export const aliased = () => viaAlias.get("/aliased");
export const namespaced = () => client.api.get("/namespaced");
export const element = () => api["delete"]("/element");
export const noSlash = () => api.get("relative");
export const root = () => api.get("/");
export const query = (q: string) => api.get(`/search?q=${q}`);
export const requestPost = () => api.request({ url: "/req", method: "post" });
export const requestDefault = () => api.request({ url: "/req-default" });
const URL = "/req-const";
export const requestShorthand = () => api.request({ url: URL, method: "put" });
export const requestDynamic = (method: "get" | "post") => api.request({ url: "/req-dynamic", method });
export const direct = () => api({ url: "/direct", method: "PATCH" });
export const absolute = () => api.get("https://example.com/x");
export const hole = (base: string) => api.get(`${base}/x`);
export const rawOptions = (opts: { url: string }) => api.request(opts);
export const spread = (args: [string]) => api.get(...args);
export const setup = () => api.interceptors.response.use((r: unknown) => r);
export const passed = () => takes(api);
export const picked = () => takes(api.get);
export const copied = api.get;
export const own = (api: { get(url: string): void }) => api.get("/not-the-client");
declare function takes(x: unknown): void;
"""
FETCH_CALLS = """\
import { fetch as shadow } from "../lib/shadow";

const LATEST = "https://api.example.com/releases/latest";
const ABSOLUTE = (name: string) => `https://example.com/${name}`;

export const same = () => fetch("/api/agents/1/chat", { method: "POST" });
export const dynamic = (id: string) => fetch(`/api/agents/${id}/chat`);
export const external = () => fetch(LATEST, { headers: {} });
export const external2 = () => fetch(ABSOLUTE("x"));
export const protocolRelative = () => fetch("//cdn.example.com/x");
export const unknownBase = (base: string) => fetch(`${base}/x`);
export const imported = () => shadow("/shadowed");
export function local(fetch: (url: string) => void) {
  fetch("/local");
}
export const viaGlobalThis = () => globalThis.fetch("/not-followed");
"""


@pytest.fixture(scope="module")
def instances(scan_tree) -> Scan:
    tree = {
        "tsconfig.json": TSCONFIG,
        "lib/http.ts": HTTP_TS,
        "lib/shadow.ts": "export const fetch = (url: string) => url;\n",
        "app/calls.ts": INSTANCE_CALLS,
        "app/fetches.ts": FETCH_CALLS,
    }
    return scan_tree(tree, INSTANCES)


def _instance_line(snippet: str, source: str = INSTANCE_CALLS) -> int:
    return next(i + 1 for i, line in enumerate(source.split("\n")) if snippet in line)


def _instance(scan: Scan, snippet: str) -> list[tuple[str, str]]:
    return [(c["method"], render(u)) for c in scan.at("app/calls.ts", _instance_line(snippet)) for u in c["urls"]]


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("const list", ("GET", "/api/accounts")),
        ("const create", ("POST", "/api/accounts")),
        ("const update", ("PUT", "/api/accounts/{param:id}")),
        ("const patch", ("PATCH", "/api/accounts/{param:id}")),
        ("const remove", ("DELETE", "/api/accounts/{param:id}")),
        ("const headIt", ("HEAD", "/api/ping")),
        ("const optionsIt", ("OPTIONS", "/api/ping")),
        ("const typed", ("GET", "/api/typed")),
        ("const element", ("DELETE", "/api/element")),
        ("const noSlash", ("GET", "/api/relative")),
        ("const root", ("GET", "/api/")),
        ("const query", ("GET", "/api/search?q={query:q}")),
    ],
)
def test_instance_verbs_take_the_method_from_the_member_and_the_base_path_from_the_config(
    instances: Scan, snippet: str, expected: tuple[str, str]
) -> None:
    assert _instance(instances, snippet) == [expected]
    [call] = instances.at("app/calls.ts", _instance_line(snippet))
    assert (call["kind"], call["client"]) == ("instance", "api")
    assert call["method_source"] == "member"


def test_an_instance_is_followed_through_default_named_aliased_and_namespace_imports(instances: Scan) -> None:
    assert _instance(instances, "const renamed") == [("GET", "/api/renamed")]
    assert _instance(instances, "const aliased") == [("GET", "/api/aliased")]
    assert _instance(instances, "const namespaced") == [("GET", "/api/namespaced")]
    [row] = instances.at("app/calls.ts", _instance_line("const renamed"))
    assert (row["callee"], row["client"]) == ("get", "api")


def test_instance_request_reads_url_and_method_from_the_config_object(instances: Scan) -> None:
    assert _instance(instances, "const requestPost") == [("POST", "/api/req")]
    assert _instance(instances, "const requestDefault") == [("GET", "/api/req-default")]
    assert _instance(instances, "const requestShorthand") == [("PUT", "/api/req-const")]
    [call] = instances.at("app/calls.ts", _instance_line("const requestPost"))
    assert call["method_source"] == "init"
    assert _instance(instances, "const direct") == [("PATCH", "/api/direct")]


def test_instance_request_with_an_unreadable_method_is_unknown_and_reported(instances: Scan) -> None:
    [call] = instances.at("app/calls.ts", _instance_line("const requestDynamic"))
    assert (call["method"], call["method_source"]) == ("UNKNOWN", "unresolved")
    [limit] = [p for p in instances.kinds("unresolved_http_method") if p.line == call["line"]]
    assert limit.message.startswith("api call: HTTP method could not be evaluated")


def test_instance_calls_that_cannot_be_read_are_diagnostics_not_rows(instances: Scan) -> None:
    for snippet, kind in [
        ("const absolute", "unresolved_frontend_path"),
        ("const hole", "unresolved_frontend_path"),
        ("const rawOptions", "unparsed_frontend_callsite"),
        ("const spread", "unparsed_frontend_callsite"),
    ]:
        line = _instance_line(snippet)
        assert instances.at("app/calls.ts", line) == [], snippet
        assert [p.kind for p in instances.problems if p.file == "frontend/app/calls.ts" and p.line == line] == [kind]


def test_instance_setup_members_are_not_calls_or_references(instances: Scan) -> None:
    line = _instance_line("const setup")
    assert instances.at("app/calls.ts", line) == []
    assert [p for p in instances.problems if p.line == line] == []
    assert [p for p in instances.problems if p.file == "frontend/lib/http.ts"] == []


def test_an_instance_passed_on_or_copied_by_member_is_reported_as_an_uncalled_reference(instances: Scan) -> None:
    for snippet in ("const passed", "const picked", "const copied"):
        line = _instance_line(snippet)
        assert [p.kind for p in instances.problems if p.file == "frontend/app/calls.ts" and p.line == line] == [
            "unparsed_frontend_callsite"
        ], snippet


def test_a_local_with_the_instance_name_is_not_the_instance(instances: Scan) -> None:
    line = _instance_line("const own")
    assert instances.at("app/calls.ts", line) == []
    assert [p for p in instances.problems if p.file == "frontend/app/calls.ts" and p.line == line] == []


def _fetch(scan: Scan, snippet: str) -> list[tuple[str, str]]:
    line = _instance_line(snippet, FETCH_CALLS)
    return [(c["method"], render(u)) for c in scan.at("app/fetches.ts", line) for u in c["urls"]]


def test_global_fetch_with_a_same_origin_path_is_a_call_with_the_init_method(instances: Scan) -> None:
    assert _fetch(instances, "const same") == [("POST", "/api/agents/1/chat")]
    assert _fetch(instances, "const dynamic") == [("GET", "/api/agents/{param:id}/chat")]
    [call] = instances.at("app/fetches.ts", _instance_line("const same", FETCH_CALLS))
    assert (call["kind"], call["client"], call["method_source"]) == ("global", "fetch", "init")


def test_global_fetch_of_an_absolute_url_is_no_call_and_no_diagnostic(instances: Scan) -> None:
    for snippet in ("const external ", "const external2", "const protocolRelative"):
        line = _instance_line(snippet, FETCH_CALLS)
        assert instances.at("app/fetches.ts", line) == [], snippet
        assert [p for p in instances.problems if p.file == "frontend/app/fetches.ts" and p.line == line] == []


def test_global_fetch_of_an_unreadable_base_is_a_diagnostic(instances: Scan) -> None:
    line = _instance_line("const unknownBase", FETCH_CALLS)
    assert [p.kind for p in instances.problems if p.file == "frontend/app/fetches.ts" and p.line == line] == [
        "unresolved_frontend_path"
    ]


def test_an_imported_or_local_fetch_is_not_the_global_one(instances: Scan) -> None:
    for snippet in ("const imported", 'fetch("/local")', "const viaGlobalThis"):
        assert instances.at("app/fetches.ts", _instance_line(snippet, FETCH_CALLS)) == [], snippet


def test_instance_and_global_rows_match_backend_routes_without_a_diagnostic(instances: Scan) -> None:
    result = route_map.build_routes(
        model(
            live("GET", "/api/accounts", 0), live("POST", "/api/agents/{agent_id}/chat", 1), live("GET", "/api/x", 2)
        ),
        instances.result,
    )
    matched = {(m["backend"]["path"], m["frontend"]["function"], m["frontend"]["client"]) for m in result["matches"]}
    assert ("/api/accounts", "list", "api") in matched
    assert ("/api/agents/{agent_id}/chat", "same", "fetch") in matched


PACKAGE_CONFIG = """
[[frontend.clients]]
kind = "instance"
package = "axios"

[[frontend.clients]]
kind = "instance"
package = "got"
export = "got"
base_path = "/api"
"""
PACKAGES = parse_config(MINIMAL_CONFIG + PACKAGE_CONFIG).frontend
PACKAGE_CALLS = """\
import axios from "axios";
import http, { create as made } from "axios";
import { got as client } from "got";
import other from "other";

export const bare = () => axios.get("/api/public/invoices/1");
export const post = (token: string) => axios.post(`/api/public/invoices/${token}`, {});
export const renamed = () => http.delete("/api/renamed");
export const request = () => axios.request({ url: "/api/req", method: "put" });
export const named = () => client.get("/accounts");
export const notConfigured = () => other.get("/api/other");
export const otherExport = () => made.get("/api/made");
export const created = axios.create({ baseURL: "/api" });
export const guard = (e: unknown) => axios.isAxiosError(e);
export const own = (axios: { get(url: string): void }) => axios.get("/api/not-the-package");
"""


@pytest.fixture(scope="module")
def packages(scan_tree) -> Scan:
    return scan_tree({"tsconfig.json": TSCONFIG, "app/calls.ts": PACKAGE_CALLS}, PACKAGES)


def _package(scan: Scan, snippet: str) -> list[tuple[str, str]]:
    line = _instance_line(snippet, PACKAGE_CALLS)
    return [(c["method"], render(u)) for c in scan.at("app/calls.ts", line) for u in c["urls"]]


def test_a_package_default_import_is_an_instance_wherever_it_is_imported(packages: Scan) -> None:
    assert _package(packages, "const bare") == [("GET", "/api/public/invoices/1")]
    assert _package(packages, "const post") == [("POST", "/api/public/invoices/{param:token}")]
    assert _package(packages, "const renamed") == [("DELETE", "/api/renamed")]
    assert _package(packages, "const request") == [("PUT", "/api/req")]
    [call] = packages.at("app/calls.ts", _instance_line("const bare", PACKAGE_CALLS))
    assert (call["kind"], call["client"], call["method_source"]) == ("instance", "axios", "member")


def test_a_package_named_export_takes_the_base_path_and_is_labelled_by_its_export(packages: Scan) -> None:
    assert _package(packages, "const named") == [("GET", "/api/accounts")]
    [call] = packages.at("app/calls.ts", _instance_line("const named", PACKAGE_CALLS))
    assert call["client"] == "got"


def test_only_the_configured_package_and_export_are_clients(packages: Scan) -> None:
    for snippet in ("const notConfigured", "const otherExport", "const own"):
        assert packages.at("app/calls.ts", _instance_line(snippet, PACKAGE_CALLS)) == [], snippet
    assert packages.problems == []


def test_a_bare_package_call_matches_its_backend_route(packages: Scan) -> None:
    result = route_map.build_routes(model(live("GET", "/api/public/invoices/{token}", 0)), packages.result)
    assert [(m["backend"]["path"], m["frontend"]["client"]) for m in result["matches"]] == [
        ("/api/public/invoices/{token}", "axios")
    ]


def test_the_default_frontend_walk_covers_only_git_visible_files(tmp_path: Path, typescript_toolchain) -> None:
    call_in = 'import { apiFetch } from "../lib/api";\nexport const go = () => apiFetch("/x");\n'
    write_tree(tmp_path, base_tree(**{"app/seen.ts": call_in, "generated/hidden.ts": call_in}))
    (tmp_path / ".gitignore").write_text("frontend/generated/\n", encoding="utf-8")
    scan = route_map.scan_frontend(tmp_path, FRONTEND, typescript_toolchain)
    assert isinstance(scan, route_map.FrontendScan)
    assert {c["file"] for c in scan.calls} == {"frontend/app/seen.ts"}


def test_a_missing_instance_module_or_export_is_a_diagnostic(scan_tree) -> None:
    base = {"tsconfig.json": TSCONFIG, "app/a.ts": "export const a = 1;\n"}
    scan = scan_tree({**base}, INSTANCES)
    assert [p.file for p in scan.kinds("frontend_client_module_missing")] == ["frontend/lib/http.ts"]
    scan = scan_tree({**base, "lib/http.ts": "export const other = 1;\n"}, INSTANCES)
    [missing] = scan.kinds("frontend_client_module_missing")
    assert (
        missing.message == "frontend/lib/http.ts: client api is not declared in the module, so its calls were not found"
    )


def test_routes_request_carries_the_configured_clients_tsconfig_and_builder_modules(tmp_path: Path) -> None:
    request = route_map.routes_request(tmp_path, INSTANCES, [])
    assert request["repo_root"] == str(tmp_path)
    assert request["frontend_root"] == str(tmp_path / "frontend")
    assert request["tsconfig"] == str(tmp_path / "frontend" / "tsconfig.json")
    assert request["clients"] == [
        {
            "kind": "instance",
            "module": "frontend/lib/http.ts",
            "export": "api",
            "default_method": "GET",
            "base_path": "/api",
        },
        {
            "kind": "global",
            "name": "fetch",
            "url_arg": 0,
            "init_arg": 1,
            "default_method": "GET",
            "same_origin_only": True,
        },
    ]
    assert request["builder_modules"] == ["frontend/lib/http.ts"]
    assert route_map.routes_request(tmp_path, FRONTEND, [])["builder_modules"] == [
        "frontend/lib/api.ts",
        "frontend/lib/utils/download-export.ts",
    ]


def test_a_configured_tsconfig_that_is_missing_is_a_read_failure_naming_it(scan_tree) -> None:
    frontend = parse_config(
        MINIMAL_CONFIG.replace("frontend/tsconfig.json", "frontend/tsconfig.app.json") + CLIENTS_CONFIG
    ).frontend
    scan = scan_tree({"lib/api.ts": CLIENTS_TS, "lib/utils/download-export.ts": DOWNLOAD_TS}, frontend)
    [problem] = [p for p in scan.problems if p.step == "read tsconfig"]
    assert (problem.kind, problem.file) == ("read_failed", "frontend/tsconfig.app.json")


# ── Missing pieces and helper failures: partial results ───────────────────


def test_missing_client_module_keeps_the_other_clients_and_the_backend(scan_tree) -> None:
    tree = {
        "tsconfig.json": TSCONFIG,
        "lib/utils/download-export.ts": "export async function downloadExport(o: { url: string }) {}\n",
        "app/a.tsx": 'import { downloadExport } from "@/lib/utils/download-export";\n'
        'export const go = () => downloadExport({ url: "/a/export" });\n',
    }
    scan = scan_tree(tree)
    missing = scan.kinds("frontend_client_module_missing")
    assert [(p.file, p.is_diagnostic) for p in missing] == [("frontend/lib/api.ts", True)]
    assert "frontend/lib/api.ts" in missing[0].message
    result = route_map.build_routes(model(live("GET", "/a/export", 0), live("GET", "/b", 1)), scan.result)
    assert result["complete"] is False and result["summary"]["coverage_pct"] is None
    assert [m["backend"]["path"] for m in result["matches"]] == ["/a/export"]
    assert [r["path"] for r in result["backend_routes"]] == ["/a/export", "/b"]


def test_missing_frontend_root_returns_the_backend_as_partial(tmp_path: Path, typescript_toolchain) -> None:
    scan = route_map.scan_frontend(tmp_path, FRONTEND, typescript_toolchain)
    assert isinstance(scan, route_map.FrontendUnavailable)
    result = route_map.build_routes(model(live("GET", "/a", 0)), scan)
    assert result["complete"] is False and result["frontend_calls"] == []
    assert [r["path"] for r in result["backend_only"]] == ["/a"]
    diag = result["diagnostics"][0]
    assert (diag["kind"], diag["file"], diag["missing"]) == ("frontend_unavailable", "frontend", "frontend")
    assert diag["fix"] == (
        "run the analysis from a checkout that has frontend/, or correct frontend.root in the config"
    )
    # The fix travels in its own field, so renderers print it exactly once.
    assert diag["message"] == "frontend: frontend root not found"


def test_unavailable_toolchain_names_the_missing_piece_and_the_fix(tmp_path: Path) -> None:
    git_init(tmp_path)
    (tmp_path / "frontend").mkdir()
    problem = ts_bridge.ToolchainProblem(
        "typescript_not_found",
        "tools/code-intel/ts/node_modules/typescript/package.json not found",
        "tools/code-intel/ts/node_modules/typescript/package.json",
        ts_bridge.FRONTEND_TS_FIX,
    )
    toolchain = ts_bridge.Toolchain(None, None, None, None, None, None, problem, (), tmp_path)
    scan = route_map.scan_frontend(tmp_path, FRONTEND, toolchain)
    assert isinstance(scan, route_map.FrontendUnavailable)
    diag = scan.diagnostic()
    assert (
        diag.kind == "frontend_unavailable" and diag.file == "tools/code-intel/ts/node_modules/typescript/package.json"
    )
    assert diag.message == "tools/code-intel/ts/node_modules/typescript/package.json not found"
    assert scan.code == "typescript_not_found"
    row = diag.as_diagnostic()
    assert (row["missing"], row["fix"]) == (
        "tools/code-intel/ts/node_modules/typescript/package.json",
        ts_bridge.FRONTEND_TS_FIX,
    )


def test_helper_failure_is_frontend_unavailable_not_a_crash(tmp_path: Path, typescript_toolchain) -> None:
    git_init(tmp_path)
    (tmp_path / "frontend").mkdir()

    def broken(*_args, **_kwargs):
        raise ts_bridge.HelperError("routes", "exit", "node exited with code 1: boom")

    scan = route_map.scan_frontend(tmp_path, FRONTEND, typescript_toolchain, run_helper=broken)
    assert isinstance(scan, route_map.FrontendUnavailable)
    # The helper lives outside this fixture repo, so it is named by absolute path.
    assert scan.message.startswith(f"{ts_bridge.HELPER.as_posix()}: ")
    assert "step 'exit'" in scan.message and "boom" in scan.message
    assert (scan.code, scan.missing, scan.fix) == ("helper_failed", None, ts_bridge.HELPER_FIX)


def test_routes_protocol_rejects_a_malformed_request(typescript_toolchain) -> None:
    request = {
        "repo_root": "/nowhere",
        "frontend_root": "/nowhere/frontend",
        "tsconfig": None,
        "files": [],
        "clients": [{"kind": "call", "module": "frontend/lib/api.ts"}],
    }
    with pytest.raises(ts_bridge.HelperError) as err:
        ts_bridge.run_helper(typescript_toolchain, "routes", request, timeout_s=60)
    assert err.value.step == "protocol" and "clients[0].export" in err.value.detail


def test_helper_reports_unreadable_paths_with_the_real_reason(tmp_path: Path, typescript_toolchain) -> None:
    """A path the compiler's JSON requests cannot carry (a lone surrogate) and a
    directory named like a source file are each one read failure; the rest of
    the frontend is still scanned."""
    write_tree(tmp_path, base_tree())
    problems: list[Problem] = []
    files = iter_source_files(tmp_path / "frontend", TS_SUFFIXES, tmp_path, problems)
    (tmp_path / "frontend" / "app" / "folder.ts").mkdir(parents=True)
    lone = replace(files[0], path=tmp_path / "frontend" / "app" / "bad\udcff.ts", rel="frontend/app/bad\udcff.ts")
    folder = replace(files[0], path=tmp_path / "frontend" / "app" / "folder.ts", rel="frontend/app/folder.ts")
    scan = route_map.scan_frontend(tmp_path, FRONTEND, typescript_toolchain, [*files, lone, folder])
    assert isinstance(scan, route_map.FrontendScan)
    reads = {p.file: p for p in scan.problems if p.kind == "read_failed"}
    assert set(reads) == {"frontend/app/bad\udcff.ts", "frontend/app/folder.ts"}
    assert "EISDIR" in reads["frontend/app/folder.ts"].message


def test_helper_read_failures_and_syntax_errors_are_named_diagnostics(tmp_path: Path, typescript_toolchain) -> None:
    write_tree(tmp_path, base_tree("export function broken( {\n", **{"app/ok.ts": "export const x = 1;\n"}))
    problems: list[Problem] = []
    files = iter_source_files(tmp_path / "frontend", TS_SUFFIXES, tmp_path, problems)
    ghost = replace(files[0], path=tmp_path / "frontend" / "app" / "ghost.ts", rel="frontend/app/ghost.ts")
    scan = route_map.scan_frontend(tmp_path, FRONTEND, typescript_toolchain, [*files, ghost])
    assert isinstance(scan, route_map.FrontendScan)
    by_kind = {p.kind: p for p in scan.problems}
    read = by_kind["read_failed"]
    assert (read.file, read.step) == ("frontend/app/ghost.ts", "read file")
    assert "frontend/app/ghost.ts: could not read file" in read.message
    syntax = by_kind["typescript_syntax_error"]
    assert (syntax.file, syntax.step) == ("frontend/lib/api.ts", "parse file")
    assert syntax.message.startswith("TypeScript reported") and "frontend/lib/api.ts" in syntax.message
    assert "recovered tree" in syntax.message


def test_a_per_file_extraction_failure_is_analysis_failed_and_keeps_the_other_files(tmp_path: Path) -> None:
    """The helper catches an exception while reading one file's calls and reports
    it under step "extract calls"; that file's calls are lost, the rest stand."""
    page = 'import { apiJson } from "../lib/api";\nexport const a = () => apiJson("/a");\n'
    write_tree(tmp_path, base_tree(**{"app/page.ts": page}))
    files = iter_source_files(tmp_path / "frontend", TS_SUFFIXES, tmp_path, [])
    broken = str(tmp_path / "frontend" / "app" / "page.ts")

    def helper(_toolchain, mode, request, *, timeout_s):
        assert mode == "routes" and request["files"]
        return {
            "protocol": 1,
            "mode": "routes",
            "typescript_version": "7.1.0-beta",
            "calls": [],
            "unresolved": [],
            "limitations": [],
            "missing_client_modules": [],
            "files_failed": [
                {
                    "file": broken,
                    "step": "extract calls",
                    "message": f"{broken}: could not extract calls (RangeError: x)",
                }
            ],
            "timings_ms": {},
        }

    toolchain = ts_bridge.Toolchain(
        "/node", "v24.0.0", Path("/ts"), "7.1.0-beta", "env", "7.1.0-beta", None, (), tmp_path
    )
    scan = route_map.scan_frontend(tmp_path, FRONTEND, toolchain, files, run_helper=helper)
    assert isinstance(scan, route_map.FrontendScan)
    [problem] = scan.problems
    assert (problem.kind, problem.category, problem.step, problem.file) == (
        "analysis_failed",
        "content_lost",
        "extract calls",
        "frontend/app/page.ts",
    )
    assert problem.message == "frontend/app/page.ts: could not extract calls (RangeError: x)"
    result = route_map.build_routes(model(live("GET", "/a", 0)), scan)
    assert result["complete"] is False and result["summary"]["coverage_pct"] is None


# ── CLI ───────────────────────────────────────────────────────────────────


def _dump_routes(*args: str | Path) -> int:
    return cli.main(["dump", "routes", *map(str, args)])


def test_cli_json_exit_code_and_stderr_name_each_lost_piece(monkeypatch, capsys, tmp_path: Path) -> None:
    lost = diagnostic(
        "read_failed",
        stage="frontend routes",
        step="read file",
        file="frontend/app/x.ts",
        exc=PermissionError(13, "Permission denied"),
    )
    monkeypatch.setattr(
        route_map,
        "compute_routes",
        lambda repo_root, config: route_map.build_routes(model(live("GET", "/a", 0), diagnostics=(lost,)), scan_of()),
    )
    code = _dump_routes("--json", "--repo-root", make_repo(tmp_path))
    out, err = capsys.readouterr()
    assert code == 2
    assert json.loads(out)["complete"] is False
    assert "frontend/app/x.ts: could not read file (PermissionError" in err
    assert "could not parse" not in err


def test_cli_refuses_a_too_old_interpreter(monkeypatch, capsys, tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "backend" / ".python-version").write_text("99.0\n", encoding="utf-8")
    monkeypatch.setattr(route_map, "compute_routes", lambda *_: pytest.fail("analysis must not run"))
    assert _dump_routes("--json", "--repo-root", repo) == 1
    assert "older than the repo minimum 99.0" in capsys.readouterr().err


def test_cli_names_a_checkout_git_cannot_list(capsys, tmp_path: Path, typescript_toolchain) -> None:
    repo = make_repo(tmp_path, MINIMAL_CONFIG + API_CLIENTS_CONFIG)
    (repo / "backend" / "service_backend" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    assert _dump_routes("--json", "--repo-root", repo) == 2
    assert "git" in capsys.readouterr().err


def test_cli_text_report_leads_with_banners(monkeypatch, capsys, tmp_path: Path) -> None:
    lim = limitation("opaque_router_call", stage="backend", file="backend/main.py", line=764, message="app escapes")
    monkeypatch.setattr(
        route_map,
        "compute_routes",
        lambda repo_root, config: route_map.build_routes(
            model(live("GET", "/a", 0), limitations=(lim,)),
            scan_of(call([{"text": "/a"}], function="getA", def_line=3, line=4)),
        ),
    )
    assert _dump_routes("--repo-root", make_repo(tmp_path)) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("ⓘ 1 analysis limitation")
    assert "getA()  [frontend/lib/api.ts:def 3 · call 4]" in out
    assert "Coverage:        100.0% of backend routes" in out


def test_compute_routes_overlaps_the_scan_with_the_backend_build(tmp_path: Path, typescript_toolchain) -> None:
    write_tree(tmp_path, base_tree('export async function getA() { return apiJson<X>("/a"); }\n'))
    result = route_map.compute_routes(
        tmp_path,
        CONFIG,
        toolchain=typescript_toolchain,
        model_builder=lambda root, backend: model(live("GET", "/a", 0)),
    )
    assert result["complete"] is True
    assert [(m["frontend"]["function"], m["backend"]["path"]) for m in result["matches"]] == [("getA", "/a")]


def test_cli_matches_a_configured_repository_from_any_cwd(typescript_toolchain, tmp_path: Path) -> None:
    repo = git_init(make_repo(tmp_path, MINIMAL_CONFIG + API_CLIENTS_CONFIG))
    app = repo / "backend" / "service_backend" / "main.py"
    app.write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/a')\ndef get_a(): ...\n\n"
        "@app.get('/unused')\ndef get_unused(): ...\n",
        encoding="utf-8",
    )
    (repo / "frontend" / "lib" / "api.ts").write_text(
        CLIENTS_TS + 'export async function getA() { return apiJson<X>("/a"); }\n', encoding="utf-8"
    )
    (repo / "frontend" / "tsconfig.json").write_text(TSCONFIG, encoding="utf-8")
    env = {**os.environ, ts_bridge.TS_DIR_ENV: str(typescript_toolchain.typescript_dir)}
    proc = subprocess.run(
        [sys.executable, "-m", "code_intel.cli", "dump", "routes", "--json", "--repo-root", str(repo)],
        cwd="/",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == 2 and payload["complete"] is True
    assert [(m["backend"]["path"], m["frontend"]["function"]) for m in payload["matches"]] == [("/a", "getA")]
    assert [r["path"] for r in payload["backend_only"]] == ["/unused"]


def test_matching_uses_the_include_snapshot_reachability(tmp_path: Path) -> None:
    """A route registered on an included router by a function nothing calls at
    import is not served through that include, so a call to it is frontend-only;
    a lambda the analysis cannot order keeps its route, listed as deferred and reported."""
    import textwrap

    from code_intel.router_graph import build_backend_model

    sources = {
        "pkg/__init__.py": "",
        "pkg/main.py": """
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): ...
            child.add_api_route("/early", handler)
            hook = lambda: child.add_api_route("/hooked", handler)
            app = FastAPI()
            app.include_router(child, prefix="/c")
            def later():
                child.add_api_route("/late", handler)
            """,
    }
    for rel, source in sources.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    backend = build_backend_model(tmp_path, "pkg", "pkg.main:app", tmp_path, include_model=_COPY)
    scan = scan_of(
        call([{"text": "/c/early"}], line=1),
        call([{"text": "/c/hooked"}], line=2),
        call([{"text": "/c/late"}], line=3),
    )
    result = route_map.build_routes(backend, scan)
    assert [(r["path"], r["mount"]) for r in result["backend_routes"]] == [
        ("/c/early", "static"),
        ("/c/hooked", "deferred"),
    ]
    assert [m["backend"]["path"] for m in result["matches"]] == ["/c/early", "/c/hooked"]
    assert [r["path"] for r in result["frontend_only"]] == ["/c/late"]
    messages = [
        lim["message"] for lim in result["analysis_limitations"] if lim["kind"] == "deferred_route_registration"
    ]
    assert any("child.add_api_route(...) runs inside later()" in m and "not served" in m for m in messages)
    assert any("child.add_api_route(...)" in m and "lazily" in m for m in messages)


@pytest.mark.parametrize(
    "runs_it",
    [
        "go = later\ngo()\n",
        "def call(f):\n    f()\ncall(later)\n",
        "HOOKS = [later]\nfor hook in HOOKS:\n    hook()\n",
        'globals()["later"]()\n',
    ],
)
def test_a_function_run_at_import_other_than_by_a_followed_call_keeps_its_route(tmp_path: Path, runs_it: str) -> None:
    """Code the analysis does not follow may run the function at import before
    the include, so its route stays listed (deferred) and matched, never dropped."""
    source = (
        "from fastapi import APIRouter, FastAPI\n"
        "child = APIRouter()\n"
        "def handler(): ...\n"
        "def later():\n"
        '    child.add_api_route("/late", handler)\n'
        f"{runs_it}"
        "app = FastAPI()\n"
        'app.include_router(child, prefix="/c")\n'
    )
    from code_intel.router_graph import build_backend_model

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "main.py").write_text(source, encoding="utf-8")
    backend = build_backend_model(tmp_path, "pkg", "pkg.main:app", tmp_path, include_model=_COPY)
    result = route_map.build_routes(backend, scan_of(call([{"text": "/c/late"}])))
    assert [(r["path"], r["mount"]) for r in result["backend_routes"]] == [("/c/late", "deferred")]
    assert [m["backend"]["path"] for m in result["matches"]] == ["/c/late"]
    assert result["frontend_only"] == []
    (lim,) = [lim for lim in result["analysis_limitations"] if lim["kind"] == "deferred_route_registration"]
    assert "whether and when it runs is unknown" in lim["message"]


def test_a_methods_none_route_matches_only_get(tmp_path: Path) -> None:
    """``methods=None`` is GET at runtime, so a POST call to the path is frontend-only."""
    from code_intel.router_graph import build_backend_model

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\ndef h(): ...\napp.add_api_route('/x', h, methods=None)\n",
        encoding="utf-8",
    )
    backend = build_backend_model(tmp_path, "pkg", "pkg.main:app", tmp_path, include_model=_COPY)
    scan = scan_of(call([{"text": "/x"}], line=1), call([{"text": "/x"}], line=2, method="POST"))
    result = route_map.build_routes(backend, scan)
    assert [(m["backend"]["method"], m["frontend"]["method"]) for m in result["matches"]] == [("GET", "GET")]
    assert [(r["method"], r["path"]) for r in result["frontend_only"]] == [("POST", "/x")]


def test_report_names_the_summary_figures_findings_can_move() -> None:
    import io

    lim = limitation("asgi_mount", stage="build router graph", file="m.py", line=2, message="m")
    scan = scan_of(call([{"text": "/a"}], function="fa"))
    result = route_map.build_routes(model(live("GET", "/a", 0), live("GET", "/b", 1), limitations=(lim,)), scan)
    out = io.StringIO()
    route_map.print_report(result, out=out)
    assert (
        out.getvalue()
        .rstrip()
        .endswith(
            "  Bounds:          backend_routes at least, matched_routes not bounded, matched_calls at most, "
            "match_rows not bounded, backend_only at least, frontend_only at least, coverage_pct not bounded "
            "(the findings above can move them)"
        )
    )
    clean = io.StringIO()
    route_map.print_report(route_map.build_routes(model(live("GET", "/a", 0)), scan), out=clean)
    assert "Bounds:" not in clean.getvalue()
