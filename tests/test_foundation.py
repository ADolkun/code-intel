"""Tests for the shared foundations: file walking, problems, route paths, lock pins,
the result shapes and the interpreter floor."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from code_intel import backend_model, code_paths, problems, result_types, route_paths, toolchain_pins

# ── code_paths ───────────────────────────────────────────────────────────


def _write(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_iter_source_files_filters_sorts_and_skips(tmp_path: Path) -> None:
    root = tmp_path / "frontend"
    _write(root / "b.tsx")
    _write(root / "a" / "z.ts")
    _write(root / "a" / "y.jsx")
    _write(root / "a" / "notes.md")
    for skipped in ("node_modules", ".next", "dist", ".git", "__pycache__", ".venv", "venv", ".hidden"):
        _write(root / skipped / "skip.ts")
    found: list[problems.Problem] = []
    files = code_paths.iter_source_files(root, code_paths.TS_SUFFIXES, tmp_path, found)
    assert [f.rel for f in files] == ["frontend/a/y.jsx", "frontend/a/z.ts", "frontend/b.tsx"]
    assert found == []
    first = files[0]
    assert first.path == root / "a" / "y.jsx"
    st = first.path.stat()
    assert first.stamp == code_paths.FileStamp(st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)


def test_generated_output_is_skipped_only_directly_under_its_parent(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    for name in sorted(code_paths.FRONTEND_GENERATED_DIRS):
        _write(frontend / name / "skip.ts")
    _write(frontend / "app" / "build" / "page.tsx")
    _write(frontend / "app" / "coverage" / "x.ts")
    backend = tmp_path / "backend"
    _write(backend / "coverage" / "report.py")
    _write(backend / "build" / "steps.py")

    def rels(root: Path, suffixes: tuple[str, ...], **kw: object) -> list[str]:
        found: list[problems.Problem] = []
        files = code_paths.iter_source_files(root, suffixes, tmp_path, found, **kw)  # type: ignore[arg-type]
        assert found == []
        return [f.rel for f in files]

    assert rels(frontend, code_paths.TS_SUFFIXES, generated_parent=frontend) == [
        "frontend/app/build/page.tsx",
        "frontend/app/coverage/x.ts",
    ]
    # The default walk, used for Python, keeps every generated-looking name.
    assert rels(backend, (".py",)) == ["backend/build/steps.py", "backend/coverage/report.py"]
    assert len(rels(frontend, code_paths.TS_SUFFIXES)) == len(code_paths.FRONTEND_GENERATED_DIRS) + 2
    # A walk from the repo root skips them under frontend/ only.
    assert rels(tmp_path, (".py", ".ts", ".tsx"), generated_parent=frontend) == [
        "backend/build/steps.py",
        "backend/coverage/report.py",
        "frontend/app/build/page.tsx",
        "frontend/app/coverage/x.ts",
    ]


def test_iter_source_files_missing_root_names_directory_and_step(tmp_path: Path) -> None:
    found: list[problems.Problem] = []
    assert code_paths.iter_source_files(tmp_path / "gone", (".py",), tmp_path, found, stage="di_map") == []
    [problem] = found
    assert problem.kind == "list_failed"
    assert problem.category == "content_lost"
    assert problem.step == "list directory"
    assert problem.stage == "di_map"
    assert problem.file == "gone"
    assert problem.exception == "FileNotFoundError"
    assert problem.message.startswith("gone: could not list directory (FileNotFoundError:")


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as a non-root user")
def test_iter_source_files_unreadable_directory_is_a_diagnostic(tmp_path: Path) -> None:
    _write(tmp_path / "ok.py")
    locked = tmp_path / "locked"
    _write(locked / "hidden.py")
    locked.chmod(0)
    try:
        found: list[problems.Problem] = []
        files = code_paths.iter_source_files(tmp_path, (".py",), tmp_path, found)
    finally:
        locked.chmod(0o755)
    assert [f.rel for f in files] == ["ok.py"]
    assert [(p.kind, p.step, p.file) for p in found] == [("list_failed", "list directory", "locked")]
    assert "PermissionError" in found[0].message


def test_iter_source_files_stat_failure_names_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "a.py")
    _write(tmp_path / "b.py")

    def failing_stat(entry: os.DirEntry) -> os.stat_result:
        if entry.name == "a.py":
            raise PermissionError(13, "Permission denied")
        return entry.stat(follow_symlinks=False)

    monkeypatch.setattr(code_paths, "_stat", failing_stat)
    found: list[problems.Problem] = []
    files = code_paths.iter_source_files(tmp_path, (".py",), tmp_path, found)
    assert [f.rel for f in files] == ["b.py"]
    assert [(p.kind, p.step, p.file) for p in found] == [("stat_failed", "stat file", "a.py")]
    assert found[0].message.startswith("a.py: could not stat file (PermissionError:")


@pytest.mark.parametrize(
    "rel,expected",
    [
        ("frontend/components/__tests__/Table.tsx", True),
        ("frontend/__mocks__/api.ts", True),
        ("frontend/test/setup.ts", True),
        ("frontend/e2e/login.ts", True),
        ("frontend/components/Table.test.tsx", True),
        ("frontend/lib/api.spec.mts", True),
        ("frontend/lib/a.test.cjs", True),
        ("backend/tests/routes/x.py", True),
        ("backend/service_backend/test_utils.py", True),
        ("backend/service_backend/x_test.py", True),
        ("backend/conftest.py", True),
        ("frontend\\components\\__tests__\\Table.tsx", True),
        ("frontend/components/Table.tsx", False),
        ("frontend/lib/testing-utils.ts", False),
        ("frontend/lib/latest.ts", False),
        ("backend/service_backend/testing.py", False),
        ("backend/service_backend/contest.py", False),
        ("tests.py", False),
    ],
)
def test_is_test_path_is_one_rule_for_python_and_js(rel: str, expected: bool) -> None:
    assert code_paths.is_test_path(rel) is expected


def test_split_source_lines_matches_ast_line_numbers() -> None:
    source = "a = 1\r\nb = '\x0c\x85\u2028'\rc = 3\nd = 4\n"
    lines = code_paths.split_source_lines(source)
    tree = ast.parse(source)
    for node in tree.body:
        assert isinstance(node, ast.Assign)
        target = node.targets[0]
        assert isinstance(target, ast.Name)
        assert lines[node.lineno - 1].startswith(f"{target.id} = ")
    assert lines == ["a = 1", "b = '\x0c\x85\u2028'", "c = 3", "d = 4", ""]


def test_read_python_source_decodes_like_the_interpreter(tmp_path: Path) -> None:
    utf8 = tmp_path / "utf8.py"
    utf8.write_bytes("x = 'é'\n".encode())
    bom = tmp_path / "bom.py"
    bom.write_bytes(b"\xef\xbb\xbfx = 1\n")
    latin = tmp_path / "latin.py"
    latin.write_bytes(b"# -*- coding: latin-1 -*-\nx = '\xe9'\n")
    assert code_paths.read_python_source(utf8) == ("x = 'é'\n", None)
    assert code_paths.read_python_source(bom) == ("x = 1\n", None)
    text, problem = code_paths.read_python_source(latin)
    assert problem is None and text is not None and "'é'" in text


def test_read_python_source_names_read_and_decode_steps(tmp_path: Path) -> None:
    text, problem = code_paths.read_python_source(tmp_path / "missing.py", "backend/missing.py", stage="symbols")
    assert text is None and problem is not None
    assert (problem.kind, problem.step, problem.file, problem.stage) == (
        "read_failed",
        "read file",
        "backend/missing.py",
        "symbols",
    )
    assert problem.message.startswith("backend/missing.py: could not read file (FileNotFoundError:")

    bad = tmp_path / "bad.py"
    bad.write_bytes(b"x = 1\ny = 2\nz = '\xff\xfe'\n")
    text, problem = code_paths.read_python_source(bad, "bad.py")
    assert text is None and problem is not None
    assert (problem.kind, problem.step, problem.exception) == ("decode_failed", "decode file", "UnicodeDecodeError")

    # Bad bytes in the first two lines fail encoding detection, which raises SyntaxError;
    # that is a decode failure, never an interpreter-version hint.
    early = tmp_path / "early.py"
    early.write_bytes(b"x = '\xff\xfe'\n")
    text, problem = code_paths.read_python_source(early, "early.py")
    assert problem is not None
    assert (problem.kind, problem.step, problem.exception) == ("decode_failed", "decode file", "SyntaxError")
    assert problem.likely_cause is None

    cookie = tmp_path / "cookie.py"
    cookie.write_bytes(b"# coding: no-such-codec\nx = 1\n")
    text, problem = code_paths.read_python_source(cookie, "cookie.py")
    assert text is None and problem is not None
    assert (problem.kind, problem.step) == ("decode_failed", "decode file")


def test_is_racy_uses_the_two_second_window() -> None:
    stamp = code_paths.FileStamp(mtime_ns=10_000_000_000, size=1, ino=1, ctime_ns=10_000_000_000)
    assert code_paths.is_racy(stamp, now_ns=11_999_999_999)
    assert not code_paths.is_racy(stamp, now_ns=12_000_000_000)
    assert code_paths.is_racy(stamp, now_ns=9_000_000_000)  # mtime in the future
    assert not code_paths.is_racy(stamp, now_ns=10_500_000_000, window_ns=100)


def test_is_racy_measures_from_the_newer_of_mtime_and_ctime() -> None:
    """A file whose mtime was set back is still as recent as its ctime."""
    restored = code_paths.FileStamp(mtime_ns=1_000_000_000, size=1, ino=1, ctime_ns=10_000_000_000)
    assert code_paths.is_racy(restored, now_ns=11_999_999_999)
    assert not code_paths.is_racy(restored, now_ns=12_000_000_000)
    future_mtime = code_paths.FileStamp(mtime_ns=10_000_000_000, size=1, ino=1, ctime_ns=1_000_000_000)
    assert code_paths.is_racy(future_mtime, now_ns=11_999_999_999)


def test_a_same_size_rewrite_with_its_mtime_restored_changes_the_stamp(
    tmp_path: Path, rewrite_keeping_mtime: Callable[[Path, str], None]
) -> None:
    """ctime is set by the kernel on every write and cannot be set back, so the
    stamp changes even though mtime, size and inode all match again."""
    path = _write(tmp_path / "a.py", "x = 1\n")
    stamp = code_paths.FileStamp.from_stat(path.stat())
    rewrite_keeping_mtime(path, "x = 2\n")
    assert code_paths.FileStamp.from_stat(path.stat()) != stamp


def test_is_under_compares_whole_segments() -> None:
    assert code_paths.is_under("vendor/sub", "vendor/sub")
    assert code_paths.is_under("vendor/sub/a.md", "vendor/sub")
    assert code_paths.is_under("vendor/sub/a.md", "vendor/sub/")
    assert not code_paths.is_under("vendor/subway.md", "vendor/sub")
    assert not code_paths.is_under("vendor", "vendor/sub")


def test_content_digest_and_fingerprint(tmp_path: Path) -> None:
    path = _write(tmp_path / "a.py", "x = 1\n")
    assert code_paths.content_digest(path) == hashlib.sha256(b"x = 1\n").hexdigest()
    one = code_paths.SourceFile(path, "a.py", code_paths.FileStamp(1, 2, 3, 7))
    two = code_paths.SourceFile(path, "b.py", code_paths.FileStamp(4, 5, 6, 8))
    assert code_paths.fingerprint([one, two]) == code_paths.fingerprint([two, one])
    moved = code_paths.SourceFile(path, "a.py", code_paths.FileStamp(1, 2, 4, 7))
    assert code_paths.fingerprint([one, two]) != code_paths.fingerprint([moved, two])
    rewritten = code_paths.SourceFile(path, "a.py", code_paths.FileStamp(1, 2, 3, 9))
    assert code_paths.fingerprint([one, two]) != code_paths.fingerprint([rewritten, two])


def test_rel_posix_inside_and_outside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    assert code_paths.rel_posix(repo / "frontend" / "lib" / "api.ts", repo) == "frontend/lib/api.ts"
    outside = tmp_path / "elsewhere" / "x.ts"
    assert code_paths.rel_posix(outside, repo) == outside.as_posix()


# ── problems ─────────────────────────────────────────────────────────────


EXPECTED_LIMITATIONS = {
    "unmounted_router",
    "unresolved_include",
    "unresolved_include_prefix",
    "unresolved_dependencies",
    "unresolved_route_path",
    "unresolved_dependency",
    "shared_dependency_id",
    "opaque_router_call",
    "opaque_router_change",
    "asgi_mount",
    "router_include_cycle",
    "unsupported_route_registration",
    "conditional_router_mount",
    "deferred_router_mount",
    "deferred_route_registration",
    "unresolved_route_router",
    "unresolved_route_prefix",
    "unresolved_mount_path",
    "mount_shadows_route",
    "unresolved_route_methods",
    "unresolved_http_method",
    "url_variants_truncated",
    "unresolved_prop_value",
    "unimported_router_change",
    "unsupported_router_argument",
    "custom_router_class",
    "unresolved_redirect_slashes",
    "opaque_decorator",
    "recursive_router_call",
    "router_call_limit",
    "conditional_import",
    "submodule_missing",
    "frontend_definitions_unavailable",
    "frontend_pages_unavailable",
}
EXPECTED_DIAGNOSTICS = {
    "read_failed",
    "decode_failed",
    "parse_failed",
    "list_failed",
    "stat_failed",
    "analysis_failed",
    "unresolved_app",
    "typescript_syntax_error",
    "frontend_unavailable",
    "frontend_client_module_missing",
    "unparsed_frontend_callsite",
    "unresolved_frontend_path",
}
REMOVED_KINDS = {"duplicate_router_mount", "ambiguous_router_binding", "unresolved_guarded_mount", "invalid_annotation"}


def test_problem_registries_hold_exactly_the_documented_kinds() -> None:
    assert set(problems.LIMITATION_KINDS) == EXPECTED_LIMITATIONS
    assert set(problems.DIAGNOSTIC_KINDS) == EXPECTED_DIAGNOSTICS
    assert not (set(problems.LIMITATION_KINDS) | set(problems.DIAGNOSTIC_KINDS)) & REMOVED_KINDS
    assert not set(problems.LIMITATION_KINDS) & set(problems.DIAGNOSTIC_KINDS)
    for kind, (category, description) in problems.DIAGNOSTIC_KINDS.items():
        assert category in ("content_lost", "resolver"), kind
        assert description, kind
    assert all(problems.LIMITATION_KINDS.values())


def test_backend_route_gap_kinds_are_the_keys_of_the_route_gap_effects() -> None:
    queries = pytest.importorskip("code_intel.queries")

    assert problems.BACKEND_ROUTE_GAP_KINDS == frozenset(queries.BACKEND_ROUTE_GAPS)
    assert queries.BACKEND_ROUTE_GAP_KINDS is problems.BACKEND_ROUTE_GAP_KINDS


def test_unregistered_kinds_are_rejected() -> None:
    with pytest.raises(ValueError, match="'made_up' is not registered"):
        problems.diagnostic("made_up", stage="s", step="read file", file="f", message="m")
    with pytest.raises(ValueError, match="'made_up' is not registered"):
        problems.limitation("made_up", stage="s", file="f", line=1, message="m")


def test_diagnostic_and_limitation_json_shapes() -> None:
    exc = PermissionError(13, "Permission denied")
    diag = problems.diagnostic("read_failed", stage="route_map", step="read file", file="frontend/lib/api.ts", exc=exc)
    assert diag.as_dict() == {
        "kind": "read_failed",
        "category": "content_lost",
        "stage": "route_map",
        "step": "read file",
        "file": "frontend/lib/api.ts",
        "line": None,
        "message": "frontend/lib/api.ts: could not read file (PermissionError: [Errno 13] Permission denied)",
        "exception": "PermissionError",
    }
    lim = problems.limitation(
        "opaque_router_call", stage="router_graph", file="backend/x.py", line=764, message="app escapes"
    )
    assert lim.as_dict() == {
        "kind": "opaque_router_call",
        "stage": "router_graph",
        "file": "backend/x.py",
        "line": 764,
        "message": "app escapes",
    }
    assert diag.is_diagnostic and not lim.is_diagnostic
    assert problems.partition([lim, diag, lim]) == ([diag], [lim, lim])


def test_diagnostic_takes_line_from_exception_and_needs_a_message() -> None:
    exc = SyntaxError("bad", ("x.py", 7, 1, "x"))
    diag = problems.diagnostic("parse_failed", stage="s", step="parse file", file="x.py", exc=exc)
    assert diag.line == 7
    assert diag.exception == "SyntaxError"
    with pytest.raises(ValueError, match="needs a message or an exception"):
        problems.diagnostic("parse_failed", stage="s", step="parse file", file="x.py")


def test_syntax_error_on_old_interpreter_names_likely_cause() -> None:
    diag = problems.diagnostic(
        "parse_failed",
        stage="s",
        step="parse file",
        file="x.py",
        exc=SyntaxError("invalid syntax"),
        python_floor=(3, 99),
    )
    assert diag.likely_cause is not None
    assert "interpreter too old" in diag.likely_cause and "3.99" in diag.likely_cause
    assert diag.as_diagnostic()["likely_cause"] == diag.likely_cause
    other = problems.diagnostic("read_failed", stage="s", step="read file", file="x.py", exc=OSError("boom"))
    assert other.likely_cause is None and "likely_cause" not in other.as_dict()
    unconstrained = problems.diagnostic(
        "parse_failed", stage="s", step="parse file", file="x.py", exc=SyntaxError("invalid syntax")
    )
    assert unconstrained.likely_cause is None


def test_incomplete_banner_groups_by_step_and_category() -> None:
    def read(name: str) -> problems.Problem:
        return problems.diagnostic("read_failed", stage="s", step="read file", file=name, exc=OSError("x"))

    listed = problems.diagnostic("list_failed", stage="s", step="list directory", file="d", exc=OSError("x"))
    banner = problems.incomplete_banner([read("a"), listed, read("b")])
    assert banner == (
        "⚠ INCOMPLETE — 2 files could not be read, 1 directory could not be listed; counts may be undercounts."
    )
    resolver = problems.diagnostic(
        "unparsed_frontend_callsite", stage="routes", step="extract calls", file="f.ts", message="m"
    )
    assert problems.incomplete_banner([resolver]) == (
        "⚠ INCOMPLETE — 1 API client reference could not be read; results may be imprecise."
    )
    # JSON dicts work as well as Problem objects.
    assert problems.incomplete_banner([read("a").as_dict()]).startswith("⚠ INCOMPLETE — 1 file could not be read;")


# Steps whose diagnostics always carry a kind with its own banner phrase.
_KIND_PHRASED_STEPS = {
    "analyze frontend": "frontend_unavailable",
    "resolve client": "frontend_client_module_missing",
    "resolve app": "unresolved_app",
    "evaluate url": "unresolved_frontend_path",
    "read call": "unparsed_frontend_callsite",
}
_STEP_LITERAL = re.compile(r"""\bstep\s*(?:=|:)\s*["']([^"']+)["']""")


def test_every_emitted_diagnostic_step_has_a_banner_phrase() -> None:
    """A step without a phrase would read "1 failure at step 'x'" in the banner."""
    tool_dir = Path(problems.__file__).resolve().parent
    producers = [
        p
        for p in (*tool_dir.glob("*.py"), *(tool_dir / "ts").glob("*.cjs"))
        if not p.name.startswith(("test_", "conftest", "tool_fixtures", "analysis_runtime"))
    ]
    steps = {m for p in producers for m in _STEP_LITERAL.findall(p.read_text(encoding="utf-8"))}
    route_map = pytest.importorskip("code_intel.route_map")

    steps |= {step for step, _ in route_map._UNRESOLVED_TEXT.values()}
    assert {"read file", "extract calls", "read tsconfig", "extract facts", "index symbols"} <= steps
    unphrased = sorted(s for s in steps if s not in problems._STEP_PHRASES and s not in _KIND_PHRASED_STEPS)
    assert unphrased == []
    assert all(kind in problems._KIND_PHRASES for kind in _KIND_PHRASED_STEPS.values())


def test_limitations_banner_lists_each_with_location() -> None:
    lim = problems.limitation("asgi_mount", stage="g", file="backend/main.py", line=12, message="StaticFiles")
    banner = problems.limitations_banner([lim, lim.as_dict()])
    lines = banner.splitlines()
    assert lines[0].startswith("ⓘ 2 analysis limitation(s)")
    assert lines[1] == "  • [asgi_mount] backend/main.py:12: StaticFiles"
    assert lines[2] == lines[1]


@pytest.mark.parametrize(
    "running,minimum,expected",
    [
        ((3, 14), (3, 14), True),
        ((3, 15), (3, 14), True),
        ((4, 0), (3, 14), True),
        ((3, 12), (3, 14), False),
        ((3, 13), (3, 14), False),
    ],
)
def test_runtime_is_compatible_uses_ge(running, minimum, expected) -> None:
    assert problems.runtime_is_compatible(minimum, running) is expected


def test_min_python_version_parses_and_falls_back(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.write_text("# pinned\n3.14.3\n")
    assert problems.min_python_version(good) == (3, 14)
    assert problems.min_python_version(tmp_path / "missing") == problems._FALLBACK_MIN_PYTHON
    junk = tmp_path / "junk"
    junk.write_text("not-a-version\n")
    assert problems.min_python_version(junk) == problems._FALLBACK_MIN_PYTHON
    assert problems.min_python_version(None) == problems._FALLBACK_MIN_PYTHON


# ── route_paths ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,key",
    [
        ("/articles/{article_id}", "/articles/{}"),
        ("/articles/{id:int}/files", "/articles/{}/files"),
        ("/files/{file_path:path}", "/files/{}"),
        ("/search?q=1", "/search"),
        ("/a/", "/a/"),
        ("/", "/"),
    ],
)
def test_path_key(path: str, key: str) -> None:
    assert route_paths.path_key(path) == key


def test_split_segments_and_param_re() -> None:
    assert route_paths.split_segments("/a/{b}") == ["a", "{b}"]
    assert route_paths.split_segments("/a/?x=1") == ["a", ""]
    assert route_paths.split_segments("/") == [""]
    match = route_paths.PARAM_RE.fullmatch("{file_path:path}")
    assert match is not None and match.groups() == ("file_path", "path")
    assert route_paths.PARAM_RE.fullmatch("{1bad}") is None


# ── backend_model / result_types ─────────────────────────────────────────


def test_backend_model_shapes_are_frozen_and_constructible() -> None:
    call = ast.parse("router.get('/x')").body[0]
    assert isinstance(call, ast.Expr)
    endpoint = backend_model.Located("pkg.mod", call.value, at=0)
    assert endpoint.line == 1
    layer = backend_model.DepLayer("router", "pkg.mod:router", ())
    route = backend_model.LiveRoute(
        method="GET",
        path="/x",
        kind="http",
        handler="get_x",
        handler_id="pkg.mod:get_x",
        file="pkg/mod.py",
        line=2,
        registered_file="pkg/mod.py",
        registered_line=1,
        chain=("pkg.main:app", "pkg.mod:router"),
        mount="static",
        layers=(layer,),
        order=0,
        endpoint=endpoint,
    )
    with pytest.raises(AttributeError):
        route.path = "/y"  # type: ignore[misc]
    target = backend_model.DepTarget("pkg.deps:get_db", "pkg.deps", "get_db", "get_db", "function", "pkg/deps.py", 3)
    use = backend_model.DepUse(target, "get_db", "direct", "db", None, "Depends", "pkg/mod.py", 2)
    assert use.target.id == "pkg.deps:get_db"
    unknown = backend_model.Unknown("cycle", "PREFIX")
    edge = backend_model.IncludeEdge("a", "b", unknown, (), "pkg/main.py", 5, 3, "conditional")
    assert (edge.seq, edge.mount) == (3, "conditional")


def test_name_resolver_protocol_is_structural() -> None:
    class Fake:
        def resolve_name(self, module: str, name: str, at: int | None) -> list[backend_model.Target]:
            return [backend_model.External(f"{module}.{name}")]

        def resolve_expr(self, loc: backend_model.Located) -> list[backend_model.Target]:
            return [backend_model.Value(loc)]

        def string(self, loc: backend_model.Located) -> str | backend_model.Unknown:
            return backend_model.Unknown("depth", "x")

        def sequence(self, loc: backend_model.Located) -> tuple[backend_model.Located, ...] | backend_model.Unknown:
            return ()

    resolver: backend_model.NameResolver = Fake()
    assert resolver.resolve_name("fastapi", "Depends", None) == [backend_model.External("fastapi.Depends")]


def test_client_spec_request_shape() -> None:
    call = result_types.ClientSpec(
        kind="call", module="src/lib/api.ts", export="apiJson", url_arg=0, init_arg=1, default_method="GET"
    )
    assert call.as_request() == {
        "kind": "call",
        "module": "src/lib/api.ts",
        "export": "apiJson",
        "url_arg": 0,
        "init_arg": 1,
        "default_method": "GET",
    }
    instance = result_types.ClientSpec(kind="instance", module="src/lib/api.ts", export="api", base_path="/api")
    assert instance.as_request() == {
        "kind": "instance",
        "module": "src/lib/api.ts",
        "export": "api",
        "default_method": "GET",
        "base_path": "/api",
    }
    fetch = result_types.ClientSpec(kind="global", name="fetch", url_arg=0, init_arg=1, same_origin_only=True)
    assert fetch.as_request() == {
        "kind": "global",
        "name": "fetch",
        "url_arg": 0,
        "init_arg": 1,
        "default_method": "GET",
        "same_origin_only": True,
    }


def test_error_fields_cover_every_error_code() -> None:
    assert set(result_types.ERROR_FIELDS) == {
        "invalid_argument",
        "invalid_root",
        "runtime_incompatible",
        "analysis_failed",
        "frontend_unavailable",
        "internal_error",
    }


# ── toolchain_pins ───────────────────────────────────────────────────────


def test_backend_pins_reads_both_packages(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n[[package]]\nname = "fastapi"\nversion = "0.162.0"\n'
        '[[package]]\nname = "starlette"\nversion = "1.10.0"\n',
        encoding="utf-8",
    )
    assert toolchain_pins.backend_pins(lock) == {"fastapi": "0.162.0", "starlette": "1.10.0"}


def test_a_package_locked_at_two_versions_is_refused(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n[[package]]\nname = "fastapi"\nversion = "0.162.0"\n'
        '[[package]]\nname = "fastapi"\nversion = "0.136.3"\n',
        encoding="utf-8",
    )
    with pytest.raises(toolchain_pins.PinError, match="several versions"):
        toolchain_pins.locked_versions(lock, ("fastapi",))


def test_backend_pins_reports_missing_and_unreadable(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text('version = 1\n[[package]]\nname = "fastapi"\nversion = "0.1.0"\n', encoding="utf-8")
    with pytest.raises(toolchain_pins.PinError, match="package 'starlette' not found"):
        toolchain_pins.backend_pins(lock)
    with pytest.raises(toolchain_pins.PinError, match="could not read lockfile"):
        toolchain_pins.backend_pins(tmp_path / "missing.lock")


def test_helper_typescript_version(tmp_path: Path) -> None:
    lock = tmp_path / "package-lock.json"
    lock.write_text(json.dumps({"packages": {"node_modules/typescript": {"version": "7.1.0-beta"}}}), encoding="utf-8")
    assert toolchain_pins.helper_typescript_version(lock) == "7.1.0-beta"
    lock.write_text(json.dumps({"packages": {}}), encoding="utf-8")
    assert toolchain_pins.helper_typescript_version(lock) is None
    assert toolchain_pins.helper_typescript_version(tmp_path / "missing.json") is None


def test_the_packaged_helper_lock_pins_typescript_7_1() -> None:
    assert toolchain_pins.helper_typescript_version() == "7.1.0-beta"
